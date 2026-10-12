import datetime
from typing import Any

import pytest
from kubernetes import client

from sk8s import anchors
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.store.configmap import ConfigMapStore
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1
from tests.test_cluster import Harness, _close_clusters  # noqa: F401  (autouse fixture)

NS = "ns1"


def _anchor(core: FakeCoreV1, wf: str) -> Any:
    return core.configmaps[(NS, f"sk8s-anchor-{wf}")]


def _create(core: FakeCoreV1, wf: str = "wf-a", **over: Any) -> Any:
    kwargs: dict[str, Any] = dict(
        namespace=NS, workflow_id=wf, store="local:/tmp/x", store_prefix=wf,
        retention_days=7, detached=False,
    )
    kwargs.update(over)
    return anchors.create_anchor(core, **kwargs)


# --- module-level helpers ------------------------------------------------------


def test_anchor_name() -> None:
    assert anchors.anchor_name("wf-abc") == "sk8s-anchor-wf-abc"


def test_create_anchor_shape() -> None:
    core = FakeCoreV1()
    cm = _create(core, "wf-a", store="s3://bkt/p", store_prefix="wf-a", retention_days=3, detached=True)
    assert isinstance(cm, client.V1ConfigMap)
    assert cm.metadata.name == "sk8s-anchor-wf-a"
    assert cm.metadata.namespace == NS
    assert cm.metadata.labels == {
        "app.kubernetes.io/managed-by": "sk8s",
        "sk8s/workflow-id": "wf-a",
        "sk8s/kind": "anchor",
    }
    ann = cm.metadata.annotations
    assert set(ann) == {
        "sk8s/driver-job", "sk8s/detached", "sk8s/created-at", "sk8s/retention-days",
        "sk8s/store", "sk8s/store-prefix",
    }
    assert ann["sk8s/driver-job"] == ""
    assert ann["sk8s/detached"] == "true"
    assert ann["sk8s/retention-days"] == "3"
    assert ann["sk8s/store"] == "s3://bkt/p"
    assert ann["sk8s/store-prefix"] == "wf-a"
    created = datetime.datetime.fromisoformat(ann["sk8s/created-at"])
    assert created.utcoffset() == datetime.timedelta(0)
    assert cm.metadata.uid  # the fake plays the API server


def test_create_anchor_409_reads_existing() -> None:
    core = FakeCoreV1()
    first = _create(core)
    second = _create(core)
    assert second.metadata.uid == first.metadata.uid
    assert len(core.configmaps) == 1


def test_get_anchor_missing_is_none() -> None:
    core = FakeCoreV1()
    assert anchors.get_anchor(core, namespace=NS, workflow_id="nope") is None
    _create(core)
    got = anchors.get_anchor(core, namespace=NS, workflow_id="wf-a")
    assert got is not None and got.metadata.name == "sk8s-anchor-wf-a"


def test_owner_ref() -> None:
    core = FakeCoreV1()
    cm = _create(core)
    ref = anchors.owner_ref(cm)
    assert isinstance(ref, client.V1OwnerReference)
    assert (ref.kind, ref.api_version, ref.name, ref.uid) == (
        "ConfigMap", "v1", "sk8s-anchor-wf-a", cm.metadata.uid,
    )
    assert ref.controller is True


def test_set_annotation() -> None:
    core = FakeCoreV1()
    _create(core)
    anchors.set_annotation(core, namespace=NS, workflow_id="wf-a", key="sk8s/driver-job", value="drv-1")
    assert _anchor(core, "wf-a").metadata.annotations["sk8s/driver-job"] == "drv-1"
    assert _anchor(core, "wf-a").metadata.annotations["sk8s/detached"] == "false"  # untouched


def test_delete_anchor_deletes_cm_and_store_prefix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    store = LocalStore(tmp_path)
    _create(core, "wf-a", store_prefix="wf-a")
    store.put("wf-a/job-1/payload", b"x")
    store.put("wf-ab/job-1/payload", b"keep")
    anchors.delete_anchor(core, store, namespace=NS, workflow_id="wf-a")
    assert (NS, "sk8s-anchor-wf-a") not in core.configmaps
    assert core.delete_policies["sk8s-anchor-wf-a"] == "Background"
    assert not (tmp_path / "wf-a").exists()
    assert (tmp_path / "wf-ab" / "job-1" / "payload").exists()


def test_delete_anchor_uses_recorded_prefix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    calls: list[str] = []

    class Spy(LocalStore):
        def delete_prefix(self, prefix: str) -> None:
            calls.append(prefix)

    _create(core, "wf-a", store_prefix="custom/prefix")
    anchors.delete_anchor(core, Spy(tmp_path), namespace=NS, workflow_id="wf-a")
    assert calls == ["custom/prefix", "workflows/wf-a"]  # blobs, then the artifact reference set


def test_delete_anchor_best_effort(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()

    class Broken(LocalStore):
        def delete_prefix(self, prefix: str) -> None:
            raise OSError("store down")

    _create(core, "wf-a")
    anchors.delete_anchor(core, Broken(tmp_path), namespace=NS, workflow_id="wf-a")  # no raise
    assert (NS, "sk8s-anchor-wf-a") not in core.configmaps
    # a missing anchor still tries the store and never raises
    anchors.delete_anchor(core, Broken(tmp_path), namespace=NS, workflow_id="gone")


# --- Cluster wiring ------------------------------------------------------------


def test_anchor_created_lazily_on_first_submit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    assert h.core.create_calls == []  # constructing and closing clusters creates nothing
    fut = h.cluster.submit(lambda: 1)
    wf = h.cluster.workflow_id
    cm = _anchor(h.core, wf)
    assert cm.metadata.labels["sk8s/kind"] == "anchor"
    assert cm.metadata.labels["sk8s/workflow-id"] == wf
    ann = cm.metadata.annotations
    assert ann["sk8s/store"] == h.cluster.config.store
    assert ann["sk8s/store-prefix"] == wf
    assert ann["sk8s/detached"] == "false"
    assert ann["sk8s/retention-days"] == str(h.cluster.config.retention_days)
    assert ann["sk8s/driver-job"] == ""
    job = h.batch.jobs[(NS, fut.job_name)]
    assert [r.uid for r in job.metadata.owner_references] == [cm.metadata.uid]


def test_second_submit_does_not_recreate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)
    h.cluster.submit(lambda: 2)
    assert h.core.create_calls == [f"sk8s-anchor-{h.cluster.workflow_id}"]


def test_second_cluster_with_same_workflow_adopts_on_409(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)
    monkeypatch.setenv("SK8S_WORKFLOW_ID", h.cluster.workflow_id)
    other = Cluster(
        Config(namespace=NS, image="x"), batch_api=h.batch, core_api=h.core,
        store=h.store, watcher_events=h._event_iter,
    )
    fut = other.submit(lambda: 1)
    assert len(h.core.configmaps) == 1
    job = h.batch.jobs[(NS, fut.job_name)]
    assert job.metadata.owner_references[0].uid == _anchor(h.core, h.cluster.workflow_id).metadata.uid
    other.close(delete_workflow=False)


def test_payload_configmaps_carry_owner_ref(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, batch = FakeCoreV1(), FakeBatchV1()
    cluster = Cluster(
        Config(namespace=NS, image="x"), batch_api=batch, core_api=core,
        store=ConfigMapStore(NS, core), watcher_events=lambda: iter(()),
    )
    try:
        fut = cluster.submit(lambda: 1)
        uid = _anchor(core, cluster.workflow_id).metadata.uid
        payloads = [
            cm for cm in core.configmaps.values()
            if (cm.metadata.annotations or {}).get("sk8s/key", "").endswith("/payload")
        ]
        assert len(payloads) == 1
        assert [r.uid for r in payloads[0].metadata.owner_references] == [uid]
        assert [r.uid for r in batch.jobs[(NS, fut.job_name)].metadata.owner_references] == [uid]
    finally:
        cluster.close(delete_workflow=False)


def test_in_pod_adopts_existing_anchor(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    existing = _create(core, "wf-driver")
    core.create_calls.clear()
    monkeypatch.setenv("SK8S_WORKFLOW_ID", "wf-driver")
    h = Harness(tmp_path, core=core)
    fut = h.cluster.submit(lambda: 1)
    assert core.create_calls == []  # adopted, never created
    assert len(core.configmaps) == 1
    job = h.batch.jobs[(NS, fut.job_name)]
    assert [r.uid for r in job.metadata.owner_references] == [existing.metadata.uid]


def test_in_pod_adoption_never_deletes_driver_anchor(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    _create(core, "wf-driver")
    monkeypatch.setenv("SK8S_WORKFLOW_ID", "wf-driver")
    h = Harness(tmp_path, core=core)
    fut = h.cluster.submit(lambda: 1)
    h.finish_job(fut.job_name)
    fut.result(timeout=10)
    h.cluster.close()
    assert (NS, "sk8s-anchor-wf-driver") in core.configmaps  # the driver owns its lifetime


def test_in_pod_missing_anchor_is_created_not_detached(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SK8S_WORKFLOW_ID", "wf-lost")
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)
    assert _anchor(h.core, "wf-lost").metadata.annotations["sk8s/detached"] == "false"


def test_close_with_pending_future_does_not_delete(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)  # never finishes
    h.cluster.close()
    assert _anchor(h.core, h.cluster.workflow_id).metadata.name == (
        f"sk8s-anchor-{h.cluster.workflow_id}"
    )
    assert len(h.core.configmaps) == 1


def test_close_deletes_when_all_done(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    h.finish_job(fut.job_name)
    fut.result(timeout=10)
    wf = h.cluster.workflow_id
    h.store.put(f"{wf}/leftover", b"x")
    h.cluster.close()
    assert not h.core.configmaps
    assert not (h.store.root / wf).exists()


def test_close_force_delete_workflow_true(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)  # still pending
    h.cluster.close(delete_workflow=True)
    assert not h.core.configmaps


def test_close_delete_workflow_false_skips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    h.finish_job(fut.job_name)
    fut.result(timeout=10)
    h.cluster.close(delete_workflow=False)
    assert len(h.core.configmaps) == 1


def test_detached_anchor_never_auto_deletes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.detached = True
    fut = h.cluster.submit(lambda: 1)
    assert _anchor(h.core, h.cluster.workflow_id).metadata.annotations["sk8s/detached"] == "true"
    h.finish_job(fut.job_name)
    fut.result(timeout=10)
    h.cluster.close()
    assert len(h.core.configmaps) == 1
    # an explicit request still wins
    h.cluster.close(delete_workflow=True)
    assert not h.core.configmaps


def test_close_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    h.finish_job(fut.job_name)
    fut.result(timeout=10)
    h.cluster.close()
    h.cluster.close()
    h.cluster.close(delete_workflow=True)  # no second delete attempt, no raise
    assert not h.core.configmaps


def test_close_without_submit_touches_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.close()
    assert h.core.create_calls == [] and not h.core.delete_policies


def test_atexit_hook_closes_once_and_respects_pending(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import atexit

    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: registered.append(fn) or fn)
    monkeypatch.setattr(atexit, "unregister", lambda fn: None)
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)
    assert len(registered) == 1
    registered[0]()  # simulated interpreter exit with a pending future
    assert len(h.core.configmaps) == 1


def test_cancelled_future_counts_as_resolved(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    assert fut.cancel()
    h.cluster.close()
    assert not h.core.configmaps


def test_resolved_before_future_set_does_not_race(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # _futures is popped before the future resolves; close must key off the future.
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    h.cluster._futures.clear()
    assert not fut.done()
    h.cluster.close()
    assert len(h.core.configmaps) == 1


def test_keep_results_survive_default_close(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sk8s.worker as worker

    core, batch = FakeCoreV1(), FakeBatchV1()
    store = ConfigMapStore(NS, core)
    cluster = Cluster(
        Config(namespace=NS, image="x"), batch_api=batch, core_api=core, store=store,
        watcher_events=lambda: iter(()),
    )
    monkeypatch.setattr(worker, "get_store", lambda *a, **k: store)
    fut = cluster.submit(lambda: 7, keep_results=True)
    job = batch.jobs[(NS, fut.job_name)]
    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    assert worker.main(env) == 0
    cluster._on_terminal(fut.job_name, "succeeded")
    assert fut.result(timeout=5) == 7
    cluster.close()
    assert (NS, f"sk8s-anchor-{cluster.workflow_id}") in core.configmaps
    assert store.get(store.uri_for(f"{cluster.workflow_id}/{fut.job_name}/result"))
    cluster.close(delete_workflow=True)  # explicit override still works
    assert (NS, f"sk8s-anchor-{cluster.workflow_id}") not in core.configmaps


def test_worker_result_configmap_carries_anchor_owner(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sk8s.worker as worker

    core, batch = FakeCoreV1(), FakeBatchV1()
    store = ConfigMapStore(NS, core)
    cluster = Cluster(
        Config(namespace=NS, image="x"), batch_api=batch, core_api=core, store=store,
        watcher_events=lambda: iter(()),
    )
    try:
        monkeypatch.setattr(worker, "get_store", lambda *a, **k: store)
        fut = cluster.submit(lambda: 1)
        anchor = _anchor(core, cluster.workflow_id)
        job = batch.jobs[(NS, fut.job_name)]
        env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
        assert env["SK8S_ANCHOR_NAME"] == anchor.metadata.name
        assert env["SK8S_ANCHOR_UID"] == anchor.metadata.uid
        assert worker.main(env) == 0
        result_cm = core.configmaps[(NS, store.uri_for(f"{cluster.workflow_id}/{fut.job_name}/result").rsplit("/", 1)[1])]
        refs = result_cm.metadata.owner_references
        assert [(r.kind, r.uid, r.controller) for r in refs] == [("ConfigMap", anchor.metadata.uid, False)]
    finally:
        cluster.close(delete_workflow=False)


def test_worker_without_anchor_env_has_no_owner(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sk8s.worker as worker
    from sk8s import serialize

    core = FakeCoreV1()
    store = ConfigMapStore(NS, core)
    monkeypatch.setattr(worker, "get_store", lambda *a, **k: store)
    uri = store.put("wf-x/j/payload", serialize.encode_payload(lambda: 1, (), {}))
    assert worker.main({"SK8S_STORE": "k8s", "SK8S_PAYLOAD_URI": uri, "SK8S_RESULT_KEY": "wf-x/j/result"}) == 0
    assert core.configmaps[(NS, store.uri_for("wf-x/j/result").rsplit("/", 1)[1])].metadata.owner_references is None


def test_submit_after_workflow_deleted_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster.submit(lambda: 1)
    h.cluster.close(delete_workflow=True)
    with pytest.raises(RuntimeError, match="deleted"):
        h.cluster.submit(lambda: 2)
    assert not h.core.configmaps
