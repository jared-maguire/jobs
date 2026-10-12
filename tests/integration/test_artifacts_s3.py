"""Artifact layer against an in-cluster MinIO: the S3 conditional-write paths, live.

SPIKE-CCW left the S3/MinIO column UNVERIFIED; this suite is that verification for the
operations the artifact layer uses (PutObject If-None-Match / If-Match, CopyObject metadata,
HEAD metadata, multipart upload, listing) and then runs the whole layer over an s3:// store.
It reuses the MinIO fixture of test_store_s3_minio (deployed in the suite namespace by
tests/integration/minio.py; client reaches it through a port-forward).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

import sk8s
from sk8s.artifacts import Artifact
from sk8s.cluster import Cluster
from sk8s.config import load_config
from sk8s.store.retry import update_blob
from sk8s.store.s3 import S3Store
from tests.integration import minio
from tests.integration.conftest import requires_cluster, suite_namespace, suite_pull_policy
from tests.integration.test_store_s3_minio import s3_client  # noqa: F401  (session fixture)

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

BUCKET = "sk8s-art-it"
IMAGE = os.environ.get("SK8S_ARTIFACTS_TEST_IMAGE", "sk8s-dev-artifacts:latest")


@pytest.fixture(scope="module")
def bucket(s3_client: Any) -> Iterator[str]:  # noqa: F811
    try:
        s3_client.create_bucket(Bucket=BUCKET)
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        pass
    yield BUCKET
    # empty and drop it
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": BUCKET}
        if token:
            kw["ContinuationToken"] = token
        resp = s3_client.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            s3_client.delete_object(Bucket=BUCKET, Key=o["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp["NextContinuationToken"]
    s3_client.delete_bucket(Bucket=BUCKET)


def store_at(s3_client: Any, prefix: str) -> S3Store:
    return S3Store(BUCKET, prefix, client=s3_client)


def err_of(exc: BaseException) -> tuple[str, int]:
    r = getattr(exc, "response", {})
    return r.get("Error", {}).get("Code", ""), r.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)


def test_what_does_minio_actually_say(s3_client: Any, bucket: str) -> None:  # noqa: F811
    """The raw facts SPIKE-CCW could not get: the error code/status of each conditional failure."""
    key = f"probe/{os.urandom(4).hex()}"
    s3_client.put_object(Bucket=bucket, Key=key, Body=b"a", IfNoneMatch="*")
    with pytest.raises(Exception) as exists:
        s3_client.put_object(Bucket=bucket, Key=key, Body=b"b", IfNoneMatch="*")
    etag = s3_client.head_object(Bucket=bucket, Key=key)["ETag"]
    with pytest.raises(Exception) as stale:
        s3_client.put_object(Bucket=bucket, Key=key, Body=b"c", IfMatch='"0000"')
    with pytest.raises(Exception) as missing:
        s3_client.put_object(Bucket=bucket, Key=f"{key}-nope", Body=b"c", IfMatch=etag)
    print("\nMINIO conditional-write failures:")
    print("  If-None-Match on existing key ->", err_of(exists.value))
    print("  If-Match with a stale etag    ->", err_of(stale.value))
    print("  If-Match on a missing key     ->", err_of(missing.value))
    assert err_of(exists.value)[1] == 412
    assert err_of(stale.value)[1] == 412
    assert err_of(missing.value)[1] in (404, 412)
    assert s3_client.get_object(Bucket=bucket, Key=key)["Body"].read() == b"a"  # losers wrote nothing


def test_create_if_absent_race_has_one_winner(s3_client: Any, bucket: str) -> None:  # noqa: F811
    store = store_at(s3_client, f"race-{os.urandom(3).hex()}")
    for trial in range(20):
        gate = threading.Barrier(2)
        wins: list[bool] = []

        def contend(i: int, trial: int = trial, gate: threading.Barrier = gate) -> None:
            gate.wait()
            wins.append(store.put_if_absent(f"k{trial}", f"w{i}".encode()))

        ts = [threading.Thread(target=contend, args=(i,)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(wins) == [False, True], (trial, wins)
        assert store.read_with_version(f"k{trial}")[0] in (b"w0", b"w1")


def test_if_match_cas_and_ten_way_contention(s3_client: Any, bucket: str) -> None:  # noqa: F811
    store = store_at(s3_client, f"cas-{os.urandom(3).hex()}")
    store.put_if_absent("doc", b"v1")
    _, v1 = store.read_with_version("doc")
    assert store.put_if_version("doc", b"v2", v1) is True
    assert store.put_if_version("doc", b"stale", v1) is False
    assert store.read_with_version("doc")[0] == b"v2"
    assert store.put_if_version("nonexistent", b"x", v1) is False

    def add(m: str) -> Any:
        def mutate(cur: bytes | None) -> bytes | None:
            members = set(json.loads(cur)) if cur else set()
            return None if m in members else json.dumps(sorted(members | {m})).encode()

        return mutate

    gate = threading.Barrier(10)
    errors: list[BaseException] = []

    def go(i: int) -> None:
        gate.wait()
        try:
            update_blob(store, "set", add(f"m{i}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert json.loads(store.read_with_version("set")[0]) == sorted(f"m{i}" for i in range(10))


def test_metadata_copy_stat_list_and_multipart(s3_client: Any, bucket: str, tmp_path: Path) -> None:  # noqa: F811
    store = store_at(s3_client, f"obj-{os.urandom(3).hex()}")
    meta = {"sk8s-artifact-sha256": "abc", "sk8s-produced-by": "wf-1/job-2"}
    small = tmp_path / "small"
    small.write_bytes(b"hello")
    assert store.upload_file("a/small", small, metadata=meta) is True
    assert store.upload_file("a/small", small, if_absent=True) is False
    st = store.stat(store.uri_for("a/small"))
    assert st is not None and st.metadata == meta and st.size == 5

    # server-side copy, metadata replaced, to another bucket path of the same service
    dst = store_at(s3_client, f"dst-{os.urandom(3).hex()}")
    new_meta = {"sk8s-artifact-sha256": "def", "sk8s-produced-by": "wf-9/j"}
    assert store.server_copy(store.uri_for("a/small"), dst, "pub/small", metadata=new_meta, if_absent=True) is True
    got = dst.stat(dst.uri_for("pub/small"))
    assert got is not None and got.metadata == new_meta
    assert store.server_copy(store.uri_for("a/small"), dst, "pub/small", metadata=new_meta, if_absent=True) is False

    # a 20 MiB file goes multipart: the ETag is not an md5, and the streamed hash still matches
    big = tmp_path / "big"
    payload = os.urandom(20 * 1024 * 1024)
    big.write_bytes(payload)
    store.upload_file("a/big", big)
    bst = store.stat(store.uri_for("a/big"))
    assert bst is not None and bst.size == len(payload)
    assert "-" in bst.version, f"expected a multipart ETag, got {bst.version}"
    h = hashlib.sha256()
    out = tmp_path / "big.out"
    store.download_file(store.uri_for("a/big"), out, hasher=h)
    assert h.hexdigest() == hashlib.sha256(payload).hexdigest() and out.read_bytes() == payload

    # pinned download detects an overwrite (If-Match on GetObject)
    from sk8s.errors import VersionChanged

    changed = tmp_path / "changed"
    changed.write_bytes(b"different")  # (identical bytes would keep the same ETag: correctly no change)
    store.upload_file("a/small", changed, metadata=None)
    with pytest.raises(VersionChanged):
        store.download_file(store.uri_for("a/small"), tmp_path / "x", version=st.version)

    keys = sorted(i.key for i in store.list_blobs("a/"))
    assert keys == ["a/big", "a/small"]


@pytest.fixture
def s3_cluster(s3_client: Any, bucket: str) -> Iterator[Cluster]:  # noqa: F811
    ns = suite_namespace()
    prefix = f"art-{os.urandom(3).hex()}/"
    c = Cluster(load_config(
        namespace=ns, image=IMAGE, pull_policy=suite_pull_policy(), ttl_seconds_after_finished=30,
        store=f"s3://{BUCKET}/{prefix}",
        store_endpoint=f"http://minio.{ns}.svc.cluster.local:{minio.PORT}",
        store_env={"AWS_ACCESS_KEY_ID": minio.ACCESS_KEY, "AWS_SECRET_ACCESS_KEY": minio.SECRET_KEY},
    ))
    yield c
    c.close()


def test_artifact_layer_over_s3_end_to_end(s3_cluster: Cluster, s3_client: Any, bucket: str, tmp_path: Path) -> None:  # noqa: F811
    c = s3_cluster
    # an EXTERNAL input: a plain object in the bucket, referenced by uri, pinned by ETag
    s3_client.put_object(Bucket=bucket, Key="inputs/s1.txt", Body=b"hello from s3\n")
    ref = sk8s.artifact(f"s3://{bucket}/inputs/s1.txt")

    def shout(f: Path, out: sk8s.OutDir) -> Path:
        (out / "s1.loud.txt").write_text(f.read_text().upper())
        return out / "s1.loud.txt"

    first = c.submit(shout, ref, memo=True)
    result = first.result(timeout=240)
    assert isinstance(result, Artifact) and result.uri.startswith(f"s3://{bucket}/")
    assert result.download(tmp_path / "o.txt").read_text() == "HELLO FROM S3\n"
    idx = c.artifact_store.read_index(result.sha256 or "")
    assert idx is not None and idx["provenance"]["produced_by"] == result.produced_by

    # memo hit: no new jobs
    before = {j.metadata.name for j in c.batch.list_namespaced_job(namespace=c.config.namespace).items}
    again = c.submit(shout, ref, memo=True)
    assert again.memo_hit and again.result() == result
    assert {j.metadata.name for j in c.batch.list_namespaced_job(namespace=c.config.namespace).items} == before

    # overwrite the input in place: ETag changes -> memo miss, and the new content flows
    s3_client.put_object(Bucket=bucket, Key="inputs/s1.txt", Body=b"changed in place\n")
    third = c.submit(shout, sk8s.artifact(f"s3://{bucket}/inputs/s1.txt"), memo=True)
    assert not third.memo_hit
    assert third.result(timeout=240).download(tmp_path / "o2.txt").read_text() == "CHANGED IN PLACE\n"

    # publish: server-side CopyObject with provenance metadata; the CAS copy is retired
    dest = f"s3://{bucket}/published/s1.loud.txt"
    pub = result.publish(dest)
    head = s3_client.head_object(Bucket=bucket, Key="published/s1.loud.txt")
    assert head["Metadata"]["sk8s-artifact-sha256"] == result.sha256
    assert head["Metadata"]["sk8s-produced-by"] == result.produced_by
    assert pub.uri == dest and c.artifact_store.store.stat(result.uri) is None
    assert result.download(tmp_path / "o3.txt").read_text() == "HELLO FROM S3\n"  # old handle resolves via the index
