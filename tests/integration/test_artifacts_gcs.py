"""The artifact layer against a REAL GCS bucket (client side, no pods): the unit fakes model
these semantics; this checks the model against the service.

Runs only when ``SK8S_STORE`` is a ``gs://bucket/prefix/`` URL the caller's ADC can write. Every
test works under its own random segment (``spike-artifacts-it/<id>/``) of the bucket and removes
it afterwards.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from sk8s import cas, gc
from sk8s.artifacts import Artifact
from sk8s.cas import ArtifactStore, cas_key
from sk8s.errors import ArtifactChanged, PublishConflict, VersionChanged
from sk8s.store.gcs import GCSStore
from sk8s.store.retry import update_blob
from tests.integration.conftest import requires_cluster

STORE_URL = os.environ.get("SK8S_STORE", "")

pytestmark = [
    pytest.mark.integration,
    requires_cluster,
    pytest.mark.skipif(not STORE_URL.startswith("gs://"), reason="SK8S_STORE is not a gs:// URL"),
    pytest.mark.timeout(300),
]

BUCKET = urlsplit(STORE_URL).netloc


@pytest.fixture
def seg() -> Iterator[str]:
    s = f"spike-artifacts-it/{secrets.token_hex(4)}"
    yield s
    GCSStore(BUCKET).delete_prefix(s)


@pytest.fixture
def astore(seg: str) -> ArtifactStore:
    return ArtifactStore(GCSStore(BUCKET, f"{seg}/store"), f"gs://{BUCKET}/{seg}/store")


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


def test_conditional_writes_on_real_gcs(seg: str) -> None:
    s = GCSStore(BUCKET, seg)
    for trial in range(10):
        gate = threading.Barrier(2)
        wins: list[bool] = []

        def contend(i: int, trial: int = trial, gate: threading.Barrier = gate) -> None:
            gate.wait()
            wins.append(s.put_if_absent(f"race/{trial}", f"w{i}".encode()))

        ts = [threading.Thread(target=contend, args=(i,)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(wins) == [False, True], (trial, wins)
    s.put_if_absent("doc", b"v1")
    data, gen = s.read_with_version("doc")
    assert data == b"v1" and gen.isdigit()
    assert s.put_if_version("doc", b"v2", gen) is True
    assert s.put_if_version("doc", b"stale", gen) is False
    assert s.put_if_version("never-existed", b"x", gen) is False  # 412 on a missing key, no create
    assert s.read_with_version("doc")[0] == b"v2"


def test_ten_way_contended_manifest_update(seg: str) -> None:
    s = GCSStore(BUCKET, seg)
    gate = threading.Barrier(10)
    errors: list[BaseException] = []

    def add(m: str) -> Any:
        def mutate(cur: bytes | None) -> bytes | None:
            members = set(json.loads(cur)) if cur else set()
            return None if m in members else json.dumps(sorted(members | {m})).encode()

        return mutate

    def go(i: int) -> None:
        gate.wait()
        try:
            update_blob(GCSStore(BUCKET, seg), "set", add(f"m{i}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert json.loads(s.read_with_version("set")[0]) == sorted(f"m{i}" for i in range(10))


def test_object_ops_metadata_pins_and_rewrite(seg: str, tmp_path: Path) -> None:
    s = GCSStore(BUCKET, seg)
    meta = {"sk8s-artifact-sha256": "abc", "sk8s-produced-by": "wf-1/job-2"}
    small = write(tmp_path, "small", b"hello")
    assert s.upload_file("o/a", small, metadata=meta) is True
    assert s.upload_file("o/a", small, if_absent=True) is False
    st = s.stat(s.uri_for("o/a"))
    assert st is not None and st.metadata == meta and st.size == 5 and st.version.isdigit()

    # the wipe trap, confirmed live: a plain content overwrite drops custom metadata ...
    s.upload_file("o/a", write(tmp_path, "v2", b"hello again"))
    wiped = s.stat(s.uri_for("o/a"))
    assert wiped is not None and wiped.metadata == {} and wiped.version != st.version
    # ... and a read pinned to the old generation is gone (versioning off: detect, not recover)
    with pytest.raises(VersionChanged):
        s.download_file(s.uri_for("o/a"), tmp_path / "x", version=st.version)

    # rewrite: server-side, metadata replaced wholesale, create-if-absent
    new_meta = {"sk8s-artifact-sha256": "def", "sk8s-produced-by": "wf-9/j"}
    assert s.server_copy(s.uri_for("o/a"), s, "pub/a", metadata=new_meta, if_absent=True) is True
    got = s.stat(s.uri_for("pub/a"))
    assert got is not None and got.metadata == new_meta and got.size == 11
    assert s.server_copy(s.uri_for("o/a"), s, "pub/a", metadata=new_meta, if_absent=True) is False

    # a multi-chunk file: streamed hash == content hash, pinned reads work, list sees it
    payload = os.urandom(24 * 1024 * 1024)
    s.upload_file("o/big", write(tmp_path, "big", payload))
    bst = s.stat(s.uri_for("o/big"))
    assert bst is not None
    h = hashlib.sha256()
    s.download_file(s.uri_for("o/big"), tmp_path / "big.out", version=bst.version, hasher=h)
    assert h.hexdigest() == hashlib.sha256(payload).hexdigest()
    assert sorted(i.key for i in s.list_blobs("o/")) == ["o/a", "o/big"]
    assert all(i.updated is not None for i in s.list_blobs("o/"))


def test_cas_publish_and_download_over_gcs(astore: ArtifactStore, seg: str, tmp_path: Path) -> None:
    data = os.urandom(5 * 1024 * 1024)
    a = astore.put_file(write(tmp_path, "s1.bam", data), workflow="wf-it", job="job-a")
    assert a.uri == f"gs://{BUCKET}/{seg}/store/{cas_key(a.sha256 or '')}"
    dest = f"gs://{BUCKET}/{seg}/published/s1.bam"
    pub = a.publish(dest)
    st = GCSStore(BUCKET).stat(dest)
    assert st is not None and st.size == len(data)
    assert st.metadata == {"sk8s-artifact-sha256": a.sha256, "sk8s-produced-by": "wf-it/job-a"}
    assert astore.store.stat(a.uri) is None  # moved
    idx = astore.read_index(a.sha256 or "")
    assert idx is not None and [loc["kind"] for loc in idx["locations"]] == ["published"]
    assert a.download(tmp_path / "back.bam").read_bytes() == data  # the old handle resolves via the index
    with pytest.raises(PublishConflict):
        astore.put_file(write(tmp_path, "other", b"different"), job="j").publish(dest)
    assert pub.version == st.version


def test_sweep_on_real_gcs(astore: ArtifactStore, tmp_path: Path) -> None:
    kept = astore.put_file(write(tmp_path, "k", os.urandom(64)), workflow="wf-keep", job="j")
    gone = astore.put_file(write(tmp_path, "g", os.urandom(64)))
    report = gc.sweep(astore.store, min_age_seconds=0.0)
    assert report.aborted is None, report.aborted
    assert report.deleted == [gone.sha256] and report.kept_reachable == 1
    assert astore.store.stat(kept.uri) is not None and astore.store.stat(gone.uri) is None
    young = astore.put_file(write(tmp_path, "y", os.urandom(64)))
    assert gc.sweep(astore.store).deleted == []  # the default 300 s floor protects a fresh object
    assert astore.store.stat(young.uri) is not None
