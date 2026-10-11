import sys

import pytest
from kubernetes.client import V1OwnerReference

from sk8s.errors import BlobNotFound, BlobTooLarge
from sk8s.store import get_store
from sk8s.store.configmap import MAX_CONFIGMAP_BLOB, ConfigMapStore
from tests.fakes import FakeCoreV1


def make_store() -> ConfigMapStore:
    return ConfigMapStore(namespace="ns1", core_api=FakeCoreV1())


def test_roundtrip() -> None:
    s = make_store()
    uri = s.put("wf-a/job-b/payload", b"\x00\x01binary")
    assert uri == "k8s://ns1/sk8s-wf-a-job-b-payload"
    assert s.get(uri) == b"\x00\x01binary"


def test_labels_applied() -> None:
    s = make_store()
    s.put("wf-a/job-b/result", b"x")
    cm = s.core_api.configmaps[("ns1", "sk8s-wf-a-job-b-result")]
    assert cm.metadata.labels["app.kubernetes.io/managed-by"] == "sk8s"
    assert cm.metadata.labels["sk8s/workflow-id"] == "wf-a"


def test_put_too_large() -> None:
    s = make_store()
    with pytest.raises(BlobTooLarge, match="s3:// or gs://"):
        s.put("wf-a/big", b"x" * (MAX_CONFIGMAP_BLOB + 1))


def test_get_missing_raises() -> None:
    with pytest.raises(BlobNotFound):
        make_store().get("k8s://ns1/sk8s-nope")


def test_delete_prefix() -> None:
    s = make_store()
    s.put("wf-a/j1/r", b"1")
    s.put("wf-b/j1/r", b"2")
    s.delete_prefix("wf-a")
    assert ("ns1", "sk8s-wf-a-j1-r") not in s.core_api.configmaps
    assert ("ns1", "sk8s-wf-b-j1-r") in s.core_api.configmaps


def test_key_sanitization() -> None:
    s = make_store()
    uri = s.put("wf-a/Job_B/payload", b"x")
    assert uri == "k8s://ns1/sk8s-wf-a-job-b-payload"


def test_put_same_key_overwrites() -> None:
    s = make_store()
    s.put("wf-a/job-b/payload", b"first")
    uri = s.put("wf-a/job-b/payload", b"second")  # 409 on create, then replace
    assert s.get(uri) == b"second"


def test_delete_missing_is_noop() -> None:
    make_store().delete("k8s://ns1/sk8s-never-existed")  # 404 swallowed


def test_owner_reference_passed_through() -> None:
    s = make_store()
    owner = V1OwnerReference(api_version="batch/v1", kind="Job", name="j", uid="u-1")
    s.put("wf-a/job-b/payload", b"x", owner=owner)
    cm = s.core_api.configmaps[("ns1", "sk8s-wf-a-job-b-payload")]
    assert cm.metadata.owner_references == [owner]


def test_delete_prefix_matches_whole_segments() -> None:
    s = make_store()
    s.put("wf-a/j1/r", b"1")
    s.put("wf-a/j1/sub/deep", b"2")
    s.put("wf-a/j1-x/r", b"3")  # sanitized name starts with "sk8s-wf-a-j1-"
    s.put("wf-ab/j1/r", b"4")  # sanitized name starts with "sk8s-wf-a"
    s.put("wf-a1/j1/r", b"5")
    # multi-segment prefix: removes wf-a/j1 and below, never wf-a/j1-x
    s.delete_prefix("wf-a/j1")
    for gone in ("wf-a/j1/r", "wf-a/j1/sub/deep"):
        with pytest.raises(BlobNotFound):
            s.get(s.uri_for(gone))
    assert s.get(s.uri_for("wf-a/j1-x/r")) == b"3"
    # single-segment prefix: removes wf-a/..., never the wf-ab or wf-a1 workflows
    s.delete_prefix("wf-a")
    with pytest.raises(BlobNotFound):
        s.get(s.uri_for("wf-a/j1-x/r"))
    assert s.get(s.uri_for("wf-ab/j1/r")) == b"4"
    assert s.get(s.uri_for("wf-a1/j1/r")) == b"5"


def test_delete_prefix_rejects_empty() -> None:
    s = make_store()
    s.put("wf-a/j1/r", b"1")
    with pytest.raises(ValueError):
        s.delete_prefix("/")
    assert s.get(s.uri_for("wf-a/j1/r")) == b"1"


def test_factory_k8s_branch_with_injected_api(monkeypatch: pytest.MonkeyPatch) -> None:
    # sk8s.k8s must not be imported when namespace and core_api are both injected.
    monkeypatch.setitem(sys.modules, "sk8s.k8s", None)
    core = FakeCoreV1()
    s = get_store("k8s", namespace="ns1", core_api=core)
    assert isinstance(s, ConfigMapStore)
    assert s.namespace == "ns1" and s.core_api is core


def test_factory_object_stores_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        get_store("s3://bucket/prefix/", namespace="ns1", core_api=FakeCoreV1())
    with pytest.raises(NotImplementedError):
        get_store("gs://bucket/prefix/", namespace="ns1", core_api=FakeCoreV1())


def test_factory_unknown_store() -> None:
    with pytest.raises(ValueError, match="unknown store"):
        get_store("bogus", namespace="ns1", core_api=FakeCoreV1())


def test_service_key_gets_service_labels_and_no_workflow_id() -> None:
    s = make_store()
    s.put("svc/api/payload", b"x")
    cm = s.core_api.configmaps[("ns1", "sk8s-svc-api-payload")]
    assert cm.metadata.labels == {
        "app.kubernetes.io/managed-by": "sk8s",
        "sk8s/kind": "service",
        "sk8s/service": "api",
    }


def test_workflow_key_labels_unchanged() -> None:
    s = make_store()
    s.put("wf-a/job-b/payload", b"x")
    cm = s.core_api.configmaps[("ns1", "sk8s-wf-a-job-b-payload")]
    assert cm.metadata.labels == {
        "app.kubernetes.io/managed-by": "sk8s",
        "sk8s/workflow-id": "wf-a",
    }


def test_delete_prefix_for_service_keys() -> None:
    s = make_store()
    s.put("svc/api/payload", b"x")
    s.put("svc/api2/payload", b"x")
    s.put("wf-a/j/payload", b"x")
    s.delete_prefix("svc/api")
    assert set(s.core_api.configmaps) == {("ns1", "sk8s-svc-api2-payload"), ("ns1", "sk8s-wf-a-j-payload")}
    s.delete_prefix("svc")
    assert set(s.core_api.configmaps) == {("ns1", "sk8s-wf-a-j-payload")}
