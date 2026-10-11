"""The Kubernetes cluster backend: submit jobs, resolve futures, clean up blobs."""
from __future__ import annotations

import atexit
import concurrent.futures
import dataclasses
import logging
import os
import sys
import threading
import time
import weakref
from typing import Any, Callable, Iterator

from kubernetes.client import ApiException

import sk8s
from sk8s import anchors, serialize, spec
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
# Workflow ids that collide with non-workflow key prefixes in the store.
RESERVED_WORKFLOW_IDS = frozenset({"svc", "doctor"})
_FETCH_ATTEMPTS = 3
_RETRY_DELAY = 0.5  # seconds between result-fetch attempts (store write visibility lag)
_K8S_DEFAULT_BACKOFF_LIMIT = 6
# A cached anchor is re-read from the API when it was last verified more than this many
# seconds ago, so a workflow deleted behind our back (kill/clean from another process)
# fails loudly instead of every new Job being silently garbage-collected (dangling owner).
_ANCHOR_RECHECK_SECONDS = 60.0
_JOB_DELETED_REASON = "job was deleted before completing (workflow killed or expired?)"

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


def new_workflow_id() -> str:
    return "wf-" + spec.job_name("x").rsplit("-", 1)[1]


class Cluster:
    def __init__(
        self,
        config: Config | None = None,
        *,
        batch_api: Any | None = None,
        core_api: Any | None = None,
        apps_api: Any | None = None,
        store: BlobStore | None = None,
        watcher_events: Callable[[], Iterator[JobEvent]] | None = None,
        detached: bool = False,
        workflow_id: str | None = None,
        **overrides: Any,
    ) -> None:
        base = config if config is not None else load_config()
        self.config = dataclasses.replace(base, **overrides) if overrides else base
        if self.config.namespace is None:
            from sk8s import k8s
            self.config = dataclasses.replace(self.config, namespace=k8s.current_namespace())
        self._batch = batch_api
        self._core = core_api
        self._apps = apps_api
        self._store = store
        # An explicit workflow_id (attach) or the pod's SK8S_WORKFLOW_ID adopts that
        # workflow: use its anchor, never delete it from this cluster.
        inherited = workflow_id or os.environ.get("SK8S_WORKFLOW_ID")
        self.workflow_id = inherited or new_workflow_id()
        if self.workflow_id in RESERVED_WORKFLOW_IDS:
            raise ValueError(
                f"workflow id {self.workflow_id!r} is reserved (svc/ holds service blobs, "
                "doctor/ holds doctor probe keys); check SK8S_WORKFLOW_ID"
            )
        self._adopted = bool(inherited)
        self.detached = detached
        self._anchor: Any | None = None  # created/adopted lazily on first submit
        self._anchor_deleted = False
        self._anchor_verified_at = 0.0  # self._clock() when the cached anchor was last confirmed to exist
        self._clock: Callable[[], float] = time.monotonic
        self._keep_any = False  # some job kept its results: don't cascade-delete on exit
        self._anchor_lock = threading.Lock()
        self._closed = False
        self._pending: set[JobFuture] = set()  # unresolved futures (unlike _futures, which empties before resolve)
        self._atexit_hook: Callable[[], None] | None = None
        self._futures: dict[str, tuple[JobFuture, bool, str]] = {}  # name -> (future, keep_results, image)
        self._watcher = JobWatcher(
            watcher_events if watcher_events is not None else self._k8s_events,
            self._on_terminal,
            autostart=False,  # _k8s_events reads self._watcher: start only once it is assigned
        )
        self._watcher.start()

    def bind_workflow(self, workflow_id: str, *, adopt: bool, detached: bool = False) -> None:
        """Re-point an unused cluster at ``workflow_id`` (used by submit_workflow/attach).

        adopt=True: the workflow exists (or is owned elsewhere); never delete it from here.
        adopt=False: this cluster creates the workflow's anchor on first submit.
        """
        with self._anchor_lock:
            if self._anchor is not None or self._futures or self._closed:
                raise RuntimeError(
                    "this Cluster is already in use; a workflow needs a fresh Cluster "
                    "(pass cluster=None to let sk8s build one)"
                )
            self.workflow_id = workflow_id
            self._adopted = adopt
            self.detached = detached

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
    def apps(self) -> Any:
        if self._apps is None:
            from sk8s import k8s
            self._apps = k8s.apps_api()
        return self._apps

    @property
    def store(self) -> BlobStore:
        if self._store is None:
            self._store = get_store(
                self.config.store,
                namespace=self.config.namespace,
                core_api=self._core,
                endpoint=self.config.store_endpoint,
            )
        return self._store

    # --- workflow anchor ----------------------------------------------------
    def _ensure_anchor(self) -> Any:
        """Create (or adopt) the workflow's anchor on first use; reused afterwards."""
        with self._anchor_lock:
            if self._anchor_deleted:
                raise self._deleted_error()
            namespace = self.config.namespace
            assert namespace is not None
            if self._anchor is not None:
                now = self._clock()
                if now - self._anchor_verified_at > _ANCHOR_RECHECK_SECONDS:
                    try:
                        present = anchors.get_anchor(
                            self.core, namespace=namespace, workflow_id=self.workflow_id
                        ) is not None
                    except Exception:  # noqa: BLE001 — transient API trouble must not block submits
                        log.warning(
                            "could not re-verify anchor of workflow %s; assuming it exists",
                            self.workflow_id, exc_info=True,
                        )
                        present = True
                    if not present:
                        self._anchor_deleted = True
                        raise self._deleted_error()
                    self._anchor_verified_at = now
                return self._anchor
            anchor = None
            if self._adopted:
                anchor = anchors.get_anchor(
                    self.core, namespace=namespace, workflow_id=self.workflow_id
                )
            if anchor is None:
                anchor = anchors.create_anchor(
                    self.core,
                    namespace=namespace,
                    workflow_id=self.workflow_id,
                    store=self.config.store,
                    store_prefix=self.workflow_id,
                    retention_days=self.config.retention_days,
                    detached=self.detached and not self._adopted,
                )
            self._anchor = anchor
            self._anchor_verified_at = self._clock()
            self._register_atexit()
            return anchor

    def _deleted_error(self) -> RuntimeError:
        return RuntimeError(
            f"workflow {self.workflow_id} was deleted (by close(), kill, or clean); "
            "create a new Cluster to submit more jobs"
        )

    def _mark_anchor_deleted(self) -> None:
        """Poison this cluster: its workflow's anchor is gone, later submits raise."""
        with self._anchor_lock:
            self._anchor_deleted = True

    def _register_atexit(self) -> None:
        ref = weakref.ref(self)

        def hook() -> None:
            cluster = ref()
            if cluster is not None:
                cluster.close()

        self._atexit_hook = hook
        atexit.register(hook)

    def _forget(self, fut: Any) -> None:
        with self._anchor_lock:
            self._pending.discard(fut)

    def _auto_delete_ok(self) -> bool:
        """Default close() policy: only a finished, non-detached, driver-owned workflow
        that kept no results. Caller holds _anchor_lock."""
        if self._adopted or self.detached or self._keep_any or self._anchor is None:
            return False
        ann = getattr(self._anchor.metadata, "annotations", None) or {}
        if ann.get(anchors.DETACHED) == "true":
            return False
        return not any(not f.done() for f in self._pending)

    # --- events from the real control plane -------------------------------
    def _k8s_events(self) -> Iterator[JobEvent]:
        from kubernetes import watch as k8s_watch

        # LIST first: replays the current phase of every job, so terminal events
        # that fired while the stream was down (or before it first opened) are
        # not lost. The watch then resumes from the list's resourceVersion.
        # Snapshot the registered names BEFORE listing: a job registered before the LIST
        # started was created before it, so its absence from the LIST means it is gone
        # (a job registered mid-LIST must not be mistaken for a deleted one).
        expected = self._watcher.registered()
        listing = self.batch.list_namespaced_job(
            namespace=self.config.namespace, label_selector=_SELECTOR
        )
        listed: set[str] = set()
        for job in listing.items:
            listed.add(job.metadata.name)
            yield (job.metadata.name, job_phase(job))
        # Jobs we are waiting on that no longer exist (deleted while the stream was down,
        # or before it first opened) will never produce another event: resolve them as
        # failed. _collect still prefers a stored result blob over this verdict.
        for name in sorted(expected - listed):
            yield (name, "failed")
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
            if event.get("type") == "DELETED":
                # Gone, whatever its last status said. A finished job reaped by its TTL
                # still resolves with its result blob; an unfinished one fails.
                yield (job.metadata.name, "failed")
            else:
                yield (job.metadata.name, job_phase(job))
            # Every 30s of stream activity, check active jobs for stuck image pulls.
            now = time.monotonic()
            if now - last_pull_check > 30:
                last_pull_check = now
                yield from self._pull_failures()
        # stream timeout -> return; JobWatcher reconnects (and we re-LIST)
        yield from self._pull_failures()

    def _pull_failures(self) -> Iterator[JobEvent]:
        """Diagnostic probe for stuck image pulls. Best-effort: any failure here
        (missing pods RBAC, odd pod status) degrades to "no pull diagnostics" and
        must never prevent job events from flowing."""
        found: list[JobEvent] = []
        try:
            if not self._futures:
                return
            pods = self.core.list_namespaced_pod(
                namespace=self.config.namespace,
                label_selector=_SELECTOR,
            )
            for pod in pods.items:
                job = (pod.metadata.labels or {}).get("job-name")
                if job not in self._futures:
                    continue
                for cs in (pod.status.container_statuses or []) if pod.status else []:
                    waiting = cs.state.waiting if cs.state is not None else None
                    if waiting and waiting.reason in _WAITING_PULL_REASONS:
                        found.append((job, "imagepull"))
        except Exception:  # noqa: BLE001
            log.exception("image-pull probe failed; continuing without pull diagnostics")
            return
        yield from found

    # --- submission --------------------------------------------------------
    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
        opts = {k: kwargs.pop(k) for k in list(kwargs) if k in JOB_OPTIONS}
        opts.pop("timeout", None)  # timeout is a run()/result() concern
        name = spec.job_name(opts.get("name_prefix", "job"))
        namespace = self.config.namespace
        assert namespace is not None
        if opts.get("namespace") not in (None, namespace):
            raise ValueError(
                f"namespace={opts['namespace']!r} differs from this cluster's namespace "
                f"{namespace!r}: per-job namespaces arrive in a later release"
            )
        image = opts.get("image") or default_image(self.config)
        payload_key = f"{self.workflow_id}/{name}/payload"
        result_key = f"{self.workflow_id}/{name}/result"
        anchor = self._ensure_anchor()
        owner = anchors.owner_ref(anchor)
        payload_uri = self.store.put(
            payload_key, serialize.encode_payload(func, args, kwargs), owner=owner
        )
        job = spec.build_job(
            name,
            image=image,
            namespace=namespace,
            workflow_id=self.workflow_id,
            payload_uri=payload_uri,
            result_key=result_key,
            store=self.config.store,
            store_endpoint=self.config.store_endpoint,
            store_env=self.config.store_env,
            service_account=opts.get("service_account", self.config.service_account),
            pull_policy=opts.get("pull_policy", self.config.pull_policy),
            requests=opts.get("requests", {}),
            limits=opts.get("limits", {}),
            volumes=self._normalize_volumes(opts.get("volumes", {})),
            backoff_limit=opts.get("backoff_limit", 0),
            ttl=self.config.ttl_seconds_after_finished,
            privileged=opts.get("privileged", False),
            owner=owner,
            anchor_name=anchor.metadata.name,
            anchor_uid=anchor.metadata.uid,
        )
        try:
            self.batch.create_namespaced_job(namespace=namespace, body=job)
        except BaseException:
            # No job will ever consume (or clean up) the payload: don't leak it.
            try:
                self.store.delete(payload_uri)
            except Exception:  # noqa: BLE001 — best-effort; the create error is what matters
                log.exception("could not delete payload blob %s after failed create", payload_key)
            raise
        fut = JobFuture(name, self.workflow_id, backend=self)
        keep = opts.get("keep_results", self.config.keep_results)
        if keep:
            self._keep_any = True
        self._futures[name] = (fut, keep, image)
        with self._anchor_lock:
            self._pending.add(fut)
        fut.add_done_callback(self._forget)
        self._watcher.register(name)
        return fut

    def track_job(self, name: str, *, keep_results: bool) -> JobFuture:
        """Future for an already-submitted job of this workflow (e.g. a detached driver),
        resolved by the watcher like any submitted job."""
        existing = self._futures.get(name)
        if existing is not None:
            return existing[0]
        fut = JobFuture(name, self.workflow_id, backend=self)
        if keep_results:
            self._keep_any = True
        self._futures[name] = (fut, keep_results, default_image(self.config))
        self._watcher.register(name)
        return fut

    def untrack_job(self, name: str) -> None:
        """Stop watching a tracked job without resolving it."""
        self._watcher.unregister(name)
        self._futures.pop(name, None)

    def settle_job(self, name: str, phase: str) -> None:
        """Resolve a tracked job now from a phase observed by polling rather than the watch."""
        self._watcher.unregister(name)
        self._on_terminal(name, phase)

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
        if phase == "imagepull":
            self._delete_stuck_job(name)
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

    def _delete_stuck_job(self, name: str) -> None:
        """A job stuck in ImagePullBackOff never finishes, so its TTL never fires."""
        try:
            self.batch.delete_namespaced_job(
                name=name, namespace=self.config.namespace, propagation_policy="Background"
            )
        except Exception:  # noqa: BLE001 — best-effort; the future still resolves
            log.exception("could not delete image-pull-stuck job %s", name)

    def _cleanup(self, name: str, keep: bool) -> None:
        keys = [f"{self.workflow_id}/{name}/payload"]
        if not keep:
            keys.append(f"{self.workflow_id}/{name}/result")
        for key in keys:
            try:
                self.store.delete(self.store.uri_for(key))
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                log.exception("could not delete blob %s", key)

    def _job_is_gone(self, name: str) -> bool:
        try:
            self.batch.read_namespaced_job(name=name, namespace=self.config.namespace)
        except ApiException as exc:
            return bool(exc.status == 404)
        except Exception:  # noqa: BLE001 — cannot tell: fall back to the generic reason
            return False
        return False

    def _failure_reason(self, name: str) -> str:
        if self._job_is_gone(name):
            return _JOB_DELETED_REASON
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
        entry = self._futures.pop(job_name, None)
        try:
            self._watcher.unregister(job_name)
            keep = entry[1] if entry is not None else self.config.keep_results
            self._cleanup(job_name, keep)
        except Exception:  # noqa: BLE001 — the job is already deleted; never raise from cancel
            log.exception("post-cancel cleanup of job %s failed", job_name)
        return True

    def close(self, delete_workflow: bool | None = None) -> None:
        """Stop watching and (maybe) delete the workflow's anchor.

        delete_workflow=None deletes the anchor only if this cluster created it, it
        is not detached, and every future it issued has resolved; True forces,
        False skips. A workflow in which any job used keep_results is never deleted
        by default (it ages out via its retention annotation); delete_workflow=True
        is the explicit override. Safe to call repeatedly: the watcher is stopped
        once, the anchor is deleted at most once, and only the first default
        close() decides.
        """
        with self._anchor_lock:
            first = not self._closed
            self._closed = True
        if first:
            self._watcher.stop()
            if self._atexit_hook is not None:
                atexit.unregister(self._atexit_hook)
        with self._anchor_lock:
            if self._anchor is None or self._anchor_deleted:
                return
            if delete_workflow is None:
                if not first or not self._auto_delete_ok():
                    return
            elif not delete_workflow:
                return
            self._anchor_deleted = True
        namespace = self.config.namespace
        assert namespace is not None
        try:
            anchors.delete_anchor(
                self.core, self.store, namespace=namespace, workflow_id=self.workflow_id
            )
        except Exception:  # noqa: BLE001 — close() runs at interpreter exit; never raise
            log.exception("could not delete anchor for workflow %s", self.workflow_id)
