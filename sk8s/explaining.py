"""``sk8s.explain(workflow_id)``: the whole picture of a workflow in one call.

Everything is reconstructed from cluster and store state alone (the workflow's anchor, its
Jobs, pods, PVCs, events and the blobs under its store prefix), so an agent with zero context
in any process gets the same answer. The module is READ-ONLY: it never creates, patches or
deletes anything.

The result is a plain, JSON-serializable dict; :func:`render` turns it into text for humans
(``sk8s explain <workflow>``). The module is named ``explaining`` (not ``explain``) so the
public function ``sk8s.explain`` does not shadow its own submodule, the way ``sk8s.clean``
and ``sk8s.cleaning`` coexist.

Stuck pods are *interpreted* with the same signatures the live watcher acts on (image-pull
reasons, ``FailedAttachVolume`` / ``ProvisioningFailed`` events) and the same typed-error
texts, but statelessly: a snapshot has no first-seen clock, so a volume held by a live pod is
reported as blocking at once rather than after the grace period.
"""
from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Callable

from kubernetes.client import ApiException

from sk8s import anchors, clones
from sk8s.cluster import _WAITING_PULL_REASONS, Cluster, job_phase
from sk8s.config import Config, load_config
from sk8s.diagnosis import SnapshotSightings, diagnose_pod_volumes, pod_holds_volumes, read_pvc
from sk8s.errors import Sk8sError
from sk8s.quantity import cpu_cores, effective_request, memory_bytes
from sk8s.store import get_store
from sk8s.store.base import BlobStore

# Documented constant behind ``estimated_cost_usd``: a round on-demand price for one vCPU-hour
# (GKE Autopilot general-purpose is about $0.045; an e2 node about $0.03). It is deliberately
# rough: only cpu requests x pod runtime, no memory, storage, GPUs, network or node overhead.
CPU_HOUR_USD = 0.04
COST_BASIS = (
    f"sum over pods of cpu_request x runtime x ${CPU_HOUR_USD:.2f} per vCPU-hour (USD); "
    "excludes memory, storage, GPUs, network and node overhead"
)
LOG_TAIL_LINES = 10
_RENDER_JOB_ROWS = 25  # a 10k-job map must not print 10k lines; the interesting ones come first

_MIN_TIME = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
_TERMINAL_POD_PHASES = ("Succeeded", "Failed")
_CONFIG_WAITING = {"CreateContainerConfigError", "CreateContainerError", "RunContainerError"}
_STARTING_WAITING = {"ContainerCreating", "PodInitializing"}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# Rebound by tests to freeze time; always called through the module global.
_now: Callable[[], datetime.datetime] = _utcnow


def format_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def _iso(stamp: Any) -> str | None:
    return stamp.isoformat() if isinstance(stamp, datetime.datetime) else None


def _aware(stamp: Any) -> datetime.datetime | None:
    if not isinstance(stamp, datetime.datetime):
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=datetime.timezone.utc)


# --- handles ---------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Ctx:
    namespace: str
    core: Any
    batch: Any
    config: Config
    cluster: Cluster | None

    def store_for(self, url: str) -> BlobStore:
        """The store the workflow's anchor records (the cluster's own when it is the same one)."""
        if self.cluster is not None and url == self.cluster.config.store:
            return self.cluster.store
        return get_store(
            url, namespace=self.namespace, core_api=self.core, endpoint=self.config.store_endpoint
        )


def _context(cluster: Any | None) -> _Ctx:
    if cluster is not None:
        if not isinstance(cluster, Cluster):
            raise TypeError(f"cluster must be an sk8s.Cluster or None, not {type(cluster).__name__}")
        namespace = cluster.config.namespace
        assert namespace is not None
        return _Ctx(namespace, cluster.core, cluster.batch, cluster.config, cluster)
    # No cluster: read-only handles from the configure() overrides, without the watcher thread a
    # Cluster starts (explain never submits, so it needs none).
    from sk8s import api, k8s

    with api._lock:
        overrides = dict(api._config_overrides)
    base = load_config()
    config = dataclasses.replace(base, **overrides) if overrides else base
    namespace = config.namespace or k8s.current_namespace()
    return _Ctx(namespace, k8s.core_api(), k8s.batch_api(), config, None)


# --- pod interpretation ------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Diagnosis:
    code: str
    text: str
    blocking: bool


class _Events:
    """Lazy, at-most-once reads of the two event streams the volume diagnoses need."""

    def __init__(self, ctx: _Ctx, warnings: list[str]) -> None:
        self._ctx = ctx
        self._warnings = warnings
        self._cache: dict[str, list[Any]] = {}
        self._broken = False

    def _read(self, field_selector: str) -> list[Any]:
        if field_selector in self._cache:
            return self._cache[field_selector]
        items: list[Any] = []
        if not self._broken:
            try:
                items = list(self._ctx.core.list_namespaced_event(
                    namespace=self._ctx.namespace, field_selector=field_selector
                ).items)
            except ApiException as exc:
                self._broken = True
                if exc.status == 403:
                    self._warnings.append(
                        f"not allowed to read events in namespace {self._ctx.namespace}, so stuck "
                        "volume mounts cannot be diagnosed; re-run `sk8s config` to grant the "
                        "read-only events rule"
                    )
                else:
                    self._warnings.append(f"could not read events (HTTP {exc.status}): volume diagnoses skipped")
        self._cache[field_selector] = items
        return items

    def attach_failures(self) -> list[Any]:
        return self._read("reason=FailedAttachVolume")

    def provisioning_failures(self) -> list[Any]:
        return self._read("involvedObject.kind=PersistentVolumeClaim,reason=ProvisioningFailed")


def _container_image(pod: Any, status: Any) -> str:
    for c in (pod.spec.containers if pod.spec is not None else None) or []:
        if c.name == status.name and c.image:
            return str(c.image)
    return str(status.image or "?")


def _pod_resource(pod: Any, resource: str) -> Any:
    """Sum of the pod's containers' effective request for ``resource`` ('cpu' -> float)."""
    total = 0.0
    seen = False
    for c in (pod.spec.containers if pod.spec is not None else None) or []:
        res = c.resources
        quantity = effective_request(
            getattr(res, "requests", None), getattr(res, "limits", None), resource
        )
        value = cpu_cores(quantity)
        if value is not None:
            total += value
            seen = True
    return total if seen else None


def _diagnose_volumes(ctx: _Ctx, pod: Any, events: _Events) -> _Diagnosis | None:
    """The shared volume-wedge rules (sk8s.diagnosis), in snapshot mode: no memory of how long a
    wedge has lasted, so whatever is wedged right now is reported (grace 0)."""
    if not any(v.persistent_volume_claim is not None for v in pod.spec.volumes or []):
        return None  # nothing to diagnose; do not even fetch events
    wedge = diagnose_pod_volumes(
        pod, events.attach_failures(), events.provisioning_failures(),
        read_pvc=lambda claim: read_pvc(ctx.core, ctx.namespace, claim),
        holds=lambda name: pod_holds_volumes(ctx.core, ctx.namespace, name),
        sightings=SnapshotSightings(), attach_grace=0.0, provision_grace=0.0,
    )
    if wedge is None:
        return None
    if wedge.kind == "attach":
        who = ", ".join(wedge.holders)
        grace = ctx.config.volume_attach_grace_seconds
        return _Diagnosis(
            "VolumeAttachBlocked",
            f"waiting for its volume {wedge.volume!r} (PVC {wedge.claim!r}): as of now it is "
            f"held by {who} on another node, and a ReadWriteOnce disk attaches to one node at "
            f"a time; it starts when the holder finishes. A live sk8s client fails the job "
            f"with VolumeAttachBlocked if this lasts over {grace:g}s. Kubernetes reports "
            f"FailedAttachVolume: {wedge.message}",
            True,
        )
    if wedge.kind == "clone-provisioning":
        return _Diagnosis(
            "VolumeProvisioningFailed",
            f"the read-only clone {wedge.claim!r} of its volume {wedge.volume!r} is still "
            f"Pending. Kubernetes reports ProvisioningFailed: {wedge.message}\n"
            "The provisioner keeps retrying; typical causes are storage quota or the cloud "
            "rate-limiting clones of one source disk: free quota or resubmit later.",
            True,
        )
    return _Diagnosis("VolumeProvisioningFailed", wedge.to_error().reason, True)


def _diagnose_waiting(ctx: _Ctx, pod: Any, events: _Events | None) -> _Diagnosis | None:
    """Why a not-yet-finished pod is not making progress, or None if it is simply running."""
    status = pod.status
    if status is None or status.phase in _TERMINAL_POD_PHASES:
        return None
    for cs in status.container_statuses or []:
        waiting = cs.state.waiting if cs.state is not None else None
        if waiting is None or not waiting.reason:
            continue
        reason, message = waiting.reason, (waiting.message or "").strip()
        detail = f": {message}" if message else ""
        if reason in _WAITING_PULL_REASONS:
            return _Diagnosis(
                reason,
                f"cannot pull image {_container_image(pod, cs)!r} ({reason}){detail}. Check that "
                "the image name and tag exist and that the cluster can reach its registry "
                "(set image=, SK8S_IMAGE or image_prefix).",
                True,
            )
        if reason == "CrashLoopBackOff":
            return _Diagnosis(
                reason, f"the container keeps crashing (CrashLoopBackOff){detail}; read the pod logs.", True
            )
        if reason in _CONFIG_WAITING:
            return _Diagnosis(
                reason, f"the container configuration is wrong ({reason}){detail}.", True
            )
        if reason not in _STARTING_WAITING:
            return _Diagnosis(reason, f"waiting: {reason}{detail}", False)
    for cond in status.conditions or []:
        if cond.type == "PodScheduled" and cond.status == "False":
            msg = (cond.message or cond.reason or "no node fits").strip()
            return _Diagnosis(
                "Unschedulable",
                f"not scheduled yet: {msg}. If the cluster autoscaler is adding a node this "
                "clears by itself; if it never does, reduce the job's requests or add capacity.",
                True,
            )
    if status.phase == "Pending" and events is not None:
        diagnosis = _diagnose_volumes(ctx, pod, events)
        if diagnosis is not None:
            return diagnosis
    for cs in status.container_statuses or []:
        waiting = cs.state.waiting if cs.state is not None else None
        if waiting is not None and waiting.reason in _STARTING_WAITING:
            return _Diagnosis(
                waiting.reason, "starting: the container is being created (pulling its image or mounting volumes)", False
            )
    if status.phase == "Pending":
        return _Diagnosis("Pending", "waiting to be scheduled onto a node", False)
    return None


def _memory_limit(pod: Any) -> str | None:
    for c in (pod.spec.containers if pod.spec is not None else None) or []:
        res = c.resources
        for source in (getattr(res, "limits", None), getattr(res, "requests", None)):
            if source and source.get("memory"):
                return str(source["memory"])
    return None


def _pod_failure(pod: Any) -> str | None:
    """Why a finished pod failed, in words (OOM, exit code, eviction), or None."""
    status = pod.status
    if status is None:
        return None
    for cs in status.container_statuses or []:
        for state in (cs.state, cs.last_state):
            term = state.terminated if state is not None else None
            if term is None:
                continue
            if term.reason == "OOMKilled":
                limit = _memory_limit(pod)
                shown = f"memory limit {limit}" if limit else "no memory limit set"
                return (
                    f"out of memory: the container was OOMKilled ({shown}); raise it, "
                    "e.g. limits={'memory': '2Gi'}"
                )
            if term.exit_code not in (0, None):
                extra = f" ({term.reason})" if term.reason and term.reason != "Error" else ""
                return f"the container exited with exit code {term.exit_code}{extra}; read the pod logs"
    if status.phase == "Failed" and status.reason:
        return f"pod {status.reason}: {status.message or 'no message'}"
    return None


# --- collection --------------------------------------------------------------------------------------


def _listed(call: Callable[[], Any], what: str, warnings: list[str]) -> list[Any]:
    try:
        return list(call().items)
    except Exception as exc:  # noqa: BLE001 — a missing section must not sink the whole picture
        status = f"HTTP {exc.status}" if isinstance(exc, ApiException) else type(exc).__name__
        warnings.append(f"could not list {what} ({status}); that section is incomplete")
        return []


def _pod_finished_at(pod: Any) -> datetime.datetime | None:
    stamps = []
    for cs in (pod.status.container_statuses if pod.status is not None else None) or []:
        term = cs.state.terminated if cs.state is not None else None
        if term is not None and _aware(term.finished_at) is not None:
            stamps.append(_aware(term.finished_at))
    return max(stamps) if stamps else None  # type: ignore[type-var]


def _job_template(job: Any) -> Any:
    try:
        return job.spec.template.spec.containers[0]
    except (AttributeError, IndexError, TypeError):
        return None


def _job_entry(
    job: Any, pods: list[Any], *, driver: str, now: datetime.datetime,
    ctx: _Ctx, events: _Events,
) -> tuple[dict[str, Any], _Diagnosis | None]:
    name = job.metadata.name
    k8s_phase = job_phase(job)
    pod_phases = [p.status.phase for p in pods if p.status is not None]
    if k8s_phase == "active":
        phase = "running" if "Running" in pod_phases else "pending"
    else:
        phase = k8s_phase
    status = job.status
    started = _aware(status.start_time) if status is not None else None
    completed = None
    if status is not None and phase == "succeeded":
        completed = _aware(status.completion_time)
    if completed is None and phase == "failed":
        for cond in (status.conditions if status is not None else None) or []:
            if cond.type == "Failed" and cond.status == "True":
                completed = _aware(cond.last_transition_time)
        if completed is None:
            finished = [t for t in map(_pod_finished_at, pods) if t is not None]
            completed = max(finished) if finished else None
    duration = None
    if started is not None:
        end = completed if phase in ("succeeded", "failed") else now
        if end is not None:
            duration = max(0.0, (end - started).total_seconds())
    template = _job_template(job)
    res = getattr(template, "resources", None)
    def request(resource: str) -> Any:
        return effective_request(
            getattr(res, "requests", None), getattr(res, "limits", None), resource
        )

    entry: dict[str, Any] = {
        "name": name,
        "role": "driver" if name == driver else "job",
        "phase": phase,
        "created_at": _iso(_aware(job.metadata.creation_timestamp)),
        "started_at": _iso(started),
        "completed_at": _iso(completed),
        "duration_seconds": duration,
        "image": getattr(template, "image", None),
        "cpu_request": cpu_cores(request("cpu")),
        "memory_request": memory_bytes(request("memory")),
        "pods": [
            {k: v for k, v in (("name", p.metadata.name), ("phase", p.status.phase if p.status else None),
                               ("node", p.spec.node_name if p.spec is not None else None)) if v}
            for p in sorted(pods, key=lambda p: p.metadata.name)
        ],
        "waiting_code": None,
        "waiting_reason": None,
        "failure_reason": None,
    }
    diagnosis: _Diagnosis | None = None
    if phase in ("pending", "running"):
        for pod in sorted(pods, key=lambda p: p.metadata.name):
            diagnosis = _diagnose_waiting(ctx, pod, events)
            if diagnosis is not None and diagnosis.blocking:
                break
        if diagnosis is not None:
            entry["waiting_code"] = diagnosis.code
            entry["waiting_reason"] = diagnosis.text
    elif phase == "failed":
        reason = next((r for r in (_pod_failure(p) for p in pods) if r), None)
        if reason is None:
            for cond in (status.conditions if status is not None else None) or []:
                if cond.type == "Failed" and cond.message:
                    reason = str(cond.message)
        entry["failure_reason"] = reason or "failed (no details left: its pods may have been reaped)"
    return entry, diagnosis


def _cost(pods: list[Any], now: datetime.datetime) -> tuple[float, bool]:
    """(cpu-hours, derivable): cpu request x runtime over pods that started and requested cpu."""
    seconds = 0.0
    derivable = False
    for pod in pods:
        status = pod.status
        cpu = _pod_resource(pod, "cpu")
        start = _aware(status.start_time) if status is not None else None
        if cpu is None or start is None:
            continue
        if status.phase in _TERMINAL_POD_PHASES:
            end = _pod_finished_at(pod)
            if end is None:
                continue
        else:
            end = now
        derivable = True
        seconds += cpu * max(0.0, (end - start).total_seconds())
    return seconds / 3600.0, derivable


def _volume_entries(pvcs: list[Any]) -> list[dict[str, Any]]:
    out = []
    for pvc in sorted(pvcs, key=lambda p: p.metadata.name):
        labels = pvc.metadata.labels or {}
        spec = pvc.spec
        requests = getattr(getattr(spec, "resources", None), "requests", None) or {}
        out.append({
            "name": pvc.metadata.name,
            "kind": labels.get(clones.KIND_LABEL, "volume"),
            "phase": pvc.status.phase if pvc.status is not None else None,
            "size": requests.get("storage"),
            "access_modes": list(getattr(spec, "access_modes", None) or []),
            "storage_class": getattr(spec, "storage_class_name", None),
            "clone_of": labels.get(clones.CLONE_OF_LABEL),
            "clone_backed": clones.is_clone_backed(pvc),
            "writer_generation": clones.generation(pvc) or None,
            "active_writers": clones.active_writers(pvc),
        })
    return out


def _driver_logs(ctx: _Ctx, pods: list[Any], warnings: list[str]) -> list[str] | None:
    if not pods:
        return []
    # The newest pod by creation time: a retried driver (backoff) leaves older failed pods behind.
    pod = max(pods, key=lambda p: (_aware(p.metadata.creation_timestamp) or _MIN_TIME, p.metadata.name))
    try:
        text = ctx.core.read_namespaced_pod_log(
            name=pod.metadata.name, namespace=ctx.namespace, tail_lines=LOG_TAIL_LINES
        )
    except Exception as exc:  # noqa: BLE001
        status = f"HTTP {exc.status}" if isinstance(exc, ApiException) else type(exc).__name__
        warnings.append(f"could not read the driver's logs ({status})")
        return None
    return str(text).splitlines()[-LOG_TAIL_LINES:]


def explain(workflow_id: str, cluster: Any | None = None) -> dict[str, Any]:
    """Everything about a workflow in one JSON-serializable dict; see the module docstring.

    Raises ``Sk8sError`` for an unknown workflow. ``cluster`` (an ``sk8s.Cluster``) supplies the
    namespace, API clients and store; ``None`` builds read-only handles from the ``configure()``
    overrides and the config file (and deliberately ignores ``configure(cluster=obj)``, as
    ``attach`` does). Nothing is created, patched or deleted.

    Keys: ``workflow`` (id, namespace, detached, created_at, age, age_seconds, retention_days,
    anchor), ``driver`` (job, phase, logs_tail; None for a workflow with no recorded driver),
    ``jobs`` (name, role, phase, timings, requests, pods, ``waiting_code``/``waiting_reason`` for
    stuck pods, ``failure_reason`` for failed jobs), ``counts`` (running/pending/succeeded/failed/
    total, driver included), ``volumes``, ``store`` (url, prefix, blob_count), ``blocking`` (plain
    English statements; empty when healthy) and ``warnings`` (sections that could not be read).
    ``estimated_cost_usd`` (+ ``estimated_cpu_hours``, ``estimated_cost_label`` = ``"rough"``,
    ``estimated_cost_basis``) appears only when some pod both requested cpu and has a runtime.
    """
    ctx = _context(cluster)
    now = _now()
    namespace = ctx.namespace
    anchor = anchors.get_anchor(ctx.core, namespace=namespace, workflow_id=workflow_id)
    if anchor is None:
        raise Sk8sError(f"unknown workflow {workflow_id}: no anchor in namespace {namespace}")
    ann = anchor.metadata.annotations or {}
    warnings: list[str] = []

    created = None
    try:
        created = _aware(datetime.datetime.fromisoformat(ann[anchors.CREATED_AT]))
    except (KeyError, TypeError, ValueError):
        warnings.append("the anchor has no readable creation time; age unknown")
    age_seconds = None if created is None else max(0.0, (now - created).total_seconds())
    try:
        retention: int | None = int(ann[anchors.RETENTION_DAYS])
    except (KeyError, TypeError, ValueError):
        retention = None
    detached = ann.get(anchors.DETACHED) == "true"
    driver_name = ann.get(anchors.DRIVER_JOB, "")
    store_url = ann.get(anchors.STORE) or ctx.config.store
    prefix = ann.get(anchors.STORE_PREFIX) or workflow_id

    selector = f"{anchors.WORKFLOW_LABEL}={workflow_id}"
    jobs = list(ctx.batch.list_namespaced_job(namespace=namespace, label_selector=selector).items)
    pods = _listed(
        lambda: ctx.core.list_namespaced_pod(namespace=namespace, label_selector=selector),
        "pods", warnings,
    )
    pvcs = _listed(
        lambda: ctx.core.list_namespaced_persistent_volume_claim(
            namespace=namespace, label_selector=selector
        ),
        "volumes", warnings,
    )
    pods_by_job: dict[str, list[Any]] = {}
    for pod in pods:
        pods_by_job.setdefault((pod.metadata.labels or {}).get("job-name", ""), []).append(pod)

    events = _Events(ctx, warnings)
    entries: list[dict[str, Any]] = []
    blocking: list[str] = []
    for job in sorted(jobs, key=lambda j: (_aware(j.metadata.creation_timestamp) or _MIN_TIME, j.metadata.name)):
        entry, diagnosis = _job_entry(
            job, pods_by_job.get(job.metadata.name, []), driver=driver_name, now=now,
            ctx=ctx, events=events,
        )
        entries.append(entry)
        if diagnosis is not None and diagnosis.blocking:
            blocking.append(f"job {entry['name']}: {diagnosis.text}")

    counts = {"running": 0, "pending": 0, "succeeded": 0, "failed": 0}
    for entry in entries:
        counts[entry["phase"]] += 1
    counts_out: dict[str, int] = {**counts, "total": len(entries)}

    driver: dict[str, Any] | None = None
    if driver_name:
        found = next((e for e in entries if e["name"] == driver_name), None)
        driver = {
            "job": driver_name,
            "phase": found["phase"] if found is not None else "unknown",
            "logs_tail": _driver_logs(ctx, pods_by_job.get(driver_name, []), warnings),
        }
        if found is None:
            driver["note"] = "the driver job no longer exists (finished and reaped by its TTL, or the workflow was killed)"
        elif found["phase"] == "failed":
            tail = [line for line in (driver["logs_tail"] or []) if line.strip()]
            last = f"; last log line: {tail[-1]}" if tail else ""
            blocking.append(f"driver {driver_name} failed: {found['failure_reason']}{last}")
    driver_done = driver is not None and driver["phase"] == "succeeded"
    if not driver_done:
        for entry in entries:
            if entry["phase"] == "failed" and entry["role"] != "driver":
                blocking.append(f"job {entry['name']} failed: {entry['failure_reason']}")

    blob_count: int | None = None
    blob_note: str | None = None
    try:
        blob_count = ctx.store_for(store_url).count_prefix(prefix)
    except NotImplementedError:
        blob_note = "this store does not support counting blobs"
    except Exception as exc:  # noqa: BLE001
        blob_note = f"could not count blobs: {type(exc).__name__}: {exc}"
        warnings.append(f"could not count blobs in store {store_url!r}: {type(exc).__name__}: {exc}")

    report: dict[str, Any] = {
        "workflow": {
            "id": workflow_id,
            "namespace": namespace,
            "detached": detached,
            "created_at": _iso(created),
            "age_seconds": age_seconds,
            "age": None if age_seconds is None else format_age(age_seconds),
            "retention_days": retention,
            "anchor": {"name": anchor.metadata.name, "uid": anchor.metadata.uid},
        },
        "driver": driver,
        "jobs": entries,
        "counts": counts_out,
        "volumes": _volume_entries(pvcs),
        "store": {
            "url": store_url, "prefix": prefix, "blob_count": blob_count,
            **({"blob_count_note": blob_note} if blob_count is None else {}),
        },
        "blocking": blocking,
        "warnings": warnings,
    }
    cpu_hours, derivable = _cost(pods, now)
    if derivable:
        report["estimated_cpu_hours"] = cpu_hours
        report["estimated_cost_usd"] = cpu_hours * CPU_HOUR_USD
        report["estimated_cost_label"] = "rough"
        report["estimated_cost_basis"] = COST_BASIS
    return report


# --- human rendering ------------------------------------------------------------------------------------


def _duration(seconds: float | None) -> str:
    return "-" if seconds is None else format_age(seconds)


def render(report: dict[str, Any]) -> str:
    """The report as text for people; ``--json`` is the same data for programs."""
    wf = report["workflow"]
    lines = [
        f"workflow {wf['id']}  namespace {wf['namespace']}  "
        f"{'detached' if wf['detached'] else 'tethered'}  age {wf['age'] or '?'}"
        + (f"  retention {wf['retention_days']}d" if wf["retention_days"] is not None else "")
    ]
    store = report["store"]
    if store["blob_count"] is None:
        lines.append(f"store {store['url']}  blob count unavailable ({store.get('blob_count_note', '?')})")
    else:
        lines.append(f"store {store['url']}  {store['blob_count']} blob(s) under {store['prefix']}/")
    driver = report["driver"]
    if driver is not None:
        lines.append(f"driver {driver['job']}  {driver['phase']}")
        if driver.get("note"):
            lines.append(f"  ({driver['note']})")
        if driver["logs_tail"]:
            lines.append("  last log lines:")
            lines.extend(f"    {line}" for line in driver["logs_tail"])
    c = report["counts"]
    lines.append("")
    lines.append(
        f"jobs: {c['running']} running, {c['pending']} pending, {c['succeeded']} succeeded, "
        f"{c['failed']} failed ({c['total']} total)"
    )
    # Anything that is not a plain success first, then the successes; capped for huge maps.
    interesting = [j for j in report["jobs"] if j["phase"] != "succeeded"]
    rest = [j for j in report["jobs"] if j["phase"] == "succeeded"]
    ordered = interesting + rest
    shown = ordered[:_RENDER_JOB_ROWS]
    if shown:
        width = max(len(j["name"]) for j in shown)
        for j in shown:
            # The full explanation is under "blocking:" below; a table row only needs a pointer.
            if j["waiting_code"]:
                note = f"  [{j['waiting_code']}]"
            elif j["failure_reason"]:
                first = j["failure_reason"].splitlines()[0]
                note = f"  {first if len(first) <= 70 else first[:67] + '...'}"
            else:
                note = ""
            lines.append(
                f"  {j['name']:<{width}}  {j['phase']:<9}  {_duration(j['duration_seconds']):>5}{note}"
            )
        if len(ordered) > len(shown):
            lines.append(f"  ... and {len(ordered) - len(shown)} more (use --json for all of them)")
    if report["volumes"]:
        lines.append("")
        lines.append("volumes:")
        for v in report["volumes"]:
            extra = f" clone of {v['clone_of']}" if v["clone_of"] else ""
            gen = f" generation {v['writer_generation']}" if v["writer_generation"] else ""
            lines.append(f"  {v['name']}  {v['kind']}  {v['phase']}  {v['size'] or '?'}{extra}{gen}")
    if "estimated_cost_usd" in report:
        lines.append("")
        cost = report["estimated_cost_usd"]
        money = f"{cost:.2f}" if cost >= 0.01 else f"{cost:.4f}"
        lines.append(
            f"estimated cost so far: ~${money} "
            f"({report['estimated_cost_label']}; {report['estimated_cpu_hours']:.2f} cpu-hours)"
        )
    lines.append("")
    if report["blocking"]:
        lines.append("blocking:")
        for statement in report["blocking"]:
            first, *more = statement.splitlines()
            lines.append(f"  - {first}")
            lines.extend(f"    {m}" for m in more)
    else:
        lines.append("nothing is blocking progress")
    if report["warnings"]:
        lines.append("")
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in report["warnings"])
    return "\n".join(lines) + "\n"
