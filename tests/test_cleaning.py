import datetime
import logging
from typing import Any

import pytest
from kubernetes import client

import sk8s
import sk8s.worker as worker
from sk8s import anchors, serialize
from sk8s.cleaning import clean
from sk8s.store.local import LocalStore
from tests.fakes import FakeCoreV1

NS = "ns1"


def _make(
    core: FakeCoreV1, wf: str, *, age_days: float = 0.0, retention: int = 7, prefix: str | None = None,
) -> Any:
    cm = anchors.create_anchor(
        core, namespace=NS, workflow_id=wf, store="local:/x", store_prefix=prefix or wf,
        retention_days=retention, detached=False,
    )
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=age_days)
    cm.metadata.annotations[anchors.CREATED_AT] = created.isoformat()
    return cm


def _has(core: FakeCoreV1, wf: str) -> bool:
    return (NS, anchors.anchor_name(wf)) in core.configmaps


def _service_cm(core: FakeCoreV1, name: str = "sk8s-anchor-wf-svc", age_days: float = 100) -> Any:
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=age_days)
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=name, namespace=NS,
            labels={"sk8s/kind": "service", "sk8s/workflow-id": "wf-svc"},
            annotations={
                anchors.CREATED_AT: old.isoformat(), anchors.RETENTION_DAYS: "1",
                anchors.STORE_PREFIX: "wf-svc",
            },
        )
    )
    core.create_namespaced_config_map(namespace=NS, body=cm)
    return cm


def test_expired_anchor_deleted_with_store_prefix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _make(core, "wf-old", age_days=8, retention=7)
    store.put("wf-old/j/payload", b"x")
    store.put("wf-other/j/payload", b"keep")
    assert clean(NS, core_api=core, store=store) == ["wf-old"]
    assert not _has(core, "wf-old")
    assert core.delete_policies["sk8s-anchor-wf-old"] == "Background"
    assert not (tmp_path / "wf-old").exists()
    assert (tmp_path / "wf-other" / "j" / "payload").exists()


def test_fresh_anchor_survives(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _make(core, "wf-new", age_days=6.9, retention=7)
    store.put("wf-new/j/payload", b"x")
    assert clean(NS, core_api=core, store=store) == []
    assert _has(core, "wf-new")
    assert (tmp_path / "wf-new").exists()


def test_each_anchor_uses_its_own_retention(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _make(core, "wf-short", age_days=2, retention=1)
    _make(core, "wf-long", age_days=2, retention=30)
    assert clean(NS, core_api=core, store=store) == ["wf-short"]
    assert _has(core, "wf-long")


def test_older_than_days_overrides_retention(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _make(core, "wf-a", age_days=2, retention=30)  # retention alone would keep it
    _make(core, "wf-b", age_days=0.5, retention=1)
    _make(core, "wf-c", age_days=20, retention=1)  # retention alone would delete it
    assert sorted(clean(NS, older_than_days=1, core_api=core, store=store)) == ["wf-a", "wf-c"]
    assert _has(core, "wf-b")
    assert clean(NS, older_than_days=0, core_api=core, store=store) == ["wf-b"]
    assert not core.configmaps


def test_workflow_targets_exactly_one_unconditionally(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _make(core, "wf-fresh", age_days=0, retention=7)
    _make(core, "wf-expired", age_days=99, retention=1)
    store.put("wf-fresh/j/payload", b"x")
    assert clean(NS, workflow="wf-fresh", core_api=core, store=store) == ["wf-fresh"]
    assert not _has(core, "wf-fresh")
    assert not (tmp_path / "wf-fresh").exists()
    assert _has(core, "wf-expired")  # expired but not targeted
    assert clean(NS, workflow="wf-nope", core_api=core, store=store) == []


def test_service_kind_is_never_touched(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _service_cm(core)
    store.put("wf-svc/state", b"x")
    _make(core, "wf-old", age_days=50)
    assert clean(NS, older_than_days=0, core_api=core, store=store) == ["wf-old"]
    assert (NS, "sk8s-anchor-wf-svc") in core.configmaps
    assert (tmp_path / "wf-svc" / "state").exists()
    # even an explicit workflow= aimed at a service-kind object is refused
    assert clean(NS, workflow="wf-svc", core_api=core, store=store) == []
    assert (NS, "sk8s-anchor-wf-svc") in core.configmaps
    assert (tmp_path / "wf-svc" / "state").exists()


def test_service_kind_ignored_even_if_listing_ignores_selector(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Sloppy(FakeCoreV1):
        def list_namespaced_config_map(self, namespace: str, label_selector: str = "") -> Any:
            return super().list_namespaced_config_map(namespace)  # returns everything

    core, store = Sloppy(), LocalStore(tmp_path)
    _service_cm(core)
    assert clean(NS, core_api=core, store=store) == []
    assert (NS, "sk8s-anchor-wf-svc") in core.configmaps


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.pop(anchors.CREATED_AT),
        lambda a: a.update({anchors.CREATED_AT: "not-a-date"}),
        lambda a: a.pop(anchors.RETENTION_DAYS),
        lambda a: a.update({anchors.RETENTION_DAYS: "soon"}),
        lambda a: a.update({anchors.RETENTION_DAYS: "-1"}),
    ],
)
def test_malformed_annotations_skipped_and_logged(tmp_path, caplog, mutate) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    bad = _make(core, "wf-bad", age_days=99)
    mutate(bad.metadata.annotations)
    _make(core, "wf-ok", age_days=99, retention=1)
    with caplog.at_level(logging.WARNING, logger="sk8s.cleaning"):
        assert clean(NS, core_api=core, store=store) == ["wf-ok"]
    assert _has(core, "wf-bad")
    assert "wf-bad" in caplog.text


def test_annotations_none_is_skipped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    cm = _make(core, "wf-bare")
    cm.metadata.annotations = None
    assert clean(NS, core_api=core, store=store) == []
    assert _has(core, "wf-bare")


def test_naive_timestamp_is_taken_as_utc(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    cm = _make(core, "wf-n")
    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=9)
    cm.metadata.annotations[anchors.CREATED_AT] = stamp.replace(tzinfo=None).isoformat()
    assert clean(NS, core_api=core, store=store) == ["wf-n"]


def test_other_namespaces_untouched(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    cm = _make(core, "wf-x", age_days=99, retention=1)
    core.configmaps[("other", anchors.anchor_name("wf-x"))] = cm
    clean(NS, core_api=core, store=store)
    assert ("other", anchors.anchor_name("wf-x")) in core.configmaps


def test_exclude_skips_listed_workflows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _make(core, "wf-self", age_days=99, retention=1)
    _make(core, "wf-old", age_days=99, retention=1)
    assert clean(NS, core_api=core, store=store, exclude=["wf-self"]) == ["wf-old"]
    assert _has(core, "wf-self")


def test_namespace_defaults_to_current(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sk8s import k8s

    core, store = FakeCoreV1(), LocalStore(tmp_path)
    _make(core, "wf-old", age_days=99, retention=1)
    monkeypatch.setattr(k8s, "current_namespace", lambda: NS)
    assert clean(core_api=core, store=store) == ["wf-old"]


def test_exported() -> None:
    assert sk8s.clean is clean and "clean" in sk8s.__all__


# --- worker sweep --------------------------------------------------------------


def _worker_env(tmp_path, fn=lambda: 5, **extra: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    store = LocalStore(tmp_path)
    uri = store.put("wf-run/j/payload", serialize.encode_payload(fn, (), {}))
    return {
        "SK8S_STORE": f"local:{tmp_path}", "SK8S_NAMESPACE": NS, "SK8S_PAYLOAD_URI": uri,
        "SK8S_RESULT_KEY": "wf-run/j/result", "SK8S_WORKFLOW_ID": "wf-run", **extra,
    }


def test_worker_sweeps_before_running_function(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    monkeypatch.setattr(worker, "_core_api", lambda: core)
    _make(core, "wf-old", age_days=99, retention=1, prefix="wf-old")
    LocalStore(tmp_path).put("wf-old/j/payload", b"x")
    gone = str(tmp_path / "wf-old")

    def fn() -> bool:
        import os

        return not os.path.exists(gone)  # evaluated in the "pod", i.e. after the sweep

    assert worker.main(_worker_env(tmp_path, fn)) == 0
    assert not _has(core, "wf-old")
    store = LocalStore(tmp_path)
    assert serialize.decode_result(store.get(store.uri_for("wf-run/j/result"))) is True


def test_worker_sweep_never_deletes_own_workflow(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    monkeypatch.setattr(worker, "_core_api", lambda: core)
    _make(core, "wf-run", age_days=99, retention=1)
    assert worker.main(_worker_env(tmp_path)) == 0
    assert _has(core, "wf-run")


def test_worker_sweep_failure_does_not_affect_job(tmp_path, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    class Broken(FakeCoreV1):
        def list_namespaced_config_map(self, namespace: str, label_selector: str = "") -> Any:
            raise client.ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(worker, "_core_api", lambda: Broken())
    with caplog.at_level(logging.WARNING, logger="sk8s.worker"):
        assert worker.main(_worker_env(tmp_path)) == 0
    store = LocalStore(tmp_path)
    assert serialize.decode_result(store.get(store.uri_for("wf-run/j/result"))) == 5
    assert "sweep failed" in caplog.text


def test_worker_sweep_client_construction_failure_does_not_affect_job(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def no_cluster() -> Any:
        raise RuntimeError("no kubeconfig")

    monkeypatch.setattr(worker, "_core_api", no_cluster)
    assert worker.main(_worker_env(tmp_path)) == 0


def test_worker_sweep_disabled_by_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    monkeypatch.setattr(worker, "_core_api", lambda: core)
    _make(core, "wf-old", age_days=99, retention=1)
    assert worker.main(_worker_env(tmp_path, SK8S_SWEEP="0")) == 0
    assert _has(core, "wf-old")
    assert worker.main(_worker_env(tmp_path, SK8S_SWEEP="1")) == 0  # only "0" disables
    assert not _has(core, "wf-old")


def test_sweep_skipped_without_result_key(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    core = FakeCoreV1()
    monkeypatch.setattr(worker, "_core_api", lambda: core)
    _make(core, "wf-old", age_days=99, retention=1)
    worker._sweep({"SK8S_NAMESPACE": NS}, LocalStore(tmp_path))
    assert _has(core, "wf-old")
