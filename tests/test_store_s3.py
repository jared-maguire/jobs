from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sk8s.errors import BlobNotFound, Sk8sError
from sk8s.store import get_store
from sk8s.store.s3 import S3Store
from tests.fakes_s3 import FakeS3Client


@pytest.fixture
def fake() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def store(fake: FakeS3Client) -> S3Store:
    return S3Store("bkt", "pre", client=fake)


def test_roundtrip(store: S3Store, fake: FakeS3Client) -> None:
    uri = store.put("wf/job/payload", b"hello")
    assert uri == "s3://bkt/pre/wf/job/payload"
    assert fake.objects[("bkt", "pre/wf/job/payload")] == b"hello"
    assert store.get(uri) == b"hello"


def test_owner_ignored(store: S3Store) -> None:
    assert store.get(store.put("k", b"v", owner=object())) == b"v"


def test_no_prefix(fake: FakeS3Client) -> None:
    s = S3Store("bkt", client=fake)
    assert s.put("a/b", b"1") == "s3://bkt/a/b"


def test_uri_for_deterministic_no_io(store: S3Store, fake: FakeS3Client) -> None:
    assert store.uri_for("wf/x/result") == "s3://bkt/pre/wf/x/result"
    assert store.uri_for("wf/x/result") == store.uri_for("wf/x/result")
    assert fake.objects == {} and fake.list_calls == 0
    assert store.put("wf/x/result", b"r") == store.uri_for("wf/x/result")


def test_get_missing_raises_blobnotfound(store: S3Store) -> None:
    with pytest.raises(BlobNotFound):
        store.get("s3://bkt/pre/nope")


def test_delete_idempotent(store: S3Store) -> None:
    uri = store.put("k", b"v")
    store.delete(uri)
    store.delete(uri)
    with pytest.raises(BlobNotFound):
        store.get(uri)


def test_delete_prefix_whole_segment_boundary(store: S3Store, fake: FakeS3Client) -> None:
    store.put("wf-a/j1/payload", b"1")
    store.put("wf-a/j1/result", b"2")
    store.put("wf-ab/j1/payload", b"3")
    store.put("wf-a1/j1/payload", b"4")
    other = S3Store("bkt", "other", client=fake)
    other.put("wf-a/j1/payload", b"5")
    store.delete_prefix("wf-a")
    assert sorted(k for _, k in fake.objects) == [
        "other/wf-a/j1/payload",
        "pre/wf-a1/j1/payload",
        "pre/wf-ab/j1/payload",
    ]


def test_delete_prefix_slashes_ignored_and_exact_key(store: S3Store, fake: FakeS3Client) -> None:
    store.put("wf-a", b"exact")
    store.put("wf-a/x", b"x")
    store.put("wf-ab", b"keep")
    store.delete_prefix("/wf-a/")
    assert sorted(k for _, k in fake.objects) == ["pre/wf-ab"]


def test_delete_prefix_empty_raises(store: S3Store, fake: FakeS3Client) -> None:
    store.put("k", b"v")
    for bad in ("", "/", "//"):
        with pytest.raises(ValueError):
            store.delete_prefix(bad)
    assert len(fake.objects) == 1


def test_delete_prefix_paginates_and_batches(fake: FakeS3Client) -> None:
    fake.page_size = 1000
    s = S3Store("bkt", "pre", client=fake)
    for i in range(2500):
        fake.objects[("bkt", f"pre/wf-a/j{i:05d}/payload")] = b"x"
    fake.objects[("bkt", "pre/wf-ab/keep")] = b"k"
    s.delete_prefix("wf-a")
    assert list(fake.objects) == [("bkt", "pre/wf-ab/keep")]
    assert fake.list_calls == 3
    assert fake.delete_batches == [1000, 1000, 500]


def test_delete_prefix_batches_capped_when_pages_large(fake: FakeS3Client) -> None:
    fake.page_size = 5000  # a server returning oversized pages must still be batched <=1000
    s = S3Store("bkt", client=fake)
    for i in range(2500):
        fake.objects[("bkt", f"wf-a/{i}")] = b"x"
    s.delete_prefix("wf-a")
    assert not fake.objects
    assert max(fake.delete_batches) <= 1000


def test_delete_prefix_noop_when_nothing_matches(store: S3Store, fake: FakeS3Client) -> None:
    store.delete_prefix("absent")
    assert fake.delete_batches == []


def test_from_url() -> None:
    fake = FakeS3Client()
    a = S3Store.from_url("s3://bkt", client=fake)
    assert a.uri_for("k") == "s3://bkt/k"
    b = S3Store.from_url("s3://bkt/some/pre/", client=fake)
    assert b.uri_for("k") == "s3://bkt/some/pre/k"
    with pytest.raises(ValueError):
        S3Store.from_url("gs://bkt")
    with pytest.raises(ValueError):
        S3Store.from_url("s3:///x")


def test_missing_boto3_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)  # makes `import boto3` raise ImportError
    with pytest.raises(Sk8sError) as ei:
        S3Store("bkt")
    assert 'install with: pip install "sk8s[s3]"' in str(ei.value)


def test_missing_boto3_not_raised_with_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    S3Store("bkt", client=FakeS3Client())


def test_import_sk8s_does_not_import_boto3() -> None:
    import subprocess

    code = (
        "import sys, sk8s, sk8s.store, sk8s.store.s3; "
        "sys.exit(1 if 'boto3' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


class _Boto3Spy:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def client(self, service: str, **kwargs: object) -> FakeS3Client:
        assert service == "s3"
        self.kwargs = kwargs
        return FakeS3Client()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Boto3Spy:
    s = _Boto3Spy()
    monkeypatch.setitem(sys.modules, "boto3", s)
    monkeypatch.delenv("SK8S_S3_ENDPOINT", raising=False)
    return s


def test_endpoint_default_is_sdk_default(spy: _Boto3Spy) -> None:
    S3Store("b")
    assert "endpoint_url" not in spy.kwargs or spy.kwargs["endpoint_url"] is None


def test_endpoint_from_config_arg(spy: _Boto3Spy) -> None:
    S3Store("b", endpoint="http://cfg:9000")
    assert spy.kwargs["endpoint_url"] == "http://cfg:9000"


def test_endpoint_env_beats_config(spy: _Boto3Spy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SK8S_S3_ENDPOINT", "http://env:9000")
    S3Store("b", endpoint="http://cfg:9000")
    assert spy.kwargs["endpoint_url"] == "http://env:9000"
    S3Store("b")
    assert spy.kwargs["endpoint_url"] == "http://env:9000"


def test_no_credentials_passed(spy: _Boto3Spy) -> None:
    S3Store("b", endpoint="http://x")
    assert set(spy.kwargs) <= {"endpoint_url"}


def test_factory_wiring(spy: _Boto3Spy) -> None:
    s = get_store("s3://b/p/")
    assert isinstance(s, S3Store)
    assert s.uri_for("k") == "s3://b/p/k"


def test_factory_passes_endpoint(spy: _Boto3Spy) -> None:
    get_store("s3://b/p/", endpoint="http://cfg:9000")
    assert spy.kwargs["endpoint_url"] == "http://cfg:9000"


def test_factory_local_unchanged(tmp_path: Path) -> None:
    get_store(f"local:{tmp_path}", endpoint="http://ignored")
