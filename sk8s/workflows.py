"""Detached workflows: submit a driver function, walk away, attach later."""
from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any, Callable

from sk8s import anchors
from sk8s.api import _new_cluster
from sk8s.cluster import Cluster, job_phase, new_workflow_id
from sk8s.errors import BlobNotFound, JobTimeout, Sk8sError
from sk8s.future import JobFuture

_TERMINAL = ("succeeded", "failed")
_POLL = 2.0  # seconds between liveness checks while waiting on the driver


def _check_cluster(cluster: Any) -> None:
    if cluster is not None and not isinstance(cluster, Cluster):
        raise TypeError(
            f"cluster must be an sk8s.Cluster or None, not {type(cluster).__name__}"
        )


def submit_workflow(
    func: Callable[..., Any], /, *args: Any, cluster: Any | None = None, **job_opts: Any
) -> WorkflowHandle:
    """Run ``func`` as the driver of a new, detached workflow and return immediately.

    The driver's own submissions are owned by the workflow's anchor, which this
    process never deletes. Keyword arguments are split as in ``submit``.

    Cluster rule: ``cluster=None`` builds a fresh ``Cluster`` from the ``configure()``
    overrides (the handle owns it) and deliberately ignores a cluster installed with
    ``configure(cluster=obj)``. A ``cluster`` you pass must be unused (otherwise
    ``RuntimeError``) and becomes dedicated to the new workflow; it is not closed
    by the handle. A non-``Cluster`` argument raises ``TypeError``.
    """
    _check_cluster(cluster)
    owned = cluster is None
    c: Cluster = cluster if cluster is not None else _new_cluster()
    wf = new_workflow_id()
    try:
        c.bind_workflow(wf, adopt=False, detached=True)
        job_opts["name_prefix"] = "wfdriver"
        job_opts["keep_results"] = True  # the driver's result must outlive it until fetched
        fut = c.submit(func, *args, **job_opts)
        # Window: between the Job's creation above and this patch the anchor records no
        # driver ("sk8s/driver-job" == ""); an attach in that gap sees no driver yet.
        anchors.set_annotation(
            c.core, namespace=_ns(c), workflow_id=wf, key=anchors.DRIVER_JOB, value=fut.job_name
        )
    except BaseException:
        # Nothing records or owns a half-submitted workflow: don't leak a detached anchor.
        if c.workflow_id == wf:  # bound: something may have been created
            anchors.delete_anchor(c.core, c.store, namespace=_ns(c), workflow_id=wf)
        if owned:
            c.close(delete_workflow=False)
        raise
    return WorkflowHandle(wf, c, driver_future=fut, owns_cluster=owned)


def attach(workflow_id: str, cluster: Any | None = None) -> WorkflowHandle:
    """Reconnect to a workflow submitted earlier (possibly by another process).

    Raises ``Sk8sError("unknown workflow ...")`` if it does not exist. Cluster rule:
    ``cluster=None`` builds a fresh ``Cluster`` from the ``configure()`` overrides
    (the handle owns it) and deliberately ignores ``configure(cluster=obj)``. A
    ``cluster`` you pass must be unused and becomes dedicated to this workflow
    (rebound only once the workflow is known to exist); it is not closed by the
    handle. A non-``Cluster`` argument raises ``TypeError``.
    """
    _check_cluster(cluster)
    owned = cluster is None
    c: Cluster = cluster if cluster is not None else _new_cluster(workflow_id=workflow_id)
    try:
        if anchors.get_anchor(c.core, namespace=_ns(c), workflow_id=workflow_id) is None:
            raise Sk8sError(f"unknown workflow {workflow_id}: no anchor in namespace {_ns(c)}")
        if not owned:
            c.bind_workflow(workflow_id, adopt=True)
    except BaseException:
        if owned:
            c.close(delete_workflow=False)
        raise
    return WorkflowHandle(workflow_id, c, owns_cluster=owned)


def _ns(cluster: Cluster) -> str:
    namespace = cluster.config.namespace
    assert namespace is not None
    return namespace


class WorkflowHandle:
    """A reference to a (possibly detached) workflow: poll, wait for, or kill it."""

    def __init__(
        self,
        workflow_id: str,
        cluster: Cluster,
        driver_future: JobFuture | None = None,
        owns_cluster: bool = False,
    ) -> None:
        self.id = workflow_id
        self._cluster = cluster
        self._fut = driver_future
        self._owns_cluster = owns_cluster
        self._killed = False
        self._deleted = False
        self._lock = threading.Lock()

    def _anchor(self, missing: str | None = None) -> Any:
        anchor = anchors.get_anchor(
            self._cluster.core, namespace=_ns(self._cluster), workflow_id=self.id
        )
        if anchor is None:
            raise Sk8sError(missing or f"unknown workflow {self.id} (deleted or never existed)")
        return anchor

    def _driver_name(self, missing: str | None = None) -> str:
        name: str = (self._anchor(missing).metadata.annotations or {}).get(anchors.DRIVER_JOB, "")
        return name

    def _jobs(self) -> list[Any]:
        listing = self._cluster.batch.list_namespaced_job(
            namespace=_ns(self._cluster), label_selector=f"{anchors.WORKFLOW_LABEL}={self.id}"
        )
        return list(listing.items)

    def status(self) -> dict[str, Any]:
        """Driver phase (active|succeeded|failed|unknown) and counts of all the
        workflow's jobs (the driver included) by phase."""
        driver = self._driver_name()
        counts = {"active": 0, "succeeded": 0, "failed": 0}
        driver_phase = "unknown"
        for job in self._jobs():
            phase = job_phase(job)
            counts[phase] = counts.get(phase, 0) + 1
            if driver and job.metadata.name == driver:
                driver_phase = phase
        return {"driver": driver_phase, "jobs": counts}

    def _killed_error(self) -> Sk8sError:
        return Sk8sError(f"workflow {self.id} was killed")

    def _driver_future(self) -> JobFuture:
        with self._lock:
            if self._fut is None:
                name = self._driver_name(f"workflow {self.id} was killed or expired")
                if not name:
                    raise Sk8sError(f"workflow {self.id} has no driver job recorded yet")
                self._fut = self._cluster.track_job(name, keep_results=True)
            return self._fut

    @staticmethod
    def _fail(fut: JobFuture, error: BaseException) -> None:
        try:
            fut.set_exception(error)
        except concurrent.futures.InvalidStateError:
            pass  # resolved first (result or kill): keep that outcome

    def _check_alive(self, fut: JobFuture) -> None:
        """Called while the driver is unresolved. The watcher only reports transitions it
        sees, so a driver that finished before we watched (or was reaped by the TTL
        controller) is resolved from the API/store directly; and a workflow deleted by
        another process (no local kill flag) is detected via its anchor."""
        name = fut.job_name
        if anchors.get_anchor(
            self._cluster.core, namespace=_ns(self._cluster), workflow_id=self.id
        ) is None:
            try:
                self._cluster.store.get(self._cluster.store.uri_for(f"{self.id}/{name}/result"))
            except BlobNotFound:
                self._cluster.untrack_job(name)
                err = Sk8sError(f"workflow {self.id} was killed or expired")
                self._fail(fut, err)
                raise err from None
            except Exception:  # noqa: BLE001 — transient store trouble: look again next round
                return
            self._cluster.settle_job(name, "succeeded")
            return
        job = next((j for j in self._jobs() if j.metadata.name == name), None)
        phase = "failed" if job is None else job_phase(job)  # reaped: result blob decides
        if phase in _TERMINAL:
            self._cluster.settle_job(name, phase)

    def result(self, timeout: float | None = None, *, delete: bool = True) -> Any:
        """Wait for the driver and return its value (re-raising its exception).

        On success with ``delete=True`` the whole workflow (jobs, blobs, anchor) is
        removed and an internally built cluster is closed; on failure it is kept for
        inspection. Repeatable while the workflow exists. Raises ``Sk8sError`` if the
        workflow was killed (here or by another process) or expired meanwhile.
        """
        if self._killed:
            raise self._killed_error()
        fut = self._driver_future()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if not fut.done():
                self._check_alive(fut)
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            wait = _POLL if remaining is None else min(_POLL, remaining)
            try:
                value = fut.result(timeout=wait)
                break
            except concurrent.futures.TimeoutError:
                if fut.done():
                    raise  # the driver itself raised a TimeoutError
                if remaining is not None and remaining <= wait:
                    raise JobTimeout(fut.job_name, timeout or 0.0) from None
        if delete and not self._deleted:
            self._delete_workflow()
        return value

    def _delete_workflow(self) -> None:
        self._deleted = True
        anchors.delete_anchor(
            self._cluster.core, self._cluster.store,
            namespace=_ns(self._cluster), workflow_id=self.id,
        )
        self.close()

    def kill(self) -> None:
        """Delete the workflow: the anchor cascades to every job and blob. A concurrent
        or later ``result()`` on this handle raises ``Sk8sError`` instead of hanging."""
        self._killed = True
        with self._lock:
            fut = self._fut
        if fut is not None:
            self._cluster.untrack_job(fut.job_name)
            self._fail(fut, self._killed_error())
        self._delete_workflow()

    def close(self) -> None:
        """Release the cluster (watcher thread) this handle built, without deleting the
        workflow. A cluster you passed in is yours to close and is left alone.
        ``result(delete=True)`` and ``kill()`` call this for you."""
        if self._owns_cluster:
            self._cluster.close(delete_workflow=False)
