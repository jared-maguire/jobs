"""CAS layout, index manifests, put_file, workflow reference sets, location resolution."""
from __future__ import annotations

import datetime
import hashlib
import json
import pickle
import threading
from pathlib import Path
from typing import Any

import pytest

from sk8s import cas
from sk8s.artifacts import Artifact, OutDir, artifact
from sk8s.cas import ArtifactStore, cas_key, index_key, manifest_key, merge_index
from sk8s.store.gcs import GCSStore
from sk8s.store.local import LocalStore
from tests.fakes_gcs import FakeGCSClient

H = hashlib.sha256(b"hello").hexdigest()


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "scratch" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def astore(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(LocalStore(tmp_path / "store"), f"local:{tmp_path / 'store'}")


@pytest.fixture
def gcs_astore() -> tuple[ArtifactStore, FakeGCSClient]:
    fake = FakeGCSClient()
    return ArtifactStore(GCSStore("bkt", "sk8s", client=fake), "gs://bkt/sk8s"), fake


# --- layout ---------------------------------------------------------------------------


def test_layout_keys_match_the_spec() -> None:
    assert cas_key(H) == f"artifacts/cas/sha256/{H[:2]}/{H}"
    assert index_key(H) == f"artifacts/index/sha256/{H[:2]}/{H}.json"
    assert manifest_key("wf-1") == "workflows/wf-1/artifacts.json"
    assert manifest_key("wf-1", "job-9") == "workflows/wf-1/job-9/artifacts.json"


@pytest.mark.parametrize("bad", ["", "abc", "G" * 64, H.upper(), H + "0", "../" + H[3:]])
def test_hashes_are_validated_before_they_become_keys(bad: str) -> None:
    with pytest.raises(ValueError):
        cas_key(bad)


def test_hash_file_streams(tmp_path: Path) -> None:
    big = write(tmp_path, "big", b"x" * (3 * 1024 * 1024 + 17))
    sha, size = cas.hash_file(big)
    assert size == 3 * 1024 * 1024 + 17
    assert sha == hashlib.sha256(b"x" * size).hexdigest()


# --- uris -----------------------------------------------------------------------------


def test_normalize_uri_forms() -> None:
    assert cas.normalize_uri("gs://b/k") == "gs://b/k"
    assert cas.normalize_uri("/data/x.fq") == "local:/data/x.fq"
    assert cas.normalize_uri("file:///data/x.fq") == "local:/data/x.fq"
    assert cas.normalize_uri("local:/data/x.fq") == "local:/data/x.fq"
    with pytest.raises(ValueError, match="unsupported"):
        cas.normalize_uri("ftp://x/y")
    with pytest.raises(ValueError):
        cas.normalize_uri("relative/path")


def test_split_uri() -> None:
    assert cas.split_uri("gs://b/a/c.txt") == ("gs", "b", "a/c.txt")
    assert cas.split_uri("s3://b/k") == ("s3", "b", "k")
    assert cas.split_uri("/x/y") == ("local", "", "x/y")


def test_store_for_uri_local_is_rooted_at_slash(tmp_path: Path) -> None:
    f = write(tmp_path, "f.txt", b"abc")
    store, key = cas.store_for_uri(str(f))
    assert store.get(store.uri_for(key)) == b"abc"


def test_store_for_uri_caches_and_uses_the_bucket_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[str, str]] = []
    fake = FakeGCSClient()
    monkeypatch.setattr(cas, "_bucket_stores", {})
    monkeypatch.setattr(
        cas, "_new_bucket_store",
        lambda scheme, bucket: (made.append((scheme, bucket)), GCSStore(bucket, client=fake))[1],
    )
    s1, k1 = cas.store_for_uri("gs://b1/a/b")
    s2, k2 = cas.store_for_uri("gs://b1/c")
    assert s1 is s2 and (k1, k2) == ("a/b", "c") and made == [("gs", "b1")]


# --- Artifact -------------------------------------------------------------------------


def test_artifact_roundtrips_json_and_pickle() -> None:
    a = Artifact(uri="gs://b/k.bam", sha256=H, size=5, version="123", name="k.bam",
                 store="gs://b/p", produced_by="wf-1/job-2")
    assert Artifact.from_dict(json.loads(json.dumps(a.to_dict()))) == a
    assert pickle.loads(pickle.dumps(a)) == a
    assert "uri" in a.to_dict() and Artifact(uri="gs://b/k").to_dict() == {"uri": "gs://b/k"}


def test_artifact_from_dict_ignores_unknown_fields() -> None:
    assert Artifact.from_dict({"uri": "gs://b/k", "future_field": 1}).uri == "gs://b/k"


def test_sk8s_artifact_reference_is_pinned_lazily() -> None:
    a = artifact("gs://bucket/in/s1.fq.gz")
    assert a.sha256 is None and a.version is None and a.filename == "s1.fq.gz"
    assert artifact("/data/x.fq").uri == "local:/data/x.fq"
    assert artifact("gs://b/x", sha256=H).sha256 == H


def test_artifact_filename_falls_back_to_hash() -> None:
    assert Artifact(uri="gs://b/cas/ab/abcd", sha256="ab" * 32).filename == "abcd"
    assert Artifact(uri="gs://b/", sha256="ab" * 32).filename == "ab" * 32
    assert Artifact(uri="gs://b/cas/x", name="s1.bam").filename == "s1.bam"


def test_repr_is_short() -> None:
    assert "sha256:" + H[:12] in repr(Artifact(uri="gs://b/x", sha256=H, name="x"))
    assert "unhashed" in repr(Artifact(uri="gs://b/x"))


def test_outdir_is_pathlike_and_divides_to_plain_paths(tmp_path: Path) -> None:
    out = OutDir(tmp_path)
    p = out / "s1.bam"
    assert type(p) is type(tmp_path) and p == tmp_path / "s1.bam"
    import os
    assert os.fspath(out) == str(tmp_path)


# --- merge_index -----------------------------------------------------------------------


def test_merge_index_creates_then_is_idempotent() -> None:
    loc = cas.location("cas", "gs://b/x")
    doc = merge_index(None, sha256=H, size=5, add=[loc], provenance={"produced_by": "wf/j"})
    assert doc is not None and doc["locations"] == [loc] and doc["size"] == 5
    assert merge_index(doc, sha256=H, size=5, add=[loc], provenance={"produced_by": "other"}) is None
    assert doc["provenance"] == {"produced_by": "wf/j"}  # first producer wins, never rewritten


def test_merge_index_locations_are_a_set_keyed_by_kind_and_uri() -> None:
    doc = merge_index(None, sha256=H, size=1, add=[cas.location("cas", "u1"), cas.location("published", "u2")])
    assert doc is not None
    again = merge_index(doc, sha256=H, size=1, add=[cas.location("published", "u2", "7")])
    assert again is not None and {(l["kind"], l["uri"]) for l in again["locations"]} == {("cas", "u1"), ("published", "u2")}
    assert next(l for l in again["locations"] if l["kind"] == "published")["version"] == "7"
    gone = merge_index(again, sha256=H, size=1, remove=[("cas", "u1")])
    assert gone is not None and [l["kind"] for l in gone["locations"]] == ["published"]
    assert merge_index(gone, sha256=H, size=1, remove=[("cas", "u1")]) is None  # idempotent remove


def test_merge_index_touch_respects_interval() -> None:
    t0 = datetime.datetime(2026, 10, 11, tzinfo=datetime.timezone.utc)
    doc = merge_index(None, sha256=H, size=1, touch=t0)
    assert doc is not None and doc["last_referenced"] == t0.isoformat()
    assert merge_index(doc, sha256=H, size=1, touch=t0 + datetime.timedelta(seconds=10)) is None
    later = merge_index(doc, sha256=H, size=1, touch=t0 + datetime.timedelta(seconds=120))
    assert later is not None and later["last_referenced"] != doc["last_referenced"]


# --- put_file -------------------------------------------------------------------------


def test_put_file_lands_in_cas_and_index(astore: ArtifactStore, tmp_path: Path) -> None:
    f = write(tmp_path, "s1.bam", b"hello")
    a = astore.put_file(f, workflow="wf-1", job="job-a", inputs=["b" * 64])
    assert a.sha256 == H and a.size == 5 and a.name == "s1.bam"
    assert a.uri == astore.store.uri_for(cas_key(H)) and a.store == astore.spec
    assert a.produced_by == "wf-1/job-a"
    assert astore.store.get(a.uri) == b"hello"
    idx = astore.read_index(H)
    assert idx is not None
    assert idx["size"] == 5 and idx["checksums"] == {"sha256": H}
    assert idx["locations"] == [{"kind": "cas", "uri": a.uri}]
    assert idx["provenance"]["produced_by"] == "wf-1/job-a"
    assert idx["provenance"]["inputs"] == ["b" * 64]
    assert "last_referenced" in idx


def test_put_file_twice_uploads_once(gcs_astore: tuple[ArtifactStore, FakeGCSClient], tmp_path: Path) -> None:
    astore, fake = gcs_astore
    a1 = astore.put_file(write(tmp_path, "a", b"hello"))
    uploads_after_first = fake.upload_calls
    a2 = astore.put_file(write(tmp_path, "b", b"hello"), name="other-name")
    assert a1.sha256 == a2.sha256 and a1.uri == a2.uri
    assert fake.upload_calls == uploads_after_first + 0  # CAS bytes: nothing re-uploaded
    idx = astore.read_index(H)
    assert idx is not None and len(idx["locations"]) == 1


def test_put_file_without_workflow_records_nothing(astore: ArtifactStore, tmp_path: Path) -> None:
    astore.put_file(write(tmp_path, "a", b"hello"))
    assert list(astore.store.list_blobs("workflows/")) == []


def test_put_file_records_intent_before_the_bytes_land(tmp_path: Path) -> None:
    events: list[str] = []

    class Spy(LocalStore):
        def upload_file(self, key: str, path: Path, **kw: Any) -> bool:
            events.append(f"upload {key}")
            return super().upload_file(key, path, **kw)

        def put_if_absent(self, key: str, data: bytes, owner: Any = None) -> bool:
            events.append(f"create {key}")
            return super().put_if_absent(key, data, owner)

    astore = ArtifactStore(Spy(tmp_path / "s"), "local:x")
    astore.put_file(write(tmp_path, "a", b"hello"), workflow="wf-1", job="j")
    kinds = [e.split()[1] for e in events]
    assert kinds.index("workflows/wf-1/j/artifacts.json") < kinds.index(cas_key(H))
    assert kinds.index(cas_key(H)) < kinds.index(index_key(H))


def test_concurrent_identical_puts_create_one_object(
    gcs_astore: tuple[ArtifactStore, FakeGCSClient], tmp_path: Path
) -> None:
    astore, fake = gcs_astore
    files = [write(tmp_path, f"f{i}", b"same bytes") for i in range(6)]
    gate = threading.Barrier(6)
    results: list[Artifact] = []
    errors: list[BaseException] = []

    def go(f: Path) -> None:
        gate.wait()
        try:
            results.append(astore.put_file(f, workflow="wf-1", job=f.name))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(f,)) for f in files]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    sha = hashlib.sha256(b"same bytes").hexdigest()
    assert {a.sha256 for a in results} == {sha} and len({a.uri for a in results}) == 1
    cas_blobs = [k for (_, k) in fake.objects if "/cas/" in k]
    assert cas_blobs == [f"sk8s/{cas_key(sha)}"]
    idx = astore.read_index(sha)
    assert idx is not None and len(idx["locations"]) == 1


def test_put_file_repairs_a_wrong_size_object(astore: ArtifactStore, tmp_path: Path) -> None:
    astore.store.put(cas_key(H), b"hel")  # corrupt: right key, wrong content
    a = astore.put_file(write(tmp_path, "a", b"hello"))
    assert astore.store.get(a.uri) == b"hello"


# --- record_refs -----------------------------------------------------------------------


def test_record_refs_is_a_deduped_sorted_set(astore: ArtifactStore) -> None:
    h2 = hashlib.sha256(b"two").hexdigest()
    astore.record_refs("wf-1", [H, h2])
    astore.record_refs("wf-1", [H])
    doc = json.loads(astore.store.read_with_version("workflows/wf-1/artifacts.json")[0])
    assert doc["refs"] == sorted([H, h2]) and doc["workflow"] == "wf-1"


def test_record_refs_second_call_does_no_io(astore: ArtifactStore, monkeypatch: pytest.MonkeyPatch) -> None:
    astore.record_refs("wf-1", [H])
    monkeypatch.setattr(astore.store, "read_with_version", lambda k: pytest.fail("should not read"))
    astore.record_refs("wf-1", [H])  # cached: already recorded by this client


def test_record_refs_by_job_shards(astore: ArtifactStore) -> None:
    astore.record_refs("wf-1", [H], job="job-a")
    assert astore.store.stat(astore.store.uri_for("workflows/wf-1/job-a/artifacts.json")) is not None
    assert astore.store.stat(astore.store.uri_for("workflows/wf-1/artifacts.json")) is None


def test_record_refs_touches_existing_index_entries(astore: ArtifactStore, tmp_path: Path) -> None:
    astore.put_file(write(tmp_path, "a", b"hello"))
    idx = astore.read_index(H)
    assert idx is not None
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).isoformat()
    idx["last_referenced"] = old
    astore.store.put(index_key(H), json.dumps(idx).encode())
    astore.record_refs("wf-new", [H])
    refreshed = astore.read_index(H)
    assert refreshed is not None and refreshed["last_referenced"] > old


def test_record_refs_rejects_junk(astore: ArtifactStore) -> None:
    with pytest.raises(ValueError):
        astore.record_refs("wf-1", ["not-a-hash"])


def test_ten_clients_recording_the_same_workflow_lose_nothing(astore: ArtifactStore) -> None:
    hs = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(10)]
    gate = threading.Barrier(10)
    # distinct ArtifactStore instances over one backend = ten separate clients (no shared cache)
    clients = [ArtifactStore(astore.store, astore.spec) for _ in hs]

    def go(c: ArtifactStore, h: str) -> None:
        gate.wait()
        c.record_refs("wf-1", [h])

    ts = [threading.Thread(target=go, args=(c, h)) for c, h in zip(clients, hs)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    doc = json.loads(astore.store.read_with_version("workflows/wf-1/artifacts.json")[0])
    assert doc["refs"] == sorted(hs)


# --- resolve -----------------------------------------------------------------------------


def test_resolve_prefers_own_uri(astore: ArtifactStore, tmp_path: Path) -> None:
    a = astore.put_file(write(tmp_path, "a", b"hello"))
    r = astore.resolve(a)
    assert r is not None and r.uri == a.uri and r.stat.size == 5


def test_resolve_falls_back_to_index_locations(astore: ArtifactStore, tmp_path: Path) -> None:
    a = astore.put_file(write(tmp_path, "a", b"hello"))
    published = tmp_path / "pub" / "x.bam"
    published.parent.mkdir()
    published.write_bytes(b"hello")
    astore.update_index(H, add=[cas.location("published", f"local:{published}")])
    astore.store.delete(a.uri)  # CAS copy retired
    r = astore.resolve(a)
    assert r is not None and r.kind == "published" and r.uri == f"local:{published}"


def test_resolve_none_when_nothing_holds_the_bytes(astore: ArtifactStore, tmp_path: Path) -> None:
    a = astore.put_file(write(tmp_path, "a", b"hello"))
    astore.store.delete(a.uri)
    problems: list[str] = []
    assert astore.resolve(a, problems) is None
    assert any("not found" in p for p in problems)


def test_resolve_rejects_wrong_size_and_changed_version(astore: ArtifactStore, tmp_path: Path) -> None:
    ext = write(tmp_path, "ext.fq", b"original")
    st = astore.store.stat(f"local:{ext}")
    assert st is not None
    pinned = Artifact(uri=f"local:{ext}", version=st.version, size=8)
    assert astore.resolve(pinned) is not None
    ext.write_bytes(b"overwritten in place!")
    problems: list[str] = []
    assert astore.resolve(pinned, problems) is None
    assert any("size" in p or "changed" in p for p in problems)
    same_size = Artifact(uri=f"local:{ext}", version=st.version)  # no size to compare: version decides
    problems = []
    assert astore.resolve(same_size, problems) is None and any("changed" in p for p in problems)
