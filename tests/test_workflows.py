import queue
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from kubernetes import client
from kubernetes.client import ApiException

import sk8s
from sk8s import workflows
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import JobTimeout, Sk8sError
from sk8s.watch import JobEvent
from tests.test_cluster import Harness, _OPEN, _close_clusters  # noqa: F401  (autouse fixture)

NS = "ns1"


def _anchor(h: Harness, wf: str) -> Any:
    return h.core.configmaps[(NS, f"sk8s-anchor-{wf}")]


def double(x: int) -> int:
    return x * 2


def boom() -> None:
    raise ValueError("driver boom")


class Cold:
    """A second Cluster (own watcher/event queue) over the SAME fakes: a fresh process."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.events: "queue.Queue[JobEvent]" = queue.Queue()
        self.cluster = Cluster(
            Config(namespace=NS, image="sk8s:dev"), batch_api=h.batch, core_api=h.core,
            store=h.store, watcher_events=self._iter,
        )
        _OPEN.append(self.cluster)

    def _iter(self) -> Iterator[JobEvent]:
        while True:
            yield self.events.get()


def test_submit_workflow_creates_detached_anchor_and_driver(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 21, cluster=h.cluster)
    wf = handle.id
    ann = _anchor(h, wf).metadata.annotations
    assert ann["sk8s/detached"] == "true"
    (driver,) = [n for (_ns, n) in h.batch.jobs]
    assert driver.startswith("wfdriver-")
    assert ann["sk8s/driver-job"] == driver
    job = h.batch.jobs[(NS, driver)]
    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    assert env["SK8S_WORKFLOW_ID"] == wf
    assert job.metadata.owner_references[0].uid == _anchor(h, wf).metadata.uid


def test_submit_workflow_keeps_driver_result_and_does_not_auto_delete(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 21, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    h.finish_job(driver)
    handle.result(timeout=10, delete=False)
    assert h.store.get(h.store.uri_for(f"{handle.id}/{driver}/result"))
    h.cluster.close()
    assert (NS, f"sk8s-anchor-{handle.id}") in h.core.configmaps


def test_submit_workflow_fresh_id_per_call(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h1, h2 = Harness(tmp_path / "a"), Harness(tmp_path / "b")
    assert workflows.submit_workflow(double, 1, cluster=h1.cluster).id != (
        workflows.submit_workflow(double, 1, cluster=h2.cluster).id
    )


def test_submit_workflow_splits_job_options_from_kwargs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(lambda x, scale=1: x * scale, 4, scale=3,
                                       image="custom:1", cluster=h.cluster)
    (job,) = h.batch.jobs.values()
    assert job.spec.template.spec.containers[0].image == "custom:1"
    h.finish_job(job.metadata.name)
    assert handle.result(timeout=10) == 12


def test_attach_finds_workflow_on_cold_cluster(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 21, cluster=h.cluster)
    cold = Cold(h)
    attached = workflows.attach(handle.id, cluster=cold.cluster)
    assert attached.id == handle.id
    assert cold.cluster.workflow_id == handle.id
    assert len(h.core.configmaps) == 1  # adopted, not created


def test_attach_unknown_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    with pytest.raises(Sk8sError, match="unknown workflow wf-nope"):
        workflows.attach("wf-nope", cluster=h.cluster)
    assert not h.core.configmaps


def test_result_returns_value_and_deletes_anchor(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 21, cluster=h.cluster)
    cold = Cold(h)
    attached = workflows.attach(handle.id, cluster=cold.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    h.finish_job(driver, phase="succeeded")
    # h.cluster's watcher got no event of its own to resolve; the attached cluster does:
    cold.events.put((driver, "succeeded"))
    assert attached.result(timeout=10) == 42
    assert not h.core.configmaps
    assert not (h.store.root / handle.id).exists()


def test_result_delete_false_keeps_workflow(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 5, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    h.finish_job(driver)
    assert handle.result(timeout=10, delete=False) == 10
    assert (NS, f"sk8s-anchor-{handle.id}") in h.core.configmaps
    assert handle.result(timeout=10, delete=False) == 10  # repeatable
    assert handle.result(timeout=10) == 10  # and then delete
    assert not h.core.configmaps


def test_result_reraises_driver_exception_and_keeps_workflow(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(boom, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    h.finish_job(driver, phase="failed")
    with pytest.raises(ValueError, match="driver boom"):
        handle.result(timeout=10)
    assert (NS, f"sk8s-anchor-{handle.id}") in h.core.configmaps  # kept for inspection


def test_result_timeout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    with pytest.raises(JobTimeout):
        handle.result(timeout=0.2)
    assert (NS, f"sk8s-anchor-{handle.id}") in h.core.configmaps


def test_attach_result_when_driver_job_already_ttl_deleted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 21, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    h.finish_job(driver)
    del h.batch.jobs[(NS, driver)]  # TTL controller reaped the finished Job
    cold = Cold(h)  # no event will ever arrive
    assert workflows.attach(handle.id, cluster=cold.cluster).result(timeout=10) == 42
    assert not h.core.configmaps


def test_attach_result_when_driver_already_succeeded_without_event(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 3, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    h.finish_job(driver)
    h.batch.jobs[(NS, driver)].status = client.V1JobStatus(succeeded=1)
    cold = Cold(h)
    assert workflows.attach(handle.id, cluster=cold.cluster).result(timeout=10) == 6


def test_result_driver_gone_without_result_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.0)
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 3, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    del h.batch.jobs[(NS, driver)]
    cold = Cold(h)
    with pytest.raises(Sk8sError):
        workflows.attach(handle.id, cluster=cold.cluster).result(timeout=10)
    assert (NS, f"sk8s-anchor-{handle.id}") in h.core.configmaps  # failure keeps the workflow


def test_kill_deletes_anchor_and_blobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    wf = handle.id
    assert (h.store.root / wf).exists()
    cold = Cold(h)
    workflows.attach(wf, cluster=cold.cluster).kill()
    assert not h.core.configmaps
    assert not (h.store.root / wf).exists()


def test_status_counts_phases(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    assert handle.status() == {"driver": "active", "jobs": {"active": 1, "succeeded": 0, "failed": 0}}

    def child(name: str, wf: str, status: Any) -> None:
        h.batch.jobs[(NS, name)] = SimpleNamespace(
            metadata=SimpleNamespace(name=name, labels={"sk8s/workflow-id": wf}),
            spec=SimpleNamespace(backoff_limit=0), status=status,
        )

    child("c1", handle.id, client.V1JobStatus(succeeded=1))
    child("c2", handle.id, client.V1JobStatus(failed=1))
    child("c3", handle.id, None)
    child("other", "wf-other", client.V1JobStatus(succeeded=1))
    h.batch.jobs[(NS, driver)].status = client.V1JobStatus(succeeded=1)
    assert handle.status() == {
        "driver": "succeeded", "jobs": {"active": 1, "succeeded": 2, "failed": 1},
    }


def test_status_driver_unknown_when_job_gone(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    del h.batch.jobs[(NS, driver)]
    assert handle.status() == {"driver": "unknown", "jobs": {"active": 0, "succeeded": 0, "failed": 0}}


def test_submit_workflow_default_cluster_is_fresh_and_detached(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sk8s.workflows as w

    h = Harness(tmp_path)
    made: list[dict[str, Any]] = []

    def fake_new_cluster(**extra: Any) -> Cluster:
        made.append(extra)
        return h.cluster

    monkeypatch.setattr(w, "_new_cluster", fake_new_cluster)
    handle = w.submit_workflow(double, 1)
    assert made == [{}]
    assert _anchor(h, handle.id).metadata.annotations["sk8s/detached"] == "true"


def test_cluster_rebinding_requires_unused_cluster(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)
    with pytest.raises(RuntimeError):
        workflows.submit_workflow(double, 1, cluster=h.cluster)


def test_cluster_workflow_id_kwarg_adopts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    c = Cluster(Config(namespace=NS, image="x"), batch_api=h.batch, core_api=h.core,
                store=h.store, watcher_events=lambda: iter(()), workflow_id="wf-given")
    _OPEN.append(c)
    assert c.workflow_id == "wf-given"
    c.submit(lambda: 1)
    assert _anchor(h, "wf-given").metadata.annotations["sk8s/detached"] == "false"  # created, not detached
    c.close()
    assert (NS, "sk8s-anchor-wf-given") in h.core.configmaps  # adopted: never deleted by close


def test_exports() -> None:
    assert sk8s.submit_workflow is workflows.submit_workflow
    assert sk8s.attach is workflows.attach
    assert sk8s.WorkflowHandle is workflows.WorkflowHandle


def test_result_after_kill_raises_promptly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    handle.kill()
    with pytest.raises(Sk8sError, match="was killed"):
        handle.result()  # no timeout: must not hang


def test_kill_wakes_a_blocked_result(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import threading

    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    errors: list[BaseException] = []

    def waiter() -> None:
        try:
            handle.result()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=waiter)
    t.start()
    t.join(0.3)
    assert t.is_alive()
    handle.kill()
    t.join(5)
    assert not t.is_alive()
    assert len(errors) == 1 and "was killed" in str(errors[0])


def test_attach_then_killed_elsewhere_result_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(workflows, "_POLL", 0.05)
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    cold = Cold(h)
    attached = workflows.attach(handle.id, cluster=cold.cluster)
    import threading

    threading.Timer(0.2, handle.kill).start()  # another handle/process kills it mid-wait
    with pytest.raises(Sk8sError, match="killed or expired"):
        attached.result(timeout=10)


def test_result_on_fresh_attach_after_kill_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 1, cluster=h.cluster)
    attached = workflows.attach(handle.id, cluster=Cold(h).cluster)
    handle.kill()
    with pytest.raises(Sk8sError, match="killed or expired"):
        attached.result(timeout=10)


def test_attach_unknown_closes_internal_cluster(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    monkeypatch.setattr(workflows, "_new_cluster", lambda **extra: h.cluster)
    with pytest.raises(Sk8sError, match="unknown workflow"):
        workflows.attach("wf-nope")
    assert h.cluster._closed
    assert h.cluster._watcher._stopped.is_set()


def test_attach_unknown_leaves_passed_cluster_unbound_and_open(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    before = h.cluster.workflow_id
    with pytest.raises(Sk8sError):
        workflows.attach("wf-nope", cluster=h.cluster)
    assert h.cluster.workflow_id == before and not h.cluster._closed


def test_submit_failure_closes_internal_cluster_and_leaks_nothing(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    monkeypatch.setattr(workflows, "_new_cluster", lambda **extra: h.cluster)

    def fail(namespace: str, body: Any) -> None:
        raise RuntimeError("api down")

    h.batch.create_namespaced_job = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="api down"):
        workflows.submit_workflow(double, 1)
    assert h.cluster._closed
    assert not h.core.configmaps


def test_owned_cluster_closed_on_result_delete_and_kill(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    monkeypatch.setattr(workflows, "_new_cluster", lambda **extra: h.cluster)
    handle = workflows.submit_workflow(double, 2)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    h.finish_job(driver)
    assert handle.result(timeout=10, delete=False) == 4
    assert not h.cluster._closed  # kept: more result()/status() calls may follow
    handle.result(timeout=10)
    assert h.cluster._closed

    h2 = Harness(tmp_path / "b")
    monkeypatch.setattr(workflows, "_new_cluster", lambda **extra: h2.cluster)
    workflows.submit_workflow(double, 2).kill()
    assert h2.cluster._closed


def test_handle_close_keeps_workflow_and_spares_passed_cluster(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    monkeypatch.setattr(workflows, "_new_cluster", lambda **extra: h.cluster)
    owned = workflows.submit_workflow(double, 1)
    owned.close()
    assert h.cluster._closed
    assert (NS, f"sk8s-anchor-{owned.id}") in h.core.configmaps

    h2 = Harness(tmp_path / "b")
    passed = workflows.submit_workflow(double, 1, cluster=h2.cluster)
    passed.close()
    assert not h2.cluster._closed


def test_non_cluster_argument_is_a_type_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError, match="Cluster"):
        workflows.submit_workflow(double, 1, cluster=object())
    with pytest.raises(TypeError, match="Cluster"):
        workflows.attach("wf-x", cluster="nope")


def test_check_alive_tolerates_transient_api_errors(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(workflows, "_POLL", 0.05)
    h = Harness(tmp_path)
    handle = workflows.submit_workflow(double, 4, cluster=h.cluster)
    driver = _anchor(h, handle.id).metadata.annotations["sk8s/driver-job"]
    real_read = h.core.read_namespaced_config_map
    real_list = h.batch.list_namespaced_job
    flaky = {"read": 1, "list": 1}

    def read(name: str, namespace: str) -> Any:
        if flaky["read"]:
            flaky["read"] -= 1
            raise ApiException(status=500, reason="Internal Server Error")
        return real_read(name=name, namespace=namespace)

    def list_jobs(*a: Any, **k: Any) -> Any:
        if flaky["list"]:
            flaky["list"] -= 1
            raise ApiException(status=503, reason="Unavailable")
        return real_list(*a, **k)

    monkeypatch.setattr(h.core, "read_namespaced_config_map", read)
    monkeypatch.setattr(h.batch, "list_namespaced_job", list_jobs)
    h.finish_job(driver)
    h.events.get_nowait()  # swallow the watch event: only _check_alive can settle the driver
    h.batch.jobs[(NS, driver)].status = client.V1JobStatus(succeeded=1)
    assert handle.result(timeout=10) == 8
    assert flaky == {"read": 0, "list": 0}  # both failures were hit and survived
