"""GCS store against a real bucket (client-side) plus the no-leftovers check through a job.

Runs only when ``SK8S_STORE`` is a ``gs://bucket/prefix/`` URL the caller's ADC can write.
Every test works under its own random key segment of that prefix and removes it afterwards.
The fakes-tested backend's semantics (BlobNotFound mapping, idempotent delete, whole-segment
``delete_prefix``) are what this verifies against the real service.
"""
from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator

import pytest

from sk8s.errors import BlobNotFound
from sk8s.store.gcs import GCSStore
from tests.integration.conftest import requires_cluster

STORE_URL = os.environ.get("SK8S_STORE", "")

pytestmark = [
    pytest.mark.integration,
    requires_cluster,
    pytest.mark.skipif(not STORE_URL.startswith("gs://"), reason="SK8S_STORE is not a gs:// URL"),
    pytest.mark.timeout(300),
]


@pytest.fixture
def store() -> Iterator[GCSStore]:
    s = GCSStore.from_url(STORE_URL)
    seg = f"it-store-{secrets.token_hex(4)}"
    s.test_seg = seg  # type: ignore[attr-defined]
    yield s
    s.delete_prefix(seg)


def _seg(s: GCSStore) -> str:
    return str(s.test_seg)  # type: ignore[attr-defined]


def _names(s: GCSStore, seg: str) -> list[str]:
    return sorted(b.name for b in s._client.list_blobs(s.bucket, prefix=s._full_key(seg)))


def test_put_get_roundtrip_and_uri(store: GCSStore) -> None:
    seg = _seg(store)
    uri = store.put(f"{seg}/a", b"payload")
    assert uri == store.uri_for(f"{seg}/a") and uri.startswith("gs://")
    assert store.get(uri) == b"payload"


def test_get_missing_is_blobnotfound(store: GCSStore) -> None:
    with pytest.raises(BlobNotFound):
        store.get(store.uri_for(f"{_seg(store)}/nope"))


def test_delete_is_idempotent(store: GCSStore) -> None:
    uri = store.put(f"{_seg(store)}/x", b"1")
    store.delete(uri)
    store.delete(uri)  # already gone: no error
    with pytest.raises(BlobNotFound):
        store.get(uri)


def test_delete_prefix_is_whole_segment(store: GCSStore) -> None:
    seg = _seg(store)
    store.put(f"{seg}/wf1/a", b"a")
    store.put(f"{seg}/wf1/deep/b", b"b")
    keep = store.put(f"{seg}/wf10/c", b"c")  # shares the string prefix "wf1" but not the segment
    store.delete_prefix(f"{seg}/wf1")
    assert [n.rsplit("/", 2)[-2] for n in _names(store, seg)] == ["wf10"]
    assert store.get(keep) == b"c"


def test_delete_prefix_rejects_empty(store: GCSStore) -> None:
    with pytest.raises(ValueError):
        store.delete_prefix("/")


def test_job_leaves_no_gcs_objects(cluster) -> None:  # type: ignore[no-untyped-def]
    s = GCSStore.from_url(cluster.config.store)
    cluster.run(lambda: "done")
    wf = cluster.workflow_id
    # Blob deletion completes shortly *after* result delivery, so poll.
    deadline = time.monotonic() + 10
    names: list[str] = []
    while time.monotonic() < deadline:
        names = _names(s, wf)
        if not names:
            break
        time.sleep(0.25)
    assert names == []
