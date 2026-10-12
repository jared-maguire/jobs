"""Mark-and-sweep of the CAS (sk8s.gc) and its wiring into clean()."""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from sk8s import anchors, cas, cleaning, gc
from sk8s.cas import ArtifactStore, cas_key, index_key
from sk8s.store.gcs import GCSStore
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1
from tests.fakes_gcs import FakeGCSClient

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def age(path: Path, seconds: float) -> None:
    """Make a local file look ``seconds`` old relative to the real clock."""
    import os
    import time

    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "store"
    return {"astore": ArtifactStore(LocalStore(root), f"local:{root}"), "root": root, "tmp": tmp_path}


def make(env: dict[str, Any], name: str, data: bytes, *, old: bool = True, wf: str | None = None,
         job: str = "j") -> cas.Artifact:  # type: ignore[name-defined]
    f = env["tmp"] / "src" / name
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(data)
    art = env["astore"].put_file(f, workflow=wf, job=job)
    if old:
        age(env["root"] / cas_key(art.sha256), 3600)
        backdate_index(env, art.sha256, 3600)
    return art


def backdate_index(env: dict[str, Any], sha: str, seconds: float) -> None:
    store: LocalStore = env["astore"].store
    doc = json.loads(store.read_with_version(index_key(sha))[0])
    doc["last_referenced"] = (datetime.datetime.now(UTC) - datetime.timedelta(seconds=seconds)).isoformat()
    store.put(index_key(sha), json.dumps(doc).encode())


def alive(env: dict[str, Any], art: Any) -> bool:
    return bool(env["astore"].store.stat(art.uri))


# --- the sweep ------------------------------------------------------------------------


def test_unreferenced_old_object_is_collected_and_its_index_tidied(env: dict[str, Any]) -> None:
    art = make(env, "a", b"garbage")
    report = gc.sweep(env["astore"].store)
    assert report.deleted == [art.sha256] and report.deleted_bytes == 7 and report.aborted is None
    assert not alive(env, art)
    assert env["astore"].read_index(art.sha256) is None  # no location left: the entry goes too


def test_workflow_reference_set_keeps_an_object_alive(env: dict[str, Any]) -> None:
    kept = make(env, "a", b"kept", wf="wf-1", job="job-a")  # job shard
    also = make(env, "b", b"also", old=True)
    env["astore"].record_refs("wf-1", [also.sha256])  # workflow-level file
    gone = make(env, "c", b"gone")
    report = gc.sweep(env["astore"].store)
    assert report.deleted == [gone.sha256] and report.kept_reachable == 2
    assert alive(env, kept) and alive(env, also) and not alive(env, gone)


def test_young_objects_are_never_collected(env: dict[str, Any]) -> None:
    young = make(env, "a", b"fresh", old=False)
    report = gc.sweep(env["astore"].store)
    assert report.deleted == [] and report.kept_young == 1 and alive(env, young)


def test_recently_referenced_old_object_is_kept(env: dict[str, Any]) -> None:
    art = make(env, "a", b"reused")  # old bytes ...
    backdate_index(env, art.sha256, 5)  # ... adopted five seconds ago by a workflow still writing its manifest
    report = gc.sweep(env["astore"].store)
    assert report.deleted == [] and report.kept_young == 1 and alive(env, art)


def test_unknown_age_counts_as_young(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    art = make(env, "a", b"mystery")
    real = LocalStore.list_blobs

    def no_ages(self: LocalStore, prefix: str) -> Any:
        for info in real(self, prefix):
            yield type(info)(key=info.key, size=info.size, updated=None)

    monkeypatch.setattr(LocalStore, "list_blobs", no_ages)
    assert gc.sweep(env["astore"].store).deleted == [] and alive(env, art)


def test_published_copy_is_kept_on_purpose(env: dict[str, Any]) -> None:
    art = make(env, "a", b"published and kept")
    pub = env["tmp"] / "pub" / "a.bam"
    pub.parent.mkdir()
    env["astore"].update_index(art.sha256, add=[cas.location("published", f"local:{pub}")])
    report = gc.sweep(env["astore"].store)
    assert report.deleted == [] and report.kept_published == 1 and alive(env, art)


def test_fresh_memo_entry_roots_its_outputs_and_an_expired_one_does_not(env: dict[str, Any]) -> None:
    fresh_out, stale_out = make(env, "a", b"fresh out"), make(env, "b", b"stale out")
    store = env["astore"].store
    k1, k2 = hashlib.sha256(b"k1").hexdigest(), hashlib.sha256(b"k2").hexdigest()
    now = datetime.datetime.now(UTC)
    store.put(cas.memo_entry_key(k1), json.dumps({"created_at": now.isoformat(), "outputs": [fresh_out.to_dict()]}).encode())
    store.put(cas.memo_entry_key(k2), json.dumps({
        "created_at": (now - datetime.timedelta(days=30)).isoformat(), "outputs": [stale_out.to_dict()],
    }).encode())
    report = gc.sweep(store, memo_retention_days=7)
    assert report.deleted == [stale_out.sha256] and report.memo_expired == [k2]
    assert alive(env, fresh_out) and not alive(env, stale_out)
    assert store.stat(store.uri_for(cas.memo_entry_key(k1))) is not None
    assert store.stat(store.uri_for(cas.memo_entry_key(k2))) is None


def test_dry_run_reports_and_deletes_nothing(env: dict[str, Any]) -> None:
    art = make(env, "a", b"garbage")
    report = gc.sweep(env["astore"].store, dry_run=True)
    assert report.deleted == [art.sha256] and report.dry_run and alive(env, art)
    assert "would delete" in report.summary()


# --- when in doubt, don't delete --------------------------------------------------------


@pytest.mark.parametrize("key,body", [
    ("workflows/wf-1/artifacts.json", b"{not json"),
    ("workflows/wf-1/artifacts.json", b"[1,2]"),
    ("workflows/wf-1/artifacts.json", b'{"refs": ["not-a-hash"]}'),
    ("workflows/wf-1/artifacts.json", b'{"refs": "abc"}'),
    (cas.memo_entry_key(hashlib.sha256(b"m").hexdigest()), b"garbled"),
    (cas.memo_entry_key(hashlib.sha256(b"m").hexdigest()), b'{"outputs": []}'),  # no created_at
])
def test_any_unreadable_root_aborts_before_the_first_delete(env: dict[str, Any], key: str, body: bytes) -> None:
    art = make(env, "a", b"garbage that WOULD be collected")
    env["astore"].store.put(key, body)
    report = gc.sweep(env["astore"].store)
    assert report.aborted and report.deleted == [] and alive(env, art)
    assert "nothing deleted" in report.summary()


def test_listing_failure_aborts_without_raising(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    art = make(env, "a", b"garbage")

    def boom(self: LocalStore, prefix: str) -> Any:
        raise OSError("store down")

    monkeypatch.setattr(LocalStore, "list_blobs", boom)
    report = gc.sweep(env["astore"].store)
    assert report.aborted and "store down" in report.aborted and alive(env, art)


def test_non_hash_keys_in_the_cas_are_ignored(env: dict[str, Any]) -> None:
    env["astore"].store.put("artifacts/cas/sha256/ab/not-a-hash", b"?")
    age(env["root"] / "artifacts/cas/sha256/ab/not-a-hash", 3600)
    report = gc.sweep(env["astore"].store)
    assert report.deleted == [] and (env["root"] / "artifacts/cas/sha256/ab/not-a-hash").exists()


def test_a_workflow_adopting_an_object_mid_sweep_wins(env: dict[str, Any]) -> None:
    """Between the candidate scan and the deletes, a workflow records the hash: the re-mark saves it."""
    art = make(env, "a", b"contended")
    other = ArtifactStore(env["astore"].store, env["astore"].spec)

    class Racy(LocalStore):
        fired = False

        def get(self, uri: str) -> bytes:
            data = super().get(uri)
            if uri.endswith(f"{art.sha256}.json") and not self.fired:  # the candidate's index is read
                self.fired = True
                ArtifactStore(self, env["astore"].spec).record_refs("wf-late", [art.sha256])
            return data

    racy = Racy(env["root"])
    report = gc.sweep(racy)
    assert racy.fired and report.deleted == [] and report.kept_reachable == 1 and alive(env, art)
    del other


def test_sweep_on_gcs_fake_uses_prefix_listing(env: dict[str, Any]) -> None:
    fake = FakeGCSClient(page_size=2)
    astore = ArtifactStore(GCSStore("bkt", "sk8s", client=fake), "gs://bkt/sk8s")
    keep_src = env["tmp"] / "k"
    keep_src.write_bytes(b"kept")
    kept = astore.put_file(keep_src, workflow="wf-1", job="j")
    arts = []
    for i in range(5):
        f = env["tmp"] / f"g{i}"
        f.write_bytes(f"garbage {i}".encode())
        arts.append(astore.put_file(f))
    old = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    for meta in fake.meta.values():
        meta.updated = old
    for a in arts + [kept]:
        idx = astore.read_index(a.sha256 or "")
        assert idx is not None
        idx["last_referenced"] = old.isoformat()
        astore.store.put(index_key(a.sha256 or ""), json.dumps(idx).encode())
    report = gc.sweep(astore.store)
    assert sorted(report.deleted) == sorted(a.sha256 or "" for a in arts)
    assert astore.store.stat(kept.uri) is not None and report.kept_reachable == 1


# --- clean() wiring -----------------------------------------------------------------------


NS = "ns1"


def _anchor(core: FakeCoreV1, wf: str, store: str, days_old: float = 30.0) -> None:
    anchors.create_anchor(core, namespace=NS, workflow_id=wf, store=store, store_prefix=wf,
                          retention_days=7, detached=False)
    cm = core.configmaps[(NS, anchors.anchor_name(wf))]
    cm.metadata.annotations[anchors.CREATED_AT] = (
        datetime.datetime.now(UTC) - datetime.timedelta(days=days_old)
    ).isoformat()


def test_deleting_an_expired_workflow_makes_its_artifacts_collectable(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    core = FakeCoreV1()
    store: LocalStore = env["astore"].store
    art = make(env, "a", b"workflow output", wf="wf-old", job="j")
    env["astore"].record_refs("wf-old", [art.sha256])
    backdate_index(env, art.sha256, 3600)  # recording touched it; let it age again
    _anchor(core, "wf-old", env["astore"].spec)
    report = gc.SweepReport()
    deleted = cleaning.clean(NS, core_api=core, batch_api=FakeBatchV1(), store=store, report=report)
    assert deleted == ["wf-old"]
    assert store.stat(store.uri_for("workflows/wf-old/artifacts.json")) is None  # roots went with it
    assert store.stat(store.uri_for("workflows/wf-old/j/artifacts.json")) is None
    # ... and the same clean() collected the now-unreachable (and old) CAS object
    assert report.deleted == [art.sha256] and not alive(env, art)


def test_a_live_workflows_artifacts_survive_clean(env: dict[str, Any]) -> None:
    core = FakeCoreV1()
    store: LocalStore = env["astore"].store
    art = make(env, "a", b"workflow output", wf="wf-live", job="j")
    _anchor(core, "wf-live", env["astore"].spec, days_old=0.0)
    report = gc.SweepReport()
    assert cleaning.clean(NS, core_api=core, batch_api=FakeBatchV1(), store=store, report=report) == []
    assert report.deleted == [] and alive(env, art)


def test_clean_artifacts_false_skips_the_sweep(env: dict[str, Any]) -> None:
    art = make(env, "a", b"garbage")
    cleaning.clean(NS, core_api=FakeCoreV1(), batch_api=FakeBatchV1(), store=env["astore"].store, artifacts=False)
    assert alive(env, art)
    cleaning.clean(NS, core_api=FakeCoreV1(), batch_api=FakeBatchV1(), store=env["astore"].store)
    assert not alive(env, art)


def test_clean_survives_a_sweep_that_cannot_list(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from sk8s.store.base import BlobStore

    class NoList(LocalStore):
        def list_blobs(self, prefix: str) -> Any:
            raise OSError("down")

    assert cleaning.clean(NS, core_api=FakeCoreV1(), batch_api=FakeBatchV1(), store=NoList(env["root"])) == []


def test_worker_startup_sweep_never_touches_the_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    import sk8s.worker as worker

    seen: dict[str, Any] = {}
    monkeypatch.setattr("sk8s.cleaning.clean", lambda *a, **k: seen.update(k))
    worker._sweep({"SK8S_RESULT_KEY": "wf/j/result", "SK8S_NAMESPACE": "ns", "SK8S_WORKFLOW_ID": "wf"})
    assert seen["artifacts"] is False


@pytest.mark.parametrize("listing", ["ascending", "descending"])
def test_an_object_adopted_during_the_delete_loop_is_spared(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    """Whichever object the sweep deletes FIRST, a workflow adopts the OTHER one right after: the
    per-delete index re-read (touch-before-record) must spare it.

    (An earlier version hard-wired "the second in sorted key order is the victim" and so assumed
    the store lists in that order; os.walk order is filesystem-defined, CI's differed, and the
    victim was the one deleted first - a test bug, not a sweep bug. The listing order is now
    forced both ways and the victim is picked at run time.)"""
    a, b = make(env, "a", b"first garbage"), make(env, "b", b"second garbage")
    shas = {a.sha256, b.sha256}
    real = LocalStore.list_blobs

    def ordered(self: LocalStore, prefix: str) -> Any:
        return iter(sorted(real(self, prefix), key=lambda i: i.key, reverse=listing == "descending"))

    monkeypatch.setattr(LocalStore, "list_blobs", ordered)

    class Racy(LocalStore):
        deleted_first: str | None = None
        victim: str | None = None

        def delete(self, uri: str) -> None:
            super().delete(uri)
            gone = next((s for s in shas if s and uri.endswith(s)), None)
            if gone and self.deleted_first is None:
                self.deleted_first = gone
                self.victim = next(s for s in shas if s != gone)  # adopted right after the first delete
                ArtifactStore(self, env["astore"].spec).update_index(self.victim, touch=True)

    racy = Racy(env["root"])
    report = gc.sweep(racy)
    assert racy.deleted_first and racy.victim
    assert report.deleted == [racy.deleted_first]  # the first went; the adopted one did not
    assert racy.victim not in report.deleted and report.kept_young == 1
    assert env["astore"].store.stat(env["astore"].store.uri_for(cas_key(racy.victim))) is not None
