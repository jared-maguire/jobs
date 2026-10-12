"""Artifact.publish (move by default / copy=True) and Artifact.download."""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from sk8s import cas
from sk8s.artifacts import Artifact, artifact
from sk8s.cas import META_PRODUCED_BY, META_SHA, ArtifactStore, cas_key
from sk8s.errors import ArtifactChanged, ArtifactError, ArtifactNotFound, PublishConflict
from sk8s.store.gcs import GCSStore
from sk8s.store.local import LocalStore
from sk8s.store.s3 import S3Store
from tests.fakes_gcs import FakeGCSClient
from tests.fakes_s3 import FakeS3Client

DATA = b"the bytes of a BAM"
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A CAS on a local store; gs:// and s3:// destinations served by fakes."""
    gcs, s3 = FakeGCSClient(), FakeS3Client()
    monkeypatch.setattr(cas, "_bucket_stores", {})
    monkeypatch.setattr(
        cas, "_new_bucket_store",
        lambda scheme, bucket: GCSStore(bucket, client=gcs) if scheme == "gs" else S3Store(bucket, client=s3),
    )
    spec = f"local:{tmp_path / 'store'}"
    astore = ArtifactStore(LocalStore(tmp_path / "store"), spec)
    monkeypatch.setattr(cas, "_artifact_stores", {(spec, ""): astore})
    src = tmp_path / "scratch" / "s1.bam"
    src.parent.mkdir()
    src.write_bytes(DATA)
    art = astore.put_file(src, workflow="wf-7f3a", job="job-x")
    return {"astore": astore, "art": art, "gcs": gcs, "s3": s3, "tmp": tmp_path}


def dest_local(world: dict[str, Any], name: str = "pub/s1.bam") -> str:
    return str(world["tmp"] / name)


# --- move (the default) -----------------------------------------------------------------


def test_publish_moves_by_default(world: dict[str, Any]) -> None:
    astore: ArtifactStore = world["astore"]
    art: Artifact = world["art"]
    dst = dest_local(world)
    pub = art.publish(dst)
    assert pub.uri == f"local:{dst}" and pub.sha256 == SHA and pub.size == len(DATA)
    assert pub.store == art.store and pub.produced_by == "wf-7f3a/job-x"
    assert Path(dst).read_bytes() == DATA
    assert astore.store.stat(art.uri) is None  # the CAS copy is retired: single copy
    idx = astore.read_index(SHA)
    assert idx is not None
    assert [(l["kind"], l["uri"]) for l in idx["locations"]] == [("published", f"local:{dst}")]


def test_publish_stamps_provenance_metadata(world: dict[str, Any]) -> None:
    dst = dest_local(world)
    world["art"].publish(dst)
    st = LocalStore("/").stat(f"local:{dst}")
    assert st is not None
    assert st.metadata == {META_SHA: SHA, META_PRODUCED_BY: "wf-7f3a/job-x"}


def test_old_cas_artifact_still_resolves_after_the_move(world: dict[str, Any], tmp_path: Path) -> None:
    art: Artifact = world["art"]
    art.publish(dest_local(world))
    got = art.download(tmp_path / "got.bam")  # art.uri points at the retired CAS key
    assert got.read_bytes() == DATA


def test_move_to_gcs_streams_when_stores_differ_and_metadata_lands_with_content(world: dict[str, Any]) -> None:
    gcs: FakeGCSClient = world["gcs"]
    pub = world["art"].publish("gs://lab/runs/oct/s1.bam")
    assert gcs.objects[("lab", "runs/oct/s1.bam")] == DATA
    assert gcs.meta[("lab", "runs/oct/s1.bam")].metadata == {META_SHA: SHA, META_PRODUCED_BY: "wf-7f3a/job-x"}
    assert gcs.rewrite_calls == 0 and pub.version == str(gcs.meta[("lab", "runs/oct/s1.bam")].generation)


def test_move_within_one_gcs_store_is_a_server_side_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gcs = FakeGCSClient()
    monkeypatch.setattr(cas, "_bucket_stores", {})
    monkeypatch.setattr(cas, "_new_bucket_store", lambda scheme, bucket: GCSStore(bucket, client=gcs))
    astore = ArtifactStore(GCSStore("lab", "sk8s", client=gcs), "gs://lab/sk8s")
    monkeypatch.setattr(cas, "_artifact_stores", {("gs://lab/sk8s", ""): astore})
    src = tmp_path / "x.bam"
    src.write_bytes(DATA)
    art = astore.put_file(src, workflow="wf-1", job="j")
    monkeypatch.setattr(
        "tests.fakes_gcs.FakeBlob.upload_from_filename",
        lambda *a, **k: pytest.fail("bytes must not pass through the client"),
    )
    pub = art.publish("gs://lab/runs/s1.bam")
    assert gcs.rewrite_calls == 1
    assert gcs.objects[("lab", "runs/s1.bam")] == DATA
    assert gcs.meta[("lab", "runs/s1.bam")].metadata[META_SHA] == SHA
    assert ("lab", "sk8s/" + cas_key(SHA)) not in gcs.objects  # CAS key retired
    assert pub.uri == "gs://lab/runs/s1.bam"


def test_move_to_s3(world: dict[str, Any]) -> None:
    s3: FakeS3Client = world["s3"]
    world["art"].publish("s3://lab/out/s1.bam")
    assert s3.objects[("lab", "out/s1.bam")] == DATA
    assert s3.meta[("lab", "out/s1.bam")]["metadata"][META_SHA] == SHA


# --- copy=True --------------------------------------------------------------------------


def test_copy_keeps_both(world: dict[str, Any]) -> None:
    astore: ArtifactStore = world["astore"]
    art: Artifact = world["art"]
    pub = art.publish(dest_local(world), copy=True)
    assert astore.store.get(art.uri) == DATA and Path(dest_local(world)).read_bytes() == DATA
    idx = astore.read_index(SHA)
    assert idx is not None
    assert {l["kind"] for l in idx["locations"]} == {"cas", "published"}
    assert pub.uri == f"local:{dest_local(world)}"


# --- never overwrite ----------------------------------------------------------------------


def test_republishing_the_same_content_is_idempotent(world: dict[str, Any]) -> None:
    art: Artifact = world["art"]
    first = art.publish(dest_local(world), copy=True)
    again = art.publish(dest_local(world), copy=True)
    assert again.uri == first.uri
    again_move = art.publish(dest_local(world))  # a move onto itself: already there, retire the CAS
    assert again_move.uri == first.uri
    assert Path(dest_local(world)).read_bytes() == DATA


def test_publish_onto_different_content_is_refused(world: dict[str, Any]) -> None:
    dst = Path(dest_local(world))
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"someone else's file")
    with pytest.raises(PublishConflict):
        world["art"].publish(str(dst))
    assert dst.read_bytes() == b"someone else's file"
    assert world["astore"].store.stat(world["art"].uri) is not None  # source untouched


def test_publish_onto_a_different_published_artifact_is_refused(world: dict[str, Any], tmp_path: Path) -> None:
    other = tmp_path / "o.bam"
    other.write_bytes(b"another artifact")
    art2 = world["astore"].put_file(other, workflow="wf-1", job="j")
    world["art"].publish(dest_local(world))
    with pytest.raises(PublishConflict, match="different content"):
        art2.publish(dest_local(world))


def test_publish_to_gcs_existing_object_is_refused(world: dict[str, Any]) -> None:
    gcs: FakeGCSClient = world["gcs"]
    gcs.objects[("lab", "taken")] = b"x"
    with pytest.raises(PublishConflict):
        world["art"].publish("gs://lab/taken")
    assert gcs.objects[("lab", "taken")] == b"x"


def test_two_publishers_racing_for_one_destination(world: dict[str, Any]) -> None:
    art: Artifact = world["art"]
    gate = threading.Barrier(4)
    results: list[Any] = []

    def go() -> None:
        gate.wait()
        try:
            results.append(art.publish("gs://lab/runs/race.bam", copy=True))
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)

    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(isinstance(r, Artifact) for r in results), results  # same-content losers are no-ops
    assert world["gcs"].objects[("lab", "runs/race.bam")] == DATA


# --- verification and failure modes ---------------------------------------------------------


def test_publish_to_a_store_that_drops_metadata_refuses_to_retire_the_source(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = LocalStore.upload_file

    def drops(self: LocalStore, key: str, path: Path, *, metadata: Any = None, if_absent: bool = False) -> bool:
        return real(self, key, path, metadata=None, if_absent=if_absent)

    monkeypatch.setattr(LocalStore, "upload_file", drops)
    with pytest.raises(ArtifactError, match="metadata is missing"):
        world["art"].publish(dest_local(world))
    assert world["astore"].store.stat(world["art"].uri) is not None  # the CAS copy survives


def test_publish_streamed_copy_verifies_the_hash(world: dict[str, Any]) -> None:
    astore: ArtifactStore = world["astore"]
    art: Artifact = world["art"]
    astore.store.put(cas_key(SHA), b"the bytes of a BAM".upper())  # same size, corrupted
    with pytest.raises(ArtifactChanged):
        art.publish("gs://lab/runs/corrupt.bam")
    assert ("lab", "runs/corrupt.bam") not in world["gcs"].objects


def test_publish_missing_source(world: dict[str, Any]) -> None:
    world["astore"].store.delete(world["art"].uri)
    with pytest.raises(ArtifactNotFound):
        world["art"].publish(dest_local(world))


def test_publish_into_configmap_store_is_rejected(world: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="ConfigMap"):
        world["art"].publish("k8s://ns/x")


def test_publish_rejects_unknown_destination_scheme(world: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        world["art"].publish("ftp://x/y")


# --- external references ------------------------------------------------------------------


def test_publishing_an_external_ref_always_copies_and_hashes_it(world: dict[str, Any], tmp_path: Path) -> None:
    ext = tmp_path / "ext" / "in.fq"
    ext.parent.mkdir()
    ext.write_bytes(DATA)
    ref = artifact(str(ext))  # bare, unpinned, unhashed
    pub = ref.publish(dest_local(world, "pub/in.fq"))
    assert ext.read_bytes() == DATA  # never deleted: not ours
    assert pub.sha256 == SHA and Path(dest_local(world, "pub/in.fq")).read_bytes() == DATA
    st = LocalStore("/").stat(f"local:{dest_local(world, 'pub/in.fq')}")
    assert st is not None and st.metadata[META_SHA] == SHA and st.metadata[META_PRODUCED_BY] == "unknown"


def test_publishing_a_changed_external_ref_is_refused(world: dict[str, Any], tmp_path: Path) -> None:
    ext = tmp_path / "ext.fq"
    ext.write_bytes(DATA)
    from sk8s import staging

    pinned = staging.pin(world["astore"], artifact(str(ext)))
    ext.write_bytes(b"X" * len(DATA))
    with pytest.raises(ArtifactChanged):
        pinned.publish(dest_local(world))


# --- move bookkeeping across several publishes ----------------------------------------------------


def test_second_publish_after_a_move_copies_from_the_first_and_never_deletes_it(world: dict[str, Any]) -> None:
    art: Artifact = world["art"]
    first = art.publish(dest_local(world, "a/s1.bam"))
    second = art.publish(dest_local(world, "b/s1.bam"))  # the CAS copy is gone; source = first publish
    assert Path(dest_local(world, "a/s1.bam")).read_bytes() == DATA  # a published ref is never deleted
    assert Path(dest_local(world, "b/s1.bam")).read_bytes() == DATA
    idx = world["astore"].read_index(SHA)
    assert {l["uri"] for l in idx["locations"]} == {first.uri, second.uri}


# --- download ----------------------------------------------------------------------------------


def test_download_to_file_and_into_a_directory(world: dict[str, Any], tmp_path: Path) -> None:
    art: Artifact = world["art"]
    f = art.download(tmp_path / "x" / "y.bam")
    assert f == tmp_path / "x" / "y.bam" and f.read_bytes() == DATA
    d = tmp_path / "dir"
    d.mkdir()
    assert art.download(d) == d / "s1.bam"


def test_download_detects_corruption_and_leaves_nothing(world: dict[str, Any], tmp_path: Path) -> None:
    world["astore"].store.put(cas_key(SHA), DATA.upper())
    target = tmp_path / "got.bam"
    with pytest.raises(ArtifactChanged):
        world["art"].download(target)
    assert not target.exists() and not (tmp_path / "got.bam.sk8s-part").exists()


def test_download_of_a_bare_external_ref(tmp_path: Path) -> None:
    ext = tmp_path / "e.txt"
    ext.write_bytes(b"abc")
    assert artifact(str(ext)).download(tmp_path / "copy.txt").read_bytes() == b"abc"
    with pytest.raises(ArtifactNotFound):
        artifact(str(tmp_path / "ghost")).download(tmp_path / "x")


# --- review regressions ----------------------------------------------------------------------------


def test_a_failed_pinned_read_never_unlinks_devnull(tmp_path: Path) -> None:
    ext = tmp_path / "e.fq"
    ext.write_bytes(DATA)
    from sk8s import staging

    pinned = staging.pin(ArtifactStore(LocalStore(tmp_path / "s"), "local:x"), artifact(str(ext)))
    ext.write_bytes(b"Z" * len(DATA))
    with pytest.raises(ArtifactChanged):
        cas.fetch(LocalStore("/"), pinned.uri, Path(os.devnull), version=pinned.version, expected_sha=None)
    assert os.path.exists(os.devnull)


def test_publish_checks_an_asserted_hash_before_stamping_it(world: dict[str, Any], tmp_path: Path) -> None:
    ext = tmp_path / "e.fq"
    ext.write_bytes(DATA)
    liar = artifact(str(ext), sha256="0" * 64)
    with pytest.raises(ArtifactChanged, match="not what was pinned"):
        liar.publish("gs://lab/runs/liar.fq")
    assert ("lab", "runs/liar.fq") not in world["gcs"].objects


def test_uri_access_reuses_the_configured_stores_client() -> None:
    fake = FakeS3Client()
    hint = S3Store("cfg-bucket", "p", client=fake)
    other, key = cas.store_for_uri("s3://elsewhere/a/b", hint)
    assert other._client is fake and key == "a/b"  # carries the configured endpoint/credentials


# --- final-round regressions ---------------------------------------------------------------------------------


def test_a_move_onto_an_already_published_name_keeps_the_cas_copy(world: dict[str, Any]) -> None:
    """The destination was NOT written by this call (its sidecar label alone vouches for it), so
    the only verified copy must survive: the move degrades to copy=True."""
    astore: ArtifactStore = world["astore"]
    art: Artifact = world["art"]
    dst = dest_local(world)
    art.publish(dst, copy=True)  # earlier publish: both copies exist
    again = art.publish(dst)  # a "move" onto the name that already holds it
    assert again.uri == f"local:{dst}"
    assert astore.store.stat(art.uri) is not None  # the CAS copy is still there
    idx = astore.read_index(SHA)
    assert idx is not None and {loc["kind"] for loc in idx["locations"]} == {"cas", "published"}


def test_a_move_whose_destination_was_overwritten_in_place_cannot_lose_the_only_copy(world: dict[str, Any]) -> None:
    """The local-store sidecar outlives an overwrite; a same-size file there still 'matches' the label."""
    astore: ArtifactStore = world["astore"]
    art: Artifact = world["art"]
    dst = Path(dest_local(world))
    art.publish(str(dst), copy=True)
    dst.write_bytes(b"X" * len(DATA))  # same size, sidecar intact
    art.publish(str(dst))
    assert astore.store.get(art.uri) == DATA  # the CAS copy was not retired on the strength of the label


def test_staging_falls_back_to_the_next_location_on_a_hash_mismatch(world: dict[str, Any], tmp_path: Path) -> None:
    import dataclasses

    from sk8s import staging

    astore: ArtifactStore = world["astore"]
    art: Artifact = world["art"]
    dst = Path(dest_local(world))
    pub = art.publish(str(dst), copy=True)
    handle = dataclasses.replace(pub, version=None)  # a version-less handle at the published object
    dst.write_bytes(b"Y" * len(DATA))  # overwritten in place with SAME-size different bytes
    out = tmp_path / "staged"
    sha, size = staging.stage_artifact(astore, handle, out)
    assert sha == SHA and size == len(DATA) and out.read_bytes() == DATA  # served from the CAS copy
    assert handle.download(tmp_path / "dl").read_bytes() == DATA  # download() falls back too
    # every candidate bad -> ArtifactChanged is the final error, and nothing is left behind
    astore.store.put(cas_key(SHA), b"Z" * len(DATA))
    out2 = tmp_path / "staged2"
    with pytest.raises(ArtifactChanged):
        staging.stage_artifact(astore, handle, out2)
    assert not out2.exists()


def test_staging_reports_changed_not_missing_when_one_location_is_gone_and_one_is_wrong(
    world: dict[str, Any], tmp_path: Path
) -> None:
    import dataclasses

    from sk8s import staging

    astore: ArtifactStore = world["astore"]
    pub = world["art"].publish(dest_local(world), copy=True)
    handle = dataclasses.replace(pub, version=None)
    Path(dest_local(world)).write_bytes(b"Y" * len(DATA))
    astore.store.delete(world["art"].uri)  # the CAS copy is gone
    with pytest.raises(ArtifactChanged):
        staging.stage_artifact(astore, handle, tmp_path / "x")
