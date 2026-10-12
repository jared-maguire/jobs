"""Streaming/metadata object operations (stat, list_blobs, upload_file, download_file,
server_copy) across all four stores."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sk8s.errors import BlobNotFound, StoreError, VersionChanged
from sk8s.store.base import BlobStore
from sk8s.store.configmap import ConfigMapStore
from sk8s.store.gcs import GCSStore
from sk8s.store.local import LocalStore
from sk8s.store.s3 import S3Store
from tests.fakes import FakeCoreV1
from tests.fakes_gcs import FakeGCSClient
from tests.fakes_s3 import FakeS3Client

META = {"sk8s-artifact-sha256": "abc", "sk8s-produced-by": "wf-1/job-2"}


@pytest.fixture(params=["local", "configmap", "gcs", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> BlobStore:
    if request.param == "local":
        return LocalStore(tmp_path / "store")
    if request.param == "configmap":
        return ConfigMapStore(namespace="ns1", core_api=FakeCoreV1())
    if request.param == "gcs":
        return GCSStore("bkt", "pre", client=FakeGCSClient())
    return S3Store("bkt", "pre", client=FakeS3Client())


def carries_metadata(store: BlobStore) -> bool:
    return not isinstance(store, ConfigMapStore)


def src_file(tmp_path: Path, data: bytes = b"hello artifact") -> Path:
    p = tmp_path / "src.bin"
    p.write_bytes(data)
    return p


def test_stat_missing_is_none(store: BlobStore) -> None:
    assert store.stat(store.uri_for("nope/x")) is None


def test_upload_then_stat_download_roundtrip(store: BlobStore, tmp_path: Path) -> None:
    assert store.upload_file("a/b/file.bin", src_file(tmp_path)) is True
    uri = store.uri_for("a/b/file.bin")
    st = store.stat(uri)
    assert st is not None and st.size == len(b"hello artifact") and st.version
    dest = tmp_path / "out" / "got.bin"
    h = hashlib.sha256()
    got = store.download_file(uri, dest, hasher=h)
    assert dest.read_bytes() == b"hello artifact"
    assert h.hexdigest() == hashlib.sha256(b"hello artifact").hexdigest()  # hash-while-streaming
    assert got.version == st.version and got.size == st.size


def test_download_missing_raises_blobnotfound(store: BlobStore, tmp_path: Path) -> None:
    with pytest.raises(BlobNotFound):
        store.download_file(store.uri_for("nope"), tmp_path / "x")


def test_upload_if_absent_does_not_clobber(store: BlobStore, tmp_path: Path) -> None:
    assert store.upload_file("k", src_file(tmp_path, b"first"), if_absent=True) is True
    assert store.upload_file("k", src_file(tmp_path, b"second"), if_absent=True) is False
    assert store.get(store.uri_for("k")) == b"first"


def test_upload_overwrite_by_default(store: BlobStore, tmp_path: Path) -> None:
    store.upload_file("k", src_file(tmp_path, b"first"))
    store.upload_file("k", src_file(tmp_path, b"second"))
    assert store.get(store.uri_for("k")) == b"second"


def test_upload_metadata_lands_with_content(store: BlobStore, tmp_path: Path) -> None:
    if not carries_metadata(store):
        with pytest.raises(StoreError, match="metadata"):
            store.upload_file("k", src_file(tmp_path), metadata=META)
        return
    store.upload_file("k", src_file(tmp_path), metadata=META)
    st = store.stat(store.uri_for("k"))
    assert st is not None and st.metadata == META


def test_pinned_download_detects_overwrite(store: BlobStore, tmp_path: Path) -> None:
    store.upload_file("k", src_file(tmp_path, b"v1"))
    uri = store.uri_for("k")
    st = store.stat(uri)
    assert st is not None
    if isinstance(store, ConfigMapStore):
        store.put("k", b"v2-changed")  # delete+recreate: new object, new resourceVersion
    else:
        store.upload_file("k", src_file(tmp_path, b"v2-changed"))
    with pytest.raises(VersionChanged):
        store.download_file(uri, tmp_path / "x", version=st.version)
    assert not (tmp_path / "x").exists() or (tmp_path / "x").read_bytes() != b"v2-changed"


def test_pinned_download_ok_when_unchanged(store: BlobStore, tmp_path: Path) -> None:
    store.upload_file("k", src_file(tmp_path, b"v1"))
    uri = store.uri_for("k")
    st = store.stat(uri)
    assert st is not None
    store.download_file(uri, tmp_path / "x", version=st.version)
    assert (tmp_path / "x").read_bytes() == b"v1"


def test_pinned_download_of_deleted_object_is_versionchanged(store: BlobStore, tmp_path: Path) -> None:
    store.upload_file("k", src_file(tmp_path, b"v1"))
    uri = store.uri_for("k")
    st = store.stat(uri)
    assert st is not None
    store.delete(uri)
    with pytest.raises(VersionChanged):
        store.download_file(uri, tmp_path / "x", version=st.version)


def test_list_blobs_prefix_keys_sizes(store: BlobStore, tmp_path: Path) -> None:
    store.put_if_absent("artifacts/cas/sha256/ab/abc", b"12345")
    store.put_if_absent("artifacts/cas/sha256/cd/cde", b"1")
    store.put_if_absent("artifacts/index/sha256/ab/abc.json", b"{}")
    store.put_if_absent("wf-1/job/payload", b"p")
    got = {i.key: i.size for i in store.list_blobs("artifacts/cas/sha256/")}
    assert got == {"artifacts/cas/sha256/ab/abc": 5, "artifacts/cas/sha256/cd/cde": 1}
    everything = {i.key for i in store.list_blobs("artifacts/")}
    assert len(everything) == 3
    assert all(i.updated is not None for i in store.list_blobs("artifacts/"))


def test_list_blobs_empty_prefix_is_empty_not_error(store: BlobStore) -> None:
    assert list(store.list_blobs("artifacts/cas/sha256/")) == []


def test_list_skips_store_internals(tmp_path: Path) -> None:
    s = LocalStore(tmp_path / "store")
    s.put_if_absent("artifacts/x", b"a")
    s.put_if_version("artifacts/x", b"b", s.read_with_version("artifacts/x")[1])  # creates a lock file
    s.upload_file("artifacts/y", src_file(tmp_path), metadata=META)  # creates a sidecar
    assert sorted(i.key for i in s.list_blobs("")) == ["artifacts/x", "artifacts/y"]


# --- server_copy -------------------------------------------------------------------


def test_server_copy_between_two_gcs_buckets_replaces_metadata(tmp_path: Path) -> None:
    fake = FakeGCSClient()
    a, b = GCSStore("srcb", "p", client=fake), GCSStore("dstb", client=fake)
    a.upload_file("k", src_file(tmp_path, b"payload"), metadata={"old": "x"})
    ok = a.server_copy(a.uri_for("k"), b, "runs/out.bin", metadata=META, if_absent=True)
    assert ok is True and fake.rewrite_calls == 1
    st = b.stat("gs://dstb/runs/out.bin")
    assert st is not None and st.metadata == META and b.get("gs://dstb/runs/out.bin") == b"payload"
    # create-if-absent: a second publish onto the same name is refused, bytes untouched
    assert a.server_copy(a.uri_for("k"), b, "runs/out.bin", metadata=META, if_absent=True) is False


def test_server_copy_gcs_none_metadata_preserves_source(tmp_path: Path) -> None:
    fake = FakeGCSClient()
    a = GCSStore("bkt", client=fake)
    a.upload_file("k", src_file(tmp_path), metadata=META)
    a.server_copy(a.uri_for("k"), a, "k2", metadata=None, if_absent=False)
    st = a.stat(a.uri_for("k2"))
    assert st is not None and st.metadata == META


def test_server_copy_gcs_missing_source(tmp_path: Path) -> None:
    a = GCSStore("bkt", client=FakeGCSClient())
    with pytest.raises(BlobNotFound):
        a.server_copy(a.uri_for("nope"), a, "k2", metadata=None, if_absent=False)


def test_server_copy_s3_same_client(tmp_path: Path) -> None:
    fake = FakeS3Client()
    a, b = S3Store("src", "p", client=fake), S3Store("dst", client=fake)
    a.upload_file("k", src_file(tmp_path, b"payload"))
    assert a.server_copy(a.uri_for("k"), b, "out/x", metadata=META, if_absent=True) is True
    st = b.stat("s3://dst/out/x")
    assert st is not None and st.metadata == META
    assert a.server_copy(a.uri_for("k"), b, "out/x", metadata=META, if_absent=True) is False


def test_server_copy_s3_different_services_declines(tmp_path: Path) -> None:
    a, b = S3Store("src", client=FakeS3Client()), S3Store("dst", client=FakeS3Client())
    a.upload_file("k", src_file(tmp_path))
    assert a.server_copy(a.uri_for("k"), b, "x", metadata=None, if_absent=False) is None


def test_server_copy_local_to_local(tmp_path: Path) -> None:
    a, b = LocalStore(tmp_path / "a"), LocalStore(tmp_path / "b")
    a.upload_file("k", src_file(tmp_path, b"zz"))
    assert a.server_copy(a.uri_for("k"), b, "pub/k", metadata=META, if_absent=True) is True
    st = b.stat(b.uri_for("pub/k"))
    assert st is not None and st.metadata == META and b.get(b.uri_for("pub/k")) == b"zz"
    assert a.server_copy(a.uri_for("k"), b, "pub/k", metadata=META, if_absent=True) is False


def test_server_copy_across_store_kinds_declines(tmp_path: Path) -> None:
    g = GCSStore("bkt", client=FakeGCSClient())
    l = LocalStore(tmp_path)
    cm = ConfigMapStore("ns", FakeCoreV1())
    assert g.server_copy("gs://bkt/x", l, "x", metadata=None, if_absent=False) is None
    assert l.server_copy(l.uri_for("x"), g, "x", metadata=None, if_absent=False) is None
    assert cm.server_copy("k8s://ns/x", g, "x", metadata=None, if_absent=False) is None
