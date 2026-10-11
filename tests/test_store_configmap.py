import pytest

from sk8s.errors import BlobNotFound, BlobTooLarge
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
