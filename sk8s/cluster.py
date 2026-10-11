"""The Kubernetes cluster backend: submit jobs, resolve futures, clean up blobs."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
import os
import sys
import time
from typing import Any, Callable, Iterator

import sk8s
from sk8s import serialize, spec
from sk8s.config import Config, load_config
from sk8s.errors import BlobNotFound, ImagePullError, JobTimeout, PodFailed, StoreError
from sk8s.future import JobFuture
from sk8s.store import get_store
from sk8s.store.base import BlobStore
from sk8s.watch import JobEvent, JobWatcher

JOB_OPTIONS = {
    "image", "requests", "limits", "volumes", "timeout", "backoff_limit",
    "service_account", "namespace", "name_prefix", "privileged",
    "pull_policy", "keep_results",
}

_WAITING_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}
_SELECTOR = "app.kubernetes.io/managed-by=sk8s"
_FETCH_ATTEMPTS = 3
_RETRY_DELAY = 0.5  # seconds between result-fetch attempts (store write visibility lag)
_K8S_DEFAULT_BACKOFF_LIMIT = 6

log = logging.getLogger("sk8s.cluster")


def default_image(config: Config) -> str:
    if config.image:
        return config.image
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"{config.image_prefix}ghcr.io/jared-maguire/sk8s:{sk8s.__version__}-py{py}"


def job_phase(job: Any) -> str:
    """Map a V1Job's status to a watcher phase: succeeded | failed | active.

    A job is only "failed" once Kubernetes will not retry it: a Failed condition,
    or more failed pods than backoffLimit allows. failed > 0 alone is not enough
    when backoff_limit > 0 (the job is still retrying).
    """
    status = job.status
    if status is None:
        return "active"
    if (status.succeeded or 0) > 0:
        return "succeeded"
    for cond in status.conditions or []:
        if cond.type == "Failed" and cond.status == "True":
            return "failed"
    spec_ = getattr(job, "spec", None)
    limit = getattr(spec_, "backoff_limit", None)
    if limit is None:
        limit = _K8S_DEFAULT_BACKOFF_LIMIT if spec_ is not None else 0
    if (status.failed or 0) > limit:
        return "failed"
    return "active"


class Cluster:
    def __init__(
        self,
        config: Config | None = None,
        *,
        batch_api: Any | None = None,
        core_api: Any | None = None,
        store: BlobStore | None = None,
        watcher_events: Callable[[], Iterator[JobEvent]] | None = None,
        **overrides: Any,
    ) -> None:
        base = config if config is not None else load_config()
        self.config = dataclasses.replace(base, **overrides) if overrides else base
        if self.config.namespace is None:
            from sk8s import k8s
            self.config = dataclasses.replace(self.config, namespace=k8s.current_namespace())
        self._batch = batch_api
        self._core = core_api
        self._store = store
        self.workflow_id = os.environ.get("SK8S_WORKFLOW_ID") or "wf-" + spec.job_name("x").rsplit("-", 1)[1]
        self._futures: dict[str, tuple[JobFuture, bool, str]] = {}  # name -> (future, keep_results, image)
        self._watcher = JobWatcher(
            watcher_events if watcher_events is not None else self._k8s_events,
            self._on_terminal,
        )

    # --- lazy client/store accessors -------------------------------------
    @property
    def batch(self) -> Any:
        if self._batch is None:
            from sk8s import k8s
            self._batch = k8s.batch_api()
        return self._batch

    @property
    def core(self) -> Any:
        if self._core is None:
            from sk8s import k8s
            self._core = k8s.core_api()
        return self._core

    @property
    def store(self) -> BlobStore:
        if self._store is None:
            self._store = get_store(
                self.config.store, namespace=self.config.namespace, core_api=self._core
            )
        return self._store

    # --- events from the real control plane -------------------------------
    def _k8s_events(self) -> Iterator[JobEvent]:
        from kubernetes import watch as k8s_watch

        # LIST first: replays the current phase of every job, so terminal events
        # that fired while the stream was down (or before it first opened) are
        # not lost. The watch then resumes from the list's resourceVersion.
        listing = self.batch.list_namespaced_job(
            namespace=self.config.namespace, label_selector=_SELECTOR
        )
        for job in listing.items:
            yield (job.metadata.name, job_phase(job))
        yield from self._pull_failures()

        stream_kwargs: dict[str, Any] = {}
        resource_version = getattr(getattr(listing, "metadata", None), "resource_version", None)
        if resource_version:
            stream_kwargs["resource_version"] = resource_version
        w = k8s_watch.Watch()  # type: ignore[no-untyped-call]
        last_pull_check = time.monotonic()
        for event in w.stream(  # type: ignore[no-untyped-call]
            self.batch.list_namespaced_job,
            namespace=self.config.namespace,
            label_selector=_SELECTOR,
            timeout_seconds=30,
            **stream_kwargs,
        ):
            job = event["object"]
            yield (job.metadata.name, job_phase(job))
            # Every 30s of stream activity, check active jobs for stuck image pulls.
            now = time.monotonic()
            if now - last_pull_check > 30:
                last_pull_check = now
                yield from self._pull_failures()
        # stream timeout -> return; JobWatcher reconnects (and we re-LIST)
        yield from self._pull_failures()

    def _pull_failures(self) -> Iterator[JobEvent]:
        with_futures = list(self._futures)
        if not with_futures:
            return
        pods = self.core.list_namespaced_pod(
            namespace=self.config.namespace,
            label_selector=_SELECTOR,
        )
        for pod in pods.items:
            job = (pod.metadata.labels or {}).get("job-name")
            if job not in self._futures:
                continue
            for cs in pod.status.container_statuses or []:
                waiting = cs.state.waiting
                if waiting and waiting.reason in _WAITING_PULL_REASONS:
                    yield (job, "imagepull")

    # --- submission --------------------------------------------------------
    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
        opts = {k: kwargs.pop(k) for k in list(kwargs) if k in JOB_OPTIONS}
        opts.pop("timeout", None)  # timeout is a run()/result() concern
        name = spec.job_name(opts.get("name_prefix", "job"))
        namespace = opts.get("namespace") or self.config.namespace
        assert namespace is not None
        image = opts.get("image") or default_image(self.config)
        payload_key = f"{self.workflow_id}/{name}/payload"
        result_key = f"{self.workflow_id}/{name}/result"
        payload_uri = self.store.put(payload_key, serialize.encode_payload(func, args, kwargs))
        job = spec.build_job(
            name,
            image=image,
            namespace=namespace,
            workflow_id=self.workflow_id,
            payload_uri=payload_uri,
            result_key=result_key,
            store=self.config.store,
            service_account=opts.get("service_account", self.config.service_account),
            pull_policy=opts.get("pull_policy", self.config.pull_policy),
            requests=opts.get("requests", {}),
            limits=opts.get("limits", {}),
            volumes=self._normalize_volumes(opts.get("volumes", {})),
            backoff_limit=opts.get("backoff_limit", 0),
            ttl=self.config.ttl_seconds_after_finished,
            privileged=opts.get("privileged", False),
        )
        self.batch.create_namespaced_job(namespace=namespace, body=job)
        fut = JobFuture(name, self.workflow_id, backend=self)
        keep = opts.get("keep_results", self.config.keep_results)
        self._futures[name] = (fut, keep, image)
        self._watcher.register(name)
        return fut

    @staticmethod
    def _normalize_volumes(volumes: Any) -> dict[str, str]:
        if isinstance(volumes, str):
            return {volumes: f"/mnt/{volumes}"}
        if isinstance(volumes, (list, tuple)):
            return {v: f"/mnt/{v}" for v in volumes}
        return dict(volumes)

    def run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", None)
        fut = self.submit(func, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # On 3.10 this is NOT the builtin TimeoutError; catching the
            # concurrent.futures name is correct on every supported version.
            raise JobTimeout(fut.job_name, timeout or 0.0) from None

    # --- resolution --------------------------------------------------------
    def _on_terminal(self, name: str, phase: str) -> None:
        """Resolve the future for a finished job. Never raises: whatever goes wrong,
        the future ends resolved (typed exception) and blob cleanup is best-effort."""
        entry = self._futures.pop(name, None)
        if entry is None:
            return
        fut, keep, image = entry
        value: Any = None
        error: BaseException | None = None
        try:
            value, error = self._collect(name, phase, image)
        except BaseException as exc:  # noqa: BLE001 — a future must never be stranded
            log.exception("resolving job %s failed unexpectedly", name)
            error = StoreError(f"could not resolve job {name}: {exc!r}")
        # Clean up before resolving so a caller woken by the future sees a clean store.
        self._cleanup(name, keep)
        try:
            if error is not None:
                fut.set_exception(error)
            else:
                fut.set_result(value)
        except concurrent.futures.InvalidStateError:
            pass  # cancelled (or otherwise finished) while we were fetching

    def _collect(self, name: str, phase: str, image: str) -> tuple[Any, BaseException | None]:
        """Returns (value, None) or (None, exception) for a terminal job."""
        result_uri = self.store.uri_for(f"{self.workflow_id}/{name}/result")
        blob: bytes | None = None
        fetch_error: Exception | None = None
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                blob = self.store.get(result_uri)
                fetch_error = None
                break
            except BlobNotFound as exc:
                fetch_error = exc
                if phase == "imagepull":
                    break
            except Exception as exc:  # noqa: BLE001 — store trouble must not strand the future
                fetch_error = exc
            if attempt < _FETCH_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY)
        if blob is not None:
            try:
                return serialize.decode_result(blob), None
            except BaseException as exc:  # noqa: BLE001 — remote exception re-raise
                return None, exc
        if fetch_error is not None and not isinstance(fetch_error, BlobNotFound):
            if isinstance(fetch_error, StoreError):
                return None, fetch_error
            err = StoreError(f"could not fetch result of job {name}: {fetch_error!r}")
            err.__cause__ = fetch_error
            return None, err
        if phase == "imagepull":
            return None, ImagePullError(name, image=image)
        return None, PodFailed(name, reason=self._failure_reason(name), logs=self._safe_logs(name))

    def _cleanup(self, name: str, keep: bool) -> None:
        keys = [f"{self.workflow_id}/{name}/payload"]
        if not keep:
            keys.append(f"{self.workflow_id}/{name}/result")
        for key in keys:
            try:
                self.store.delete(self.store.uri_for(key))
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                log.exception("could not delete blob %s", key)

    def _failure_reason(self, name: str) -> str:
        try:
            pods = self.core.list_namespaced_pod(
                namespace=self.config.namespace, label_selector=f"job-name={name}"
            )
            for pod in pods.items:
                for cs in pod.status.container_statuses or []:
                    if cs.state.terminated and cs.state.terminated.reason:
                        return str(cs.state.terminated.reason)
        except Exception:  # noqa: BLE001 — diagnostics must not mask the failure
            pass
        return "job failed and left no result (pod details unavailable)"

    def _safe_logs(self, name: str) -> str | None:
        try:
            return self.logs(name)
        except Exception:  # noqa: BLE001
            return None

    # --- JobBackend --------------------------------------------------------
    def logs(self, job_name: str) -> str:
        pods = self.core.list_namespaced_pod(
            namespace=self.config.namespace, label_selector=f"job-name={job_name}"
        )
        chunks = []
        for pod in pods.items:
            chunks.append(
                self.core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=self.config.namespace
                )
            )
        return "\n".join(chunks)

    def cancel(self, job_name: str) -> bool:
        try:
            self.batch.delete_namespaced_job(
                name=job_name, namespace=self.config.namespace,
                propagation_policy="Foreground",
            )
        except Exception:  # noqa: BLE001
            return False
        self._futures.pop(job_name, None)
        return True

    def close(self) -> None:
        self._watcher.stop()
