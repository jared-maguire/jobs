from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sk8s.errors import BlobNotFound, Sk8sError
from sk8s.store import get_store
from sk8s.store.gcs import GCSStore
from tests.fakes_gcs import FakeGCSClient


@pytest.fixture
def fake() -> FakeGCSClient:
    return FakeGCSClient()


@pytest.fixture
def store(fake: FakeGCSClient) -> GCSStore:
    return GCSStore("bkt", "pre", client=fake)


def test_roundtrip(store: GCSStore, fake: FakeGCSClient) -> None:
    uri = store.put("wf/job/payload", b"hello")
    assert uri == "gs://bkt/pre/wf/job/payload"
    assert fake.objects[("bkt", "pre/wf/job/payload")] == b"hello"
    assert store.get(uri) == b"hello"


def test_owner_ignored(store: GCSStore) -> None:
    assert store.get(store.put("k", b"v", owner=object())) == b"v"


def test_no_prefix(fake: FakeGCSClient) -> None:
    s = GCSStore("bkt", client=fake)
    assert s.put("a/b", b"1") == "gs://bkt/a/b"


def test_uri_for_deterministic_no_io(store: GCSStore, fake: FakeGCSClient) -> None:
    assert store.uri_for("wf/x/result") == "gs://bkt/pre/wf/x/result"
    assert store.uri_for("wf/x/result") == store.uri_for("wf/x/result")
    assert fake.objects == {} and fake.list_calls == 0
    assert store.put("wf/x/result", b"r") == store.uri_for("wf/x/result")


def test_get_missing_raises_blobnotfound(store: GCSStore) -> None:
    with pytest.raises(BlobNotFound):
        store.get("gs://bkt/pre/nope")


def test_get_other_errors_propagate(store: GCSStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from google.api_core.exceptions import Forbidden

    def boom(self: object, **_: object) -> bytes:
        raise Forbidden("nope")

    monkeypatch.setattr("tests.fakes_gcs.FakeBlob.download_as_bytes", boom)
    with pytest.raises(Forbidden):
        store.get("gs://bkt/pre/k")


def test_get_wrong_bucket_raises_valueerror(store: GCSStore) -> None:
    with pytest.raises(ValueError):
        store.get("gs://other/pre/k")
    with pytest.raises(ValueError):
        store.get("s3://bkt/pre/k")


def test_delete_idempotent(store: GCSStore) -> None:
    uri = store.put("k", b"v")
    store.delete(uri)
    store.delete(uri)
    with pytest.raises(BlobNotFound):
        store.get(uri)


def test_delete_prefix_whole_segment_boundary(store: GCSStore, fake: FakeGCSClient) -> None:
    store.put("wf-a/j1/payload", b"1")
    store.put("wf-a/j1/result", b"2")
    store.put("wf-ab/j1/payload", b"3")
    store.put("wf-a1/j1/payload", b"4")
    other = GCSStore("bkt", "other", client=fake)
    other.put("wf-a/j1/payload", b"5")
    store.delete_prefix("wf-a")
    assert sorted(k for _, k in fake.objects) == [
        "other/wf-a/j1/payload",
        "pre/wf-a1/j1/payload",
        "pre/wf-ab/j1/payload",
    ]


def test_delete_prefix_slashes_ignored_and_exact_key(
    store: GCSStore, fake: FakeGCSClient
) -> None:
    store.put("wf-a", b"exact")
    store.put("wf-a/x", b"x")
    store.put("wf-ab", b"keep")
    store.delete_prefix("/wf-a/")
    assert sorted(k for _, k in fake.objects) == ["pre/wf-ab"]


def test_delete_prefix_empty_raises(store: GCSStore, fake: FakeGCSClient) -> None:
    store.put("k", b"v")
    for bad in ("", "/", "//"):
        with pytest.raises(ValueError):
            store.delete_prefix(bad)
    assert len(fake.objects) == 1


def test_delete_prefix_paginates(fake: FakeGCSClient) -> None:
    fake.page_size = 1000
    s = GCSStore("bkt", "pre", client=fake)
    for i in range(2500):
        fake.objects[("bkt", f"pre/wf-a/j{i:05d}/payload")] = b"x"
    fake.objects[("bkt", "pre/wf-ab/keep")] = b"k"
    s.delete_prefix("wf-a")
    assert list(fake.objects) == [("bkt", "pre/wf-ab/keep")]
    assert fake.list_calls == 3
    assert fake.delete_calls == 2500


def test_delete_prefix_noop_when_nothing_matches(store: GCSStore, fake: FakeGCSClient) -> None:
    store.delete_prefix("absent")
    assert fake.delete_calls == 0


def test_from_url() -> None:
    fake = FakeGCSClient()
    a = GCSStore.from_url("gs://bkt", client=fake)
    assert a.uri_for("k") == "gs://bkt/k"
    b = GCSStore.from_url("gs://bkt/some/pre/", client=fake)
    assert b.uri_for("k") == "gs://bkt/some/pre/k"
    with pytest.raises(ValueError):
        GCSStore.from_url("s3://bkt")
    with pytest.raises(ValueError):
        GCSStore.from_url("gs:///x")


def test_missing_gcs_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)  # import raises ImportError
    with pytest.raises(Sk8sError) as ei:
        GCSStore("bkt")
    assert 'install with: pip install "sk8s[gcs]"' in str(ei.value)


def test_missing_gcs_not_raised_with_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    GCSStore("bkt", client=FakeGCSClient())


def test_import_sk8s_does_not_import_google() -> None:
    import subprocess

    # google.auth may already be loaded by the kubernetes client; the GCS SDK must not be.
    code = (
        "import sys, sk8s, sk8s.store, sk8s.store.gcs; "
        "bad = ('google.cloud', 'google.api_core'); "
        "sys.exit(1 if any(m == b or m.startswith(b + '.') for m in sys.modules for b in bad) else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


class _StorageSpy:
    """Stands in for the google.cloud.storage module."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def Client(self, *args: object, **kwargs: object) -> FakeGCSClient:  # noqa: N802
        self.calls.append((args, kwargs))
        return FakeGCSClient()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _StorageSpy:
    import google.cloud

    s = _StorageSpy()
    monkeypatch.setitem(sys.modules, "google.cloud.storage", s)
    monkeypatch.setattr(google.cloud, "storage", s, raising=False)
    return s


def test_default_client_uses_sdk_default_chain(spy: _StorageSpy) -> None:
    GCSStore("b")
    assert spy.calls == [((), {})]  # no credentials / project passed


def test_factory_wiring(spy: _StorageSpy) -> None:
    s = get_store("gs://b/p/")
    assert isinstance(s, GCSStore)
    assert s.uri_for("k") == "gs://b/p/k"


def test_factory_ignores_endpoint(spy: _StorageSpy) -> None:
    s = get_store("gs://b/p/", endpoint="http://ignored")
    assert isinstance(s, GCSStore)
    assert spy.calls == [((), {})]


def test_factory_local_unchanged(tmp_path: Path) -> None:
    get_store(f"local:{tmp_path}", endpoint="http://ignored")


def test_count_prefix_whole_segments_and_exact_key(store: GCSStore, fake: FakeGCSClient) -> None:
    store.put("wf-a", b"exact")
    store.put("wf-a/j1/payload", b"1")
    store.put("wf-a/j1/result", b"2")
    store.put("wf-ab/j1/payload", b"3")
    assert store.count_prefix("wf-a") == 3
    assert store.count_prefix("/wf-a/") == 3
    assert store.count_prefix("absent") == 0
    assert len(fake.objects) == 4  # reading only: nothing deleted
    assert fake.delete_calls == 0


def test_count_prefix_paginates_and_rejects_empty(fake: FakeGCSClient) -> None:
    fake.page_size = 1000
    s = GCSStore("bkt", "pre", client=fake)
    for i in range(2500):
        fake.objects[("bkt", f"pre/wf-a/j{i:05d}/payload")] = b"x"
    assert s.count_prefix("wf-a") == 2500
    with pytest.raises(ValueError):
        s.count_prefix("")
