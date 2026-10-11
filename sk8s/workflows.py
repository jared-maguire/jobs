"""Detached workflows: submit a driver function, walk away, attach later."""
from __future__ import annotations

import concurrent.futures
import threading
from typing import Any, Callable

from sk8s import anchors
from sk8s.api import _new_cluster
from sk8s.cluster import Cluster, job_phase, new_workflow_id
from sk8s.errors import JobTimeout, Sk8sError
from sk8s.future import JobFuture

_TERMINAL = ("succeeded", "failed")


def submit_workflow(
    func: Callable[..., Any], /, *args: Any, cluster: Any | None = None, **job_opts: Any
) -> WorkflowHandle:
    """Run ``func`` as the driver of a new, detached workflow and return immediately.

    The driver's own submissions are owned by the workflow's anchor, which this
    process never deletes. ``cluster`` (if given) must be unused: it becomes
    dedicated to the new workflow. Keyword arguments are split as in ``submit``.
    """
    c: Cluster = cluster if cluster is not None else _new_cluster()
    wf = new_workflow_id()
    c.bind_workflow(wf, adopt=False, detached=True)
    job_opts["name_prefix"] = "wfdriver"
    job_opts["keep_results"] = True  # the driver's result must outlive it until fetched
    try:
        fut = c.submit(func, *args, **job_opts)
        anchors.set_annotation(
            c.core, namespace=_ns(c), workflow_id=wf, key=anchors.DRIVER_JOB, value=fut.job_name
        )
    except BaseException:
        # Nothing records or owns a half-submitted workflow: don't leak a detached anchor.
        anchors.delete_anchor(c.core, c.store, namespace=_ns(c), workflow_id=wf)
        raise
    return WorkflowHandle(wf, c, driver_future=fut)


def attach(workflow_id: str, cluster: Any | None = None) -> WorkflowHandle:
    """Reconnect to a workflow submitted earlier (possibly by another process)."""
    c: Cluster
    if cluster is None:
        c = _new_cluster(workflow_id=workflow_id)
    else:
        c = cluster
        c.bind_workflow(workflow_id, adopt=True)
    if anchors.get_anchor(c.core, namespace=_ns(c), workflow_id=workflow_id) is None:
        raise Sk8sError(f"unknown workflow {workflow_id}: no anchor in namespace {_ns(c)}")
    return WorkflowHandle(workflow_id, c)


def _ns(cluster: Cluster) -> str:
    namespace = cluster.config.namespace
    assert namespace is not None
    return namespace


class WorkflowHandle:
    """A reference to a (possibly detached) workflow: poll, wait for, or kill it."""

    def __init__(
        self, workflow_id: str, cluster: Cluster, driver_future: JobFuture | None = None
    ) -> None:
        self.id = workflow_id
        self._cluster = cluster
        self._fut = driver_future
        self._lock = threading.Lock()

    def _anchor(self) -> Any:
        anchor = anchors.get_anchor(
            self._cluster.core, namespace=_ns(self._cluster), workflow_id=self.id
        )
        if anchor is None:
            raise Sk8sError(f"unknown workflow {self.id} (deleted or never existed)")
        return anchor

    def _driver_name(self) -> str:
        name: str = (self._anchor().metadata.annotations or {}).get(anchors.DRIVER_JOB, "")
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

    def _driver_future(self) -> JobFuture:
        with self._lock:
            if self._fut is None:
                name = self._driver_name()
                if not name:
                    raise Sk8sError(f"workflow {self.id!r} has no driver job recorded yet")
                self._fut = self._cluster.track_job(name, keep_results=True)
            fut = self._fut
        if not fut.done():
            self._settle_if_finished(fut.job_name)
        return fut

    def _settle_if_finished(self, name: str) -> None:
        """The watcher only reports transitions it sees, so a driver that finished
        before we started watching (or was already reaped by the TTL controller)
        is resolved from the API/store directly."""
        job = next((j for j in self._jobs() if j.metadata.name == name), None)
        phase = "failed" if job is None else job_phase(job)  # reaped: result blob decides
        if phase in _TERMINAL:
            self._cluster.settle_job(name, phase)

    def result(self, timeout: float | None = None, *, delete: bool = True) -> Any:
        """Wait for the driver and return its value (re-raising its exception).

        On success with ``delete=True`` the whole workflow (jobs, blobs, anchor) is
        removed; on failure it is kept for inspection. Repeatable while it exists.
        """
        fut = self._driver_future()
        try:
            value = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            if fut.done():
                raise  # the driver itself raised a TimeoutError
            raise JobTimeout(fut.job_name, timeout or 0.0) from None
        if delete:
            self.kill()
        return value

    def kill(self) -> None:
        """Delete the workflow: the anchor cascades to every job and blob."""
        anchors.delete_anchor(
            self._cluster.core, self._cluster.store,
            namespace=_ns(self._cluster), workflow_id=self.id,
        )
