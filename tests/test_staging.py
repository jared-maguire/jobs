"""Staging: artifact args become Paths, OutDir outputs become Artifacts (worker run in-process)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import sk8s
from sk8s import serialize, staging, worker
from sk8s.artifacts import Artifact, OutDir, artifact
from sk8s.cas import ArtifactStore, cas_key, index_key
from sk8s.errors import ArtifactChanged, ArtifactError, ArtifactNotFound
from sk8s.store.local import LocalStore

H5 = hashlib.sha256(b"hello").hexdigest()


@pytest.fixture
def astore(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(LocalStore(tmp_path / "store"), f"local:{tmp_path / 'store'}")


def ext_file(tmp_path: Path, name: str = "s1.fq", data: bytes = b"hello") -> Path:
    p = tmp_path / "ext" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    return p


def run_worker(astore: ArtifactStore, func: Any, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None,
               wf: str = "wf-1", job: str = "job-a") -> tuple[int, bytes]:
    store = astore.store
    prepared = staging.prepare_submission(astore, wf, args, kwargs or {})
    uri = store.put(f"{wf}/{job}/payload", serialize.encode_payload(func, prepared.args, prepared.kwargs, extra_header=prepared.header))
    env = {"SK8S_STORE": astore.spec, "SK8S_PAYLOAD_URI": uri, "SK8S_RESULT_KEY": f"{wf}/{job}/result"}
    code = worker.main(env)
    return code, store.get(store.uri_for(f"{wf}/{job}/result"))


# --- tree walking ---------------------------------------------------------------------


def test_find_artifacts_walks_containers_in_order_deduped() -> None:
    a, b = artifact("gs://b/a"), artifact("gs://b/b")
    assert staging.find_artifacts(((a, [b, {"k": a}]), {"x": (b,)})) == [a, b]
    assert staging.find_artifacts(("plain", 3, [1, 2])) == []


def test_only_plain_containers_are_walked_consistently() -> None:
    import collections

    a = artifact("gs://b/a")
    Pair = collections.namedtuple("Pair", "x y")  # noqa: PYI024
    assert staging.find_artifacts([a, {a}, frozenset({a})]) == [a]
    assert staging.find_artifacts(Pair(a, 1)) == []  # not walked ...
    assert staging.map_tree(Pair(a, 1), lambda x: "P", Artifact) == Pair(a, 1)  # ... and not rebuilt
    assert staging.map_tree({a, 1}, lambda x: "P", Artifact) == {"P", 1}


def test_map_tree_rebuilds_without_touching_other_values() -> None:
    a = artifact("gs://b/a")
    out = staging.map_tree((a, [a, "s", 3], {"k": a, "n": None}), lambda x: "P", Artifact)
    assert out == ("P", ["P", "s", 3], {"k": "P", "n": None})


# --- submit side ----------------------------------------------------------------------


def test_prepare_without_artifacts_is_a_noop(astore: ArtifactStore) -> None:
    p = staging.prepare_submission(astore, "wf-1", (1, 2), {"k": 3})
    assert p.args == (1, 2) and p.kwargs == {"k": 3} and p.header == {} and p.ephemeral_bytes == 0
    assert list(astore.store.list_blobs("")) == []  # touched no store


def test_prepare_pins_external_refs_and_records_intent(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path)
    a = artifact(str(f))
    p = staging.prepare_submission(astore, "wf-1", (a,), {})
    pinned = p.args[0]
    assert pinned.version and pinned.size == 5 and pinned.sha256 is None
    assert p.header["artifacts"] == [pinned.to_dict()] and p.ephemeral_bytes == 5


def test_prepare_records_cas_inputs_in_the_workflow_set(astore: ArtifactStore, tmp_path: Path) -> None:
    made = astore.put_file(ext_file(tmp_path))
    staging.prepare_submission(astore, "wf-2", (), {"fq": made})
    doc = json.loads(astore.store.read_with_version("workflows/wf-2/artifacts.json")[0])
    assert doc["refs"] == [H5]


def test_prepare_missing_external_fails_before_anything_is_submitted(astore: ArtifactStore) -> None:
    with pytest.raises(ArtifactNotFound, match="does not exist"):
        staging.prepare_submission(astore, "wf-1", (artifact("/no/such/file.fq"),), {})


def test_prepare_size_assertion(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path)
    with pytest.raises(ArtifactChanged, match="expected 99"):
        staging.prepare_submission(astore, "wf-1", (artifact(str(f), size=99),), {})


@pytest.mark.parametrize("n,want", [
    (0, None), (1, "1024Mi"), (1024**2, "1024Mi"),  # tiny inputs: the 1Gi floor
    (512 * 1024**2, "1024Mi"), (512 * 1024**2 + 1, "1025Mi"),  # 2x crosses the floor
    (10 * 1024**3, "20480Mi"),
])
def test_ephemeral_request_is_twice_the_inputs_rounded_up_with_a_floor(n: int, want: str | None) -> None:
    assert staging.ephemeral_request(n) == want


# --- stage_artifact -------------------------------------------------------------------


def test_stage_cas_artifact_verifies_the_hash(astore: ArtifactStore, tmp_path: Path) -> None:
    made = astore.put_file(ext_file(tmp_path))
    dest = tmp_path / "scratch" / "x"
    assert staging.stage_artifact(astore, made, dest) == (H5, 5)
    assert dest.read_bytes() == b"hello"


def test_stage_cas_artifact_with_corrupted_bytes_is_refused(astore: ArtifactStore, tmp_path: Path) -> None:
    made = astore.put_file(ext_file(tmp_path))
    astore.store.put(cas_key(H5), b"HELLO")  # same size, different bytes
    with pytest.raises(ArtifactChanged, match="not what was pinned"):
        staging.stage_artifact(astore, made, tmp_path / "scratch" / "x")
    assert not (tmp_path / "scratch" / "x").exists()


def test_stage_external_learns_and_indexes_the_hash(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path)
    pinned = staging.pin(astore, artifact(str(f)))
    sha, size = staging.stage_artifact(astore, pinned, tmp_path / "scratch" / "x")
    assert (sha, size) == (H5, 5)
    idx = astore.read_index(H5)
    assert idx is not None
    assert [(l["kind"], l["uri"]) for l in idx["locations"]] == [("external", f"local:{f}")]


def test_stage_external_overwritten_after_pin_is_changed(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path)
    pinned = staging.pin(astore, artifact(str(f)))
    f.write_bytes(b"world")  # in-place overwrite: same size, new version
    with pytest.raises(ArtifactChanged, match="overwritten"):
        staging.stage_artifact(astore, pinned, tmp_path / "scratch" / "x")


def test_stage_external_with_wrong_expected_hash(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path)
    with pytest.raises(ArtifactChanged, match="hashes to"):
        staging.stage_artifact(astore, staging.pin(astore, artifact(str(f), sha256="0" * 64)), tmp_path / "x")


def test_stage_missing_everywhere(astore: ArtifactStore, tmp_path: Path) -> None:
    made = astore.put_file(ext_file(tmp_path))
    astore.store.delete(made.uri)
    with pytest.raises(ArtifactNotFound, match="not available anywhere"):
        staging.stage_artifact(astore, made, tmp_path / "x")


# --- OutDir detection -----------------------------------------------------------------


def test_outdir_params_finds_annotated_parameters() -> None:
    def f(fq: Path, out: OutDir, n: int = 1, *, other: sk8s.OutDir) -> None: ...

    assert staging.outdir_params(f) == ["out", "other"]
    assert staging.outdir_params(lambda x: x) == []
    assert staging.outdir_params(len) == []  # builtins have no usable signature


def test_outdir_params_survives_a_pickle_roundtrip_with_string_annotations(tmp_path: Path) -> None:
    import cloudpickle

    def f(fq, out: sk8s.OutDir):  # type: ignore[no-untyped-def]
        return out / "x"

    g = cloudpickle.loads(cloudpickle.dumps(f))
    assert staging.outdir_params(g) == ["out"]


# --- collect_outputs ------------------------------------------------------------------


def make_out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def test_collect_single_path(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_out(tmp_path)
    (out / "s1.bam").write_bytes(b"hello")
    value, produced = staging.collect_outputs(out / "s1.bam", out, astore, workflow="wf-1", job="j", inputs=["a" * 64])
    assert isinstance(value, Artifact) and value.sha256 == H5 and value.name == "s1.bam"
    assert produced == [value]
    idx = astore.read_index(H5)
    assert idx is not None and idx["provenance"]["inputs"] == ["a" * 64]


def test_collect_nested_structures_and_passthrough(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_out(tmp_path)
    (out / "a.txt").write_bytes(b"hello")
    (out / "b.txt").write_bytes(b"other")
    ext = artifact("gs://b/already")
    value, produced = staging.collect_outputs(
        {"x": [out / "a.txt", (out / "b.txt", 3)], "keep": ext, "s": "str", "n": None},
        out, astore, workflow=None, job=None, inputs=[],
    )
    assert isinstance(value["x"][0], Artifact) and value["x"][1][1] == 3
    assert value["keep"] is ext and value["s"] == "str" and len(produced) == 2


def test_collect_relative_paths_only_with_an_outdir(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_out(tmp_path)
    (out / "r.txt").write_bytes(b"hello")
    rel, _ = staging.collect_outputs(Path("r.txt"), out, astore, workflow=None, job=None, inputs=[])
    assert isinstance(rel, Artifact)
    plain, produced = staging.collect_outputs(Path("r.txt"), out, astore, workflow=None, job=None, inputs=[], relative_ok=False)
    assert plain == Path("r.txt") and produced == []


def test_collect_outdir_and_directories_become_dicts(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_out(tmp_path)
    (out / "sub").mkdir()
    (out / "sub" / "x.txt").write_bytes(b"hello")
    (out / "top.txt").write_bytes(b"other")
    whole, _ = staging.collect_outputs(OutDir(out), out, astore, workflow=None, job=None, inputs=[])
    assert sorted(whole) == ["sub/x.txt", "top.txt"] and whole["sub/x.txt"].sha256 == H5
    sub, _ = staging.collect_outputs(out / "sub", out, astore, workflow=None, job=None, inputs=[])
    assert list(sub) == ["x.txt"]


def test_collect_path_outside_outdir_is_data(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_out(tmp_path)
    elsewhere = tmp_path / "ref.fa"
    elsewhere.write_bytes(b"x")
    value, produced = staging.collect_outputs(elsewhere, out, astore, workflow=None, job=None, inputs=[])
    assert value == elsewhere and produced == []
    sneaky, produced = staging.collect_outputs(out / ".." / "ref.fa", out, astore, workflow=None, job=None, inputs=[])
    assert produced == []  # a traversal does not escape the OutDir into the CAS


def test_collect_missing_declared_output_is_an_error(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_out(tmp_path)
    with pytest.raises(ArtifactError, match="does not exist"):
        staging.collect_outputs(out / "never-written.bam", out, astore, workflow=None, job=None, inputs=[])


# --- the worker, end to end (in-process) -------------------------------------------------


def test_worker_stages_input_and_collects_output(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path, "s1.fq.gz", b"hello")

    def align(fq: Path, out: sk8s.OutDir) -> Any:
        assert isinstance(fq, Path) and fq.name == "s1.fq.gz"  # real POSIX path, original name
        (out / "s1.bam").write_bytes(fq.read_bytes().upper())
        return out / "s1.bam"

    code, blob = run_worker(astore, align, (artifact(str(f)),))
    assert code == 0
    result = serialize.decode_result(blob)
    assert isinstance(result, Artifact) and result.size == 5
    assert astore.store.get(result.uri) == b"HELLO"
    assert result.produced_by == "wf-1/job-a" and result.store == astore.spec
    header = json.loads(blob.split(b"\n", 1)[0])
    assert header["artifacts"] == [result.to_dict()]
    # provenance: the output's index names the input's hash; the input's index names its source
    idx = astore.read_index(result.sha256 or "")
    assert idx is not None and idx["provenance"]["inputs"] == [H5]
    in_idx = astore.read_index(H5)
    assert in_idx is not None and in_idx["locations"][0]["kind"] == "external"
    # the job shard recorded the output hash BEFORE it was needed (intent first)
    shard = json.loads(astore.store.read_with_version("workflows/wf-1/job-a/artifacts.json")[0])
    assert shard["refs"] == [result.sha256]


def test_worker_cleans_up_scratch(astore: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratchbase"
    monkeypatch.setenv("SK8S_SCRATCH", str(scratch))
    seen: list[Path] = []

    def fn(fq: Path, out: sk8s.OutDir) -> int:
        seen.append(fq)
        return 1

    store = astore.store
    f = ext_file(tmp_path)
    prepared = staging.prepare_submission(astore, "wf-1", (artifact(str(f)),), {})
    uri = store.put("wf-1/j/payload", serialize.encode_payload(fn, prepared.args, prepared.kwargs))
    env = {"SK8S_STORE": astore.spec, "SK8S_PAYLOAD_URI": uri, "SK8S_RESULT_KEY": "wf-1/j/result", "SK8S_SCRATCH": str(scratch)}
    assert worker.main(env) == 0
    assert list(scratch.iterdir()) == []


def test_worker_function_without_artifacts_touches_no_artifact_code(astore: ArtifactStore) -> None:
    code, blob = run_worker(astore, lambda x: x + 1, (1,))
    assert code == 0 and serialize.decode_result(blob) == 2
    assert [i.key for i in astore.store.list_blobs("artifacts/")] == []
    assert [i.key for i in astore.store.list_blobs("workflows/")] == []


def test_worker_failed_stage_becomes_a_typed_error_result(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path)
    store = astore.store
    pinned = staging.pin(astore, artifact(str(f)))
    f.write_bytes(b"CHANGED")  # overwritten between submit and the pod's start
    uri = store.put("wf-1/j/payload", serialize.encode_payload(lambda fq: 1, (pinned,), {}))
    env = {"SK8S_STORE": astore.spec, "SK8S_PAYLOAD_URI": uri, "SK8S_RESULT_KEY": "wf-1/j/result"}
    assert worker.main(env) == 1
    with pytest.raises(ArtifactChanged):
        serialize.decode_result(store.get(store.uri_for("wf-1/j/result")))


def test_worker_user_outdir_argument_is_respected(astore: ArtifactStore, tmp_path: Path) -> None:
    mine = tmp_path / "mine"
    mine.mkdir()

    def fn(out: sk8s.OutDir) -> Path:
        return out / "x"

    code, blob = run_worker(astore, fn, (OutDir(mine),))
    # the explicit OutDir was used as-is (no scratch dir injected); the returned Path is not in
    # the worker's scratch OutDir, so it is data, not an output
    assert code == 0 and serialize.decode_result(blob) == mine / "x"


def test_worker_function_returning_artifacts_unchanged(astore: ArtifactStore, tmp_path: Path) -> None:
    f = ext_file(tmp_path)
    ref = artifact(str(f))
    code, blob = run_worker(astore, lambda: ref)
    assert code == 0 and serialize.decode_result(blob) == ref
