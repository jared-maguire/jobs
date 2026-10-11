"""Workflow integration suite: nested jobs, detach/attach, kill + anchor cascades.

Needs the worker image built from THIS branch (the in-pod worker runs this branch's
``sk8s.map`` / sweep code), tagged ``sk8s-dev-workflows:latest``::

    minikube image build -t sk8s-dev-workflows:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_workflows.py -v

Override the tag with ``SK8S_WORKFLOW_TEST_IMAGE``. Every test uses default (7-day)
retention so the worker-side sweep never touches live workflows.
"""
import os
import time
from typing import Any, Callable

import pytest

from sk8s import anchors
from sk8s.cluster import Cluster
from sk8s.config import load_config
from tests.integration.conftest import requires_cluster

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

IMAGE = os.environ.get("SK8S_WORKFLOW_TEST_IMAGE", "sk8s-dev-workflows:latest")


def new_cluster() -> Cluster:
    return Cluster(load_config(image=IMAGE, pull_policy="Never", ttl_seconds_after_finished=30))


@pytest.fixture
def wf_cluster():  # type: ignore[no-untyped-def]
    c = new_cluster()
    yield c
    c.close()


def _selector(c: Cluster, wf: str) -> str:
    return f"{anchors.WORKFLOW_LABEL}={wf}"


def workflow_objects(c: Cluster, wf: str) -> dict[str, list[str]]:
    ns = c.config.namespace
    return {
        "configmaps": sorted(
            i.metadata.name
            for i in c.core.list_namespaced_config_map(ns, label_selector=_selector(c, wf)).items
        ),
        "jobs": sorted(
            i.metadata.name
            for i in c.batch.list_namespaced_job(ns, label_selector=_selector(c, wf)).items
        ),
    }


def wait_until(pred: Callable[[], Any], timeout: float, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    value = pred()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = pred()
    return value


def test_nested_jobs_submitting_jobs(wf_cluster) -> None:  # type: ignore[no-untyped-def]
    def driver() -> int:
        import sk8s

        return sum(sk8s.map(lambda x: x * x, range(3)))

    assert wf_cluster.run(driver) == 5


def test_detached_roundtrip(wf_cluster) -> None:  # type: ignore[no-untyped-def]
    from sk8s import attach, submit_workflow

    def pipeline() -> int:  # defined locally so cloudpickle ships it by value
        import sk8s

        return sum(sk8s.map(lambda x: x * x, range(3)))

    handle = submit_workflow(pipeline, cluster=wf_cluster)
    wf = handle.id
    try:
        assert workflow_objects(wf_cluster, wf)["configmaps"]  # anchor + payload exist

        # A brand new Cluster (as another process would build) reattaches by id.
        fresh = new_cluster()
        try:
            other = attach(wf, cluster=fresh)
            assert other.result(timeout=240) == 5
            ns = fresh.config.namespace
            assert wait_until(
                lambda: anchors.get_anchor(fresh.core, namespace=ns, workflow_id=wf) is None
                and workflow_objects(fresh, wf) == {"configmaps": [], "jobs": []},
                timeout=10,
            ), workflow_objects(fresh, wf)
        finally:
            fresh.close()
    finally:
        handle.kill()  # no-op if already deleted; never leaks a detached workflow on failure
        handle.close()


def test_kill_cascade(wf_cluster) -> None:  # type: ignore[no-untyped-def]
    from sk8s import submit_workflow

    def sleeper() -> None:
        import time

        time.sleep(300)

    handle = submit_workflow(sleeper, cluster=wf_cluster)
    wf = handle.id
    assert workflow_objects(wf_cluster, wf)["jobs"]
    handle.kill()
    assert wait_until(
        lambda: workflow_objects(wf_cluster, wf) == {"configmaps": [], "jobs": []}, timeout=20
    ), workflow_objects(wf_cluster, wf)


def test_anchor_deletion_cascades(wf_cluster) -> None:  # type: ignore[no-untyped-def]
    fut = wf_cluster.submit(lambda: "x", keep_results=True)
    assert fut.result(timeout=240) == "x"
    wf = wf_cluster.workflow_id
    ns = wf_cluster.config.namespace
    before = workflow_objects(wf_cluster, wf)
    assert before["jobs"]
    assert len(before["configmaps"]) > 1  # anchor + the job's blobs
    name = anchors.anchor_name(wf)
    assert name in before["configmaps"]

    wf_cluster.core.delete_namespaced_config_map(name=name, namespace=ns)

    assert wait_until(
        lambda: workflow_objects(wf_cluster, wf) == {"configmaps": [], "jobs": []}, timeout=30
    ), workflow_objects(wf_cluster, wf)
