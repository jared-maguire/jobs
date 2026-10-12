"""The Kubernetes cluster backend: submit jobs, resolve futures, clean up blobs."""
from __future__ import annotations

import atexit
import concurrent.futures
import dataclasses
import datetime
import json
import logging
import os
import re
import sys
import threading
import time
import weakref
from typing import Any, Callable, Iterator, Literal, overload

from kubernetes import client
from kubernetes.client import ApiException

import sk8s
from sk8s import anchors, clones, serialize, spec, volumes
from sk8s import expect as expectations
from sk8s.config import Config, load_config
from sk8s.dryrun import DryRun, resource_totals, serialize_job
from sk8s.errors import (
    BlobNotFound, ImageNotConfigured, ImagePullError, JobRejected, JobTimeout, PodFailed, StoreError,
    VolumeAttachBlocked, VolumeError, VolumeProvisioningFailed,
)
from sk8s.future import JobFuture
from sk8s.store import get_store
from sk8s.store.base import BlobStore
from sk8s.watch import JobEvent, JobWatcher

JOB_OPTIONS = {
    "image", "requests", "limits", "volumes", "timeout", "backoff_limit",
    "service_account", "namespace", "name_prefix", "privileged",
    "pull_policy", "keep_results", "expect", "dry_run",
}

_WAITING_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
# Volume diagnoses (spike-report.md section 11). Both are events, not container waiting reasons:
# the pod only ever says ContainerCreating / Pending. Match on the persistent signatures only; the
# transient attach noise ("CSINode ... does not contain driver", "ControllerPublish not permitted
# ... backoff condition") shares the FailedAttachVolume reason and must never be fatal.
_ATTACH_BLOCKED = re.compile(r"Volume is already used by pod\(s\) (?P<pods>.+?)\s*$")
_NO_CONTENT_SOURCE = "VolumeContentSource must be provided"
_PV_IN_EVENT = re.compile(r'volume "(?P<pv>[^"]+)"')


def match_pv(message: str) -> str | None:
    m = _PV_IN_EVENT.search(message or "")
    return m.group("pv") if m else None

_STUCK_PHASES = ("imagepull", "volume-stuck")
# Stands in for the anchor's uid in a dry run's owner reference / SK8S_ANCHOR_UID (no anchor exists).
_DRY_RUN_UID = "<assigned-when-the-workflow-anchor-is-created>"
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

# API statuses meaning "the cluster refused this Job" (admission denial, forbidden, invalid).
_REJECTION_STATUSES = frozenset({400, 403, 422})

log = logging.getLogger("sk8s.cluster")


@dataclasses.dataclass
class _MountPlan:
    """What a submitted job needs done when it ends, for its clone-backed volumes.

    ``writers``: volumes it mounts read-write (each carries a writer marker until the job is
    over). ``readers``: volumes it mounts read-only through a clone (superseded clones are
    swept when it ends).
    """

    writers: list[str] = dataclasses.field(default_factory=list)
    readers: list[str] = dataclasses.field(default_factory=list)


def _job_rejected(name: str, exc: ApiException, *, privileged: bool) -> JobRejected:
    """Turn an API refusal into a typed error carrying the server's own message."""
    message = ""
    try:
        body = json.loads(exc.body) if exc.body else {}
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
    except (TypeError, ValueError):
        pass
    message = (message or str(exc.reason or "") or "rejected").strip()
    # Hints are gated on evidence in the server's message, not on the request alone:
    # a 403 can equally be an RBAC denial, and blaming privileged=True there misleads.
    hint = None
    lowered = message.lower()
    if privileged and "privileged" in lowered:
        hint = (
            "privileged=True was requested; managed clusters such as GKE Autopilot refuse "
            "privileged containers. Drop privileged=True or use a cluster that allows it."
        )
    elif exc.status == 403 and "forbidden" in lowered:
        hint = (
            "the credentials sk8s is using may lack permission to create Jobs in this "
            "namespace; run `sk8s doctor` to check the ServiceAccount's Role."
        )
    return JobRejected(name, exc.status, message, hint)


def default_image(config: Config) -> str:
    if config.image:
        return config.image
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"{config.image_prefix}ghcr.io/jared-maguire/sk8s:{sk8s.__version__}-py{py}"


def resolve_image(config: Config, explicit: str | None = None) -> str:
    """The image a new job/service/pod runs: ``explicit`` (the ``image`` option), else the
    configured image, else the ``image_prefix``-composed default name.

    Raises ``ImageNotConfigured`` when none is set: the bare default name (no prefix) is a
    ghcr.io path nothing is ever published to, so it could only fail later as a pull error.
    """
    if explicit:
        return explicit
    if config.image or config.image_prefix:
        return default_image(config)
    raise ImageNotConfigured()


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
        watch: bool = True,
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
        self._mount_plans: dict[str, _MountPlan] = {}  # job name -> clone-backed volume bookkeeping
        self._read_volumes: set[str] = set()  # clone-backed volumes this client mounted read-only
        self._diagnoses: dict[str, PodFailed] = {}  # job name -> the typed error for a "volume-stuck" job
        self._attach_blocked_since: dict[str, float] = {}  # pod name -> when its attach block was first seen
        self._provision_failing_since: dict[str, float] = {}  # clone claim -> first ProvisioningFailed sighting
        self._wall_clock: Callable[[], datetime.datetime] = (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        self._events_forbidden = False
        self._sleep: Callable[[float], None] = time.sleep
        self._watcher = JobWatcher(
            watcher_events if watcher_events is not None else self._k8s_events,
            self._on_terminal,
            autostart=False,  # _k8s_events reads self._watcher: start only once it is assigned
        )
        if watch:  # watch=False: a cluster that only previews (dry_run) never opens a stream
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
        yield from self._probe_failures()

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
                yield from self._probe_failures()
        # stream timeout -> return; JobWatcher reconnects (and we re-LIST)
        yield from self._probe_failures()

    def _probe_failures(self) -> Iterator[JobEvent]:
        """Every diagnostic probe, run on the same cadence (each stream (re)connect and every
        30s of stream activity, so a stuck job surfaces within about a minute of its grace)."""
        yield from self._pull_failures()
        yield from self._volume_failures()

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

    def _volume_failures(self) -> Iterator[JobEvent]:
        """Diagnostic probe for volume wedges. Best-effort like the pull probe: any failure
        degrades to "no volume diagnostics" and never stops job events from flowing."""
        found: list[JobEvent] = []
        try:
            if not self._futures:
                return
            pods = self.core.list_namespaced_pod(
                namespace=self.config.namespace, label_selector=_SELECTOR
            )
            waiting: list[tuple[str, Any]] = []  # (job, pod) for our pods that have not started
            live_pods = set()
            for pod in pods.items:
                job = (pod.metadata.labels or {}).get("job-name")
                if job not in self._futures:
                    continue
                if pod.status is None or pod.status.phase != "Pending":
                    continue  # running (or over): any event on it is history
                live_pods.add(pod.metadata.name)
                if any(v.persistent_volume_claim is not None for v in pod.spec.volumes or []):
                    waiting.append((job, pod))
            for gone in set(self._attach_blocked_since) - live_pods:
                del self._attach_blocked_since[gone]
            claims = {
                v.persistent_volume_claim.claim_name
                for _, pod in waiting for v in pod.spec.volumes or []
                if v.persistent_volume_claim is not None
            }
            for gone in set(self._provision_failing_since) - claims:
                del self._provision_failing_since[gone]
            if not waiting or self._events_forbidden:
                return
            attach = self._events("reason=FailedAttachVolume")
            if self._events_forbidden:
                return
            provisioning = self._events("involvedObject.kind=PersistentVolumeClaim,reason=ProvisioningFailed")
            for job, pod in waiting:
                diagnosis = self._diagnose_pod(pod, attach, provisioning)
                if diagnosis is not None:
                    self._diagnoses[job] = diagnosis
                    found.append((job, "volume-stuck"))
        except Exception:  # noqa: BLE001
            log.exception("volume probe failed; continuing without volume diagnostics")
            return
        yield from found

    def _events(self, field_selector: str) -> list[Any]:
        try:
            listing = self.core.list_namespaced_event(
                namespace=self.config.namespace, field_selector=field_selector
            )
        except ApiException as exc:
            if exc.status != 403:
                raise
            # The Role predates the events rule (re-run `sk8s config`): no diagnostics, say so once.
            self._events_forbidden = True
            log.warning(
                "not allowed to read events in namespace %s, so stuck volume mounts cannot be "
                "diagnosed; re-run `sk8s config` to grant the read-only events rule",
                self.config.namespace,
            )
            return []
        return list(listing.items)

    def _diagnose_pod(self, pod: Any, attach: list[Any], provisioning: list[Any]) -> PodFailed | None:
        job = pod.metadata.labels["job-name"]
        mounts = {
            v.persistent_volume_claim.claim_name: v.name
            for v in pod.spec.volumes or []
            if v.persistent_volume_claim is not None
        }
        for ev in provisioning:
            involved = ev.involved_object
            if involved.name not in mounts:
                continue
            if _NO_CONTENT_SOURCE in (ev.message or ""):  # infeasible: no grace period
                return VolumeProvisioningFailed(
                    job, mounts[involved.name], involved.name, ev.message
                )
            diagnosis = self._diagnose_clone(job, mounts[involved.name], involved.name, ev.message)
            if diagnosis is not None:
                return diagnosis
        for ev in attach:
            if ev.involved_object.name != pod.metadata.name:
                continue
            match = _ATTACH_BLOCKED.search(ev.message or "")
            if match is None:
                continue  # transient attach noise, not the persistent signature
            holders = [h for h in re.split(r"[,\s]+", match.group("pods")) if h]
            live = [h for h in holders if self._pod_holds_volumes(h)]
            if not live:
                self._attach_blocked_since.pop(pod.metadata.name, None)  # a detach in flight
                return None
            since = self._attach_blocked_since.setdefault(pod.metadata.name, self._clock())
            if self._clock() - since < self.config.volume_attach_grace_seconds:
                return None
            claim, volume = self._blocked_mount(mounts, match_pv(ev.message))
            return VolumeAttachBlocked(job, volume, claim, live, ev.message)
        return None

    def _diagnose_clone(self, job: str, volume: str, claim: str, message: str) -> PodFailed | None:
        """A ProvisioningFailed event on a clone sk8s created: fatal once it has stayed Pending
        for the grace period (the provisioner retries transient causes by itself)."""
        try:
            pvc = self.core.read_namespaced_persistent_volume_claim(
                name=claim, namespace=self.config.namespace
            )
        except ApiException:
            return None
        is_clone = (pvc.metadata.labels or {}).get(clones.KIND_LABEL) == clones.KIND_CLONE
        if not is_clone or pvc.status is None or pvc.status.phase != "Pending":
            self._provision_failing_since.pop(claim, None)  # provisioned since: the event is history
            return None
        since = self._provision_failing_since.setdefault(claim, self._clock())
        grace = self.config.volume_provision_grace_seconds
        if self._clock() - since < grace:
            return None
        return VolumeProvisioningFailed(job, volume, claim, message, clone_grace=grace)

    def _blocked_mount(self, mounts: dict[str, str], pv: str | None) -> tuple[str, str]:
        """(claim, volume) of the mount the attach event is about: the claim bound to PV ``pv``."""
        if pv is not None and len(mounts) > 1:
            for claim, volume in mounts.items():
                try:
                    bound = self.core.read_namespaced_persistent_volume_claim(
                        name=claim, namespace=self.config.namespace
                    ).spec.volume_name
                except ApiException:
                    continue
                if bound == pv:
                    return claim, volume
        return next(iter(mounts.items()))

    def _pod_holds_volumes(self, name: str) -> bool:
        """True unless the pod is over or gone (a finished pod releases its disk, spike section 7)."""
        try:
            pod = self.core.read_namespaced_pod(name=name, namespace=self.config.namespace)
        except ApiException as exc:
            return bool(exc.status != 404)
        phase = None if pod.status is None else pod.status.phase
        return phase not in ("Succeeded", "Failed")

    # --- submission --------------------------------------------------------
    @staticmethod
    def _expectation(opts: dict[str, Any]) -> Any:
        """The ``expect=`` gate of a submission (validated), or None."""
        expect = opts.get("expect")
        if expect is not None:
            expectations.require_callable(expect)
        return expect

    def _job_settings(self, opts: dict[str, Any]) -> tuple[str, str, str, list[volumes.Mount]]:
        """(job name, namespace, image, parsed mounts) for a submission; ValueError before any
        side effect. Shared by ``submit`` and ``dry_run`` so a preview validates the same way."""
        name = spec.job_name(opts.get("name_prefix", "job"))
        namespace = self.config.namespace
        assert namespace is not None
        if opts.get("namespace") not in (None, namespace):
            raise ValueError(
                f"namespace={opts['namespace']!r} differs from this cluster's namespace "
                f"{namespace!r}: per-job namespaces arrive in a later release"
            )
        image = resolve_image(self.config, opts.get("image"))  # before anything is created
        mounts = volumes.parse_mounts(opts.get("volumes"))
        return name, namespace, image, mounts

    def _build_job(
        self, name: str, *, image: str, namespace: str, opts: dict[str, Any], payload_uri: str,
        result_key: str, pod_mounts: list[spec.PodMount], owner: Any, anchor_name: str,
        anchor_uid: str,
    ) -> Any:
        return spec.build_job(
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
            volumes=pod_mounts,
            backoff_limit=opts.get("backoff_limit", 0),
            ttl=self.config.ttl_seconds_after_finished,
            privileged=opts.get("privileged", False),
            owner=owner,
            anchor_name=anchor_name,
            anchor_uid=anchor_uid,
        )

    @overload
    def submit(
        self, func: Callable[..., Any], /, *args: Any, dry_run: Literal[True], **kwargs: Any
    ) -> DryRun: ...

    @overload
    def submit(
        self, func: Callable[..., Any], /, *args: Any, dry_run: Literal[False] = ..., **kwargs: Any
    ) -> JobFuture: ...

    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture | DryRun:
        opts = {k: kwargs.pop(k) for k in list(kwargs) if k in JOB_OPTIONS}
        opts.pop("timeout", None)  # timeout is a run()/result() concern
        if opts.pop("dry_run", False):
            return self._dry_run(func, args, kwargs, opts)
        expect = self._expectation(opts)  # before any side effect
        name, namespace, image, mounts = self._job_settings(opts)
        # Order matters (see sk8s.clones, "Races"): writers are held back and MARKED first, before
        # anything slow; read-only mounts are resolved to a clone as LATE as possible, right before
        # the Job that references it. Everything after the markers cleans them up on failure.
        pvcs, plan = self._prepare_mounts(mounts, name)
        payload_key = f"{self.workflow_id}/{name}/payload"
        result_key = f"{self.workflow_id}/{name}/result"
        payload_uri: str | None = None
        job_created = False
        try:
            anchor = self._ensure_anchor()
            owner = anchors.owner_ref(anchor)
            payload_uri = self.store.put(
                payload_key, serialize.encode_payload(func, args, kwargs, expect=expect), owner=owner
            )
            pod_mounts = self._resolve_pod_mounts(mounts, pvcs, plan)
            job = self._build_job(
                name, image=image, namespace=namespace, opts=opts, payload_uri=payload_uri,
                result_key=result_key, pod_mounts=pod_mounts, owner=owner,
                anchor_name=anchor.metadata.name, anchor_uid=anchor.metadata.uid,
            )
            try:
                self.batch.create_namespaced_job(namespace=namespace, body=job)
            except ApiException as exc:
                if exc.status in _REJECTION_STATUSES:
                    raise _job_rejected(name, exc, privileged=bool(opts.get("privileged"))) from exc
                raise
            job_created = True
            self._verify_clones(pod_mounts)
        except BaseException:
            if job_created:  # a Job whose clone vanished would hang Pending forever
                self._delete_stuck_job(name)
            self._settle_writers(plan, name, wrote=False)  # the job never ran: nothing written
            if payload_uri is not None:  # no job will ever consume (or clean up) the payload
                try:
                    self.store.delete(payload_uri)
                except Exception:  # noqa: BLE001 — best-effort; the original error is what matters
                    log.exception("could not delete payload blob %s after failed submit", payload_key)
            raise
        fut = JobFuture(name, self.workflow_id, backend=self)
        keep = opts.get("keep_results", self.config.keep_results)
        if keep:
            self._keep_any = True
        if plan.writers or plan.readers:
            self._mount_plans[name] = plan
        self._futures[name] = (fut, keep, image)
        with self._anchor_lock:
            self._pending.add(fut)
        fut.add_done_callback(self._forget)
        self._watcher.register(name)
        return fut

    def dry_run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> DryRun:
        """Build the Job ``submit`` would create and return it as a :class:`DryRun`, creating
        nothing: no anchor, no payload upload, no API call (volumes are not even read)."""
        opts = {k: kwargs.pop(k) for k in list(kwargs) if k in JOB_OPTIONS}
        opts.pop("timeout", None)
        opts.pop("dry_run", None)
        return self._dry_run(func, args, kwargs, opts)

    def _dry_run(
        self, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any],
        opts: dict[str, Any],
    ) -> DryRun:
        from sk8s.store.configmap import ConfigMapStore, check_blob_size

        expect = self._expectation(opts)  # same checks, same order as submit
        name, namespace, image, mounts = self._job_settings(opts)  # same errors as submit
        payload_key = f"{self.workflow_id}/{name}/payload"
        # unpicklable args fail here too; expect rides in the payload exactly as in submit
        payload = serialize.encode_payload(func, args, kwargs, expect=expect)
        notes: list[str] = []
        configmap_store = (
            isinstance(self._store, ConfigMapStore) if self._store is not None
            else self.config.store == "k8s"
        )
        if configmap_store:
            check_blob_size(payload_key, payload)
        try:
            payload_uri = self.store.uri_for(payload_key)
        except Exception as exc:  # noqa: BLE001 — resolving the store must not make a preview fail
            payload_uri = f"<{self.config.store}>/{payload_key}"
            notes.append(
                f"the store could not be opened here ({type(exc).__name__}), so the payload URI "
                "is a placeholder"
            )
        # Read-only mounts of a ReadWriteOnce+ReadOnlyMany volume are served from a hidden clone
        # chosen at submit time; a preview does not read volumes, so every claim is the volume.
        pod_mounts = [spec.PodMount(m.name, m.name, m.mountpoint, m.read_only) for m in mounts]
        if any(m.read_only for m in mounts):
            notes.append(
                "read-only mounts show the volume itself as the claim; a ReadWriteOnce+ReadOnlyMany "
                "volume is mounted from a hidden read-only clone, which is chosen when the job is "
                "really submitted"
            )
        anchor_name = anchors.anchor_name(self.workflow_id)
        owner = client.V1OwnerReference(
            api_version="v1", kind="ConfigMap", name=anchor_name, uid=_DRY_RUN_UID, controller=True
        )
        job = self._build_job(
            name, image=image, namespace=namespace, opts=opts, payload_uri=payload_uri,
            result_key=f"{self.workflow_id}/{name}/result", pod_mounts=pod_mounts, owner=owner,
            anchor_name=anchor_name, anchor_uid=_DRY_RUN_UID,
        )
        cpu, memory = resource_totals(opts.get("requests"), opts.get("limits"))
        return DryRun(
            jobs=[serialize_job(job)], image=image, store=self.config.store, namespace=namespace,
            n_jobs=1, total_cpu_request=cpu, total_memory_request=memory,
            workflow_id=self.workflow_id, payload_bytes=len(payload), notes=notes,
        )

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

    # --- volumes: mode resolution and the hidden read-only clones ---------------------
    def _clone_manager(self) -> clones.CloneManager:
        namespace = self.config.namespace
        assert namespace is not None
        return clones.CloneManager(
            self.core, self.batch, namespace,
            writer_state=self._writer_state,
            job_active=lambda job: job_phase(job) == "active",
            wait_seconds=self.config.volume_wait_seconds,
            clock=self._clock, sleep=self._sleep,
            completed_at=self._completed_at, wall_clock=self._wall_clock,
        )

    def _completed_at(self, job_name: str) -> datetime.datetime | None:
        try:
            job = self.batch.read_namespaced_job(name=job_name, namespace=self.config.namespace)
            stamp = job.status.completion_time if job.status is not None else None
        except Exception:  # noqa: BLE001 — unknown ordering is handled conservatively
            return None
        return stamp if isinstance(stamp, datetime.datetime) else None

    def _writer_state(self, job_name: str) -> str:
        """running | succeeded | failed | gone, for an rw job named in a volume's writer marker."""
        phase = None
        try:
            job = self.batch.read_namespaced_job(name=job_name, namespace=self.config.namespace)
            phase = job_phase(job)
        except ApiException as exc:
            if exc.status != 404:
                log.warning("cannot read job %s to settle its volume: %s", job_name, exc)
                return clones.RUNNING  # unknown: never clone under a writer we cannot rule out
        if phase == "active":
            return clones.RUNNING
        try:  # a finished or deleted job's pods can linger (terminating) and still write
            pods = self.core.list_namespaced_pod(
                namespace=self.config.namespace, label_selector=f"job-name={job_name}"
            )
        except Exception:  # noqa: BLE001
            log.warning("cannot list pods of job %s to settle its volume", job_name, exc_info=True)
            return clones.RUNNING
        if any(getattr(p.status, "phase", None) in ("Pending", "Running") for p in pods.items):
            return clones.RUNNING
        return {"succeeded": clones.SUCCEEDED, "failed": clones.FAILED}.get(phase or "", clones.GONE)

    def _prepare_mounts(
        self, mounts: list[volumes.Mount], job_name: str
    ) -> tuple[dict[str, Any], _MountPlan]:
        """Read every volume; hold back and mark this job as a writer of each clone-backed
        volume it mounts read-write. A read error is fatal: an rw job must never run unmarked."""
        plan = _MountPlan()
        pvcs: dict[str, Any] = {}
        if not mounts:
            return pvcs, plan
        mgr = self._clone_manager()
        try:
            for m in mounts:
                pvc = mgr.read_volume(m.name)  # VolumeNotFound: fail now, not as a Pending pod
                pvcs[m.name] = pvc
                if not m.read_only and clones.is_clone_backed(pvc):
                    mgr.guard_writer_start(m.name, pvc)
                    mgr.mark_writer(m.name, job_name)
                    plan.writers.append(m.name)
        except BaseException:
            self._settle_writers(plan, job_name, wrote=False)
            raise
        return pvcs, plan

    def _resolve_pod_mounts(
        self, mounts: list[volumes.Mount], pvcs: dict[str, Any], plan: _MountPlan
    ) -> list[spec.PodMount]:
        """Pick the claim for each mount: the volume itself or, for a read-only mount of a
        clone-backed volume, its current clone (waits for a running writer, bounded)."""
        pod_mounts: list[spec.PodMount] = []
        mgr = self._clone_manager() if mounts else None
        for m in mounts:
            claim = m.name
            if m.read_only and clones.is_clone_backed(pvcs[m.name]):
                assert mgr is not None
                claim = mgr.resolve_read_only(m.name)
                plan.readers.append(m.name)
                self._read_volumes.add(m.name)
            pod_mounts.append(spec.PodMount(m.name, claim, m.mountpoint, m.read_only))
        return pod_mounts

    def _verify_clones(self, pod_mounts: list[spec.PodMount]) -> None:
        """The Job exists: make sure the clone it claims was not swept in the meantime."""
        for m in pod_mounts:
            if m.claim == m.name:
                continue
            try:
                claim = self.core.read_namespaced_persistent_volume_claim(
                    name=m.claim, namespace=self.config.namespace
                )
            except ApiException as exc:
                if exc.status != 404:
                    log.warning("cannot re-check clone %s: %s", m.claim, exc)
                    continue
                raise VolumeError(
                    f"the read-only clone {m.claim!r} of volume {m.name!r} was deleted while "
                    f"this job was being submitted (another client superseded it); nothing was "
                    f"started. Submit the job again."
                ) from exc
            # A Terminating clone (deletion issued, held open by finishing pods) will vanish
            # before our pod can bind it — treat it the same as already gone.
            if claim.metadata is not None and claim.metadata.deletion_timestamp is not None:
                raise VolumeError(
                    f"the read-only clone {m.claim!r} of volume {m.name!r} is being deleted "
                    f"(another client superseded it); nothing was started. Submit the job again."
                )

    def _mark_writers(self, plan: _MountPlan, job_name: str) -> None:
        if plan.writers:
            mgr = self._clone_manager()
            for volume in plan.writers:
                mgr.mark_writer(volume, job_name)

    def _settle_writers(self, plan: _MountPlan, job_name: str, *, wrote: bool) -> None:
        """Best effort: a leftover marker is reconciled by the next read-only mount anyway."""
        if not plan.writers:
            return
        try:
            mgr = self._clone_manager()
            for volume in plan.writers:
                mgr.settle_writer(volume, job_name, wrote=wrote)
        except Exception:  # noqa: BLE001
            log.exception("could not settle writer marker of job %s", job_name)

    def _finish_mounts(self, name: str, phase: str) -> None:
        """A job ended: make a completed writer the volume's new generation and sweep clones."""
        plan = self._mount_plans.pop(name, None)
        if plan is None:
            return
        try:
            if plan.writers:
                if phase == "succeeded":
                    state = clones.SUCCEEDED
                elif phase == "failed":
                    state = self._writer_state(name)  # a reaped (DELETED) job may have succeeded
                else:  # imagepull / volume-stuck: it never ran
                    state = clones.FAILED
                if state != clones.RUNNING:  # (running: the marker stays; a reader reconciles it)
                    self._settle_writers(
                        plan, name, wrote=state in (clones.SUCCEEDED, clones.GONE)
                    )
            if plan.readers:
                mgr = self._clone_manager()
                for volume in plan.readers:
                    mgr.sweep_stale(volume)
        except Exception:  # noqa: BLE001 — a future must resolve whatever happens here
            log.exception("could not finish volume bookkeeping of job %s", name)

    def run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", None)
        fut = self.submit(func, *args, **kwargs)
        if isinstance(fut, DryRun):  # run(..., dry_run=True): the preview is the answer
            return fut
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
        if phase in _STUCK_PHASES:
            self._delete_stuck_job(name)
        # Clean up before resolving so a caller woken by the future sees a clean store (and a
        # writer's volume already carries its new generation).
        self._cleanup(name, keep)
        self._finish_mounts(name, phase)
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
                if phase in _STUCK_PHASES:
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
        if phase == "volume-stuck":
            diagnosis = self._diagnoses.pop(name, None)
            return None, diagnosis or PodFailed(name, reason="its volume mount never completed")
        return None, PodFailed(name, reason=self._failure_reason(name), logs=self._safe_logs(name))

    def _delete_stuck_job(self, name: str) -> None:
        """A job stuck in ImagePullBackOff or on a volume never finishes, so its TTL never fires."""
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
        # A cancelled writer's marker is left for the next read-only mount to reconcile: the
        # Job is deleted in the foreground, so it only reads as "gone" once its pods are.
        self._mount_plans.pop(job_name, None)
        try:
            self._watcher.unregister(job_name)
            keep = entry[1] if entry is not None else self.config.keep_results
            self._cleanup(job_name, keep)
        except Exception:  # noqa: BLE001 — the job is already deleted; never raise from cancel
            log.exception("post-cancel cleanup of job %s failed", job_name)
        return True

    def _sweep_read_volumes(self) -> None:
        """Last chance to remove clones a young-age floor kept alive at reader end: nothing
        retries a skipped sweep, so close() does (floor 0; see sk8s.clones). Never raises."""
        if not self._read_volumes:
            return
        try:
            mgr = self._clone_manager()
            for volume in sorted(self._read_volumes):
                try:
                    mgr.sweep_stale(volume, min_age=0.0)
                except Exception:  # noqa: BLE001
                    log.exception("could not sweep superseded clones of volume %s", volume)
        except Exception:  # noqa: BLE001 — close() runs at interpreter exit
            log.exception("could not sweep superseded clones on close")

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
            self._sweep_read_volumes()
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
