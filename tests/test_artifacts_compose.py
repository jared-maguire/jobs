"""How the artifact layer composes with envelope v2 (expect=), dry_run and the fan-out routing."""
from __future__ import annotations

import json
import queue
from pathlib import Path
from typing import Any, Iterator

import pytest

import sk8s
from sk8s import api, memo, serialize, staging, worker
from sk8s.artifacts import Artifact, artifact
from sk8s.cas import ArtifactStore
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import ExpectationFailed, VersionMismatch
from sk8s.store.base import BlobStore
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1


# --- the wire envelope ----------------------------------------------------------------------------


def f(x: int) -> int:
    return x


def gate(r: Any) -> bool:
    return True


def test_extra_header_composes_with_expect_and_keeps_the_body_byte_identical() -> None:
    plain = serialize.encode_payload(f, (1,), {})
    with_header = serialize.encode_payload(f, (1,), {}, extra_header={"artifacts": [{"uri": "u"}]})
    assert plain.partition(b"\n")[2] == with_header.partition(b"\n")[2]  # the body never changes
    header = json.loads(with_header.partition(b"\n")[0])
    assert header["envelope"] == serialize.ENVELOPE_VERSION == 2 and header["artifacts"] == [{"uri": "u"}]
    both = serialize.encode_payload(f, (1,), {}, expect=gate, extra_header={"artifacts": []})
    bh = json.loads(both.partition(b"\n")[0])
    assert bh["expect"] is True and bh["envelope"] == 2 and "artifacts" in bh
    func, args, kwargs, expect = serialize.decode_payload_full(both)
    assert (args, kwargs) == ((1,), {}) and expect is not None


def test_an_artifact_free_payload_has_no_artifact_header() -> None:
    assert "artifacts" not in json.loads(serialize.encode_payload(f, (1,), {}).partition(b"\n")[0])


def test_a_newer_envelope_is_still_refused_with_artifact_keys_present() -> None:
    blob = serialize.encode_payload(f, (1,), {}, extra_header={"artifacts": []})
    head, _, body = blob.partition(b"\n")
    h = json.loads(head)
    h["envelope"] = serialize.ENVELOPE_VERSION + 1
    with pytest.raises(VersionMismatch):
        serialize.decode_payload_full(json.dumps(h).encode() + b"\n" + body)


def test_result_header_artifacts_do_not_disturb_old_readers() -> None:
    a = Artifact(uri="gs://b/x", sha256="ab" * 32)
    blob = serialize.encode_result({"k": a}, [a.to_dict()])
    assert json.loads(blob.partition(b"\n")[0])["artifacts"] == [a.to_dict()]
    assert serialize.decode_result(blob) == {"k": a}
    assert serialize.load_result_value(blob) == {"k": a}
    assert "artifacts" not in json.loads(serialize.encode_result(1).partition(b"\n")[0])


# --- the worker: gate FIRST, then upload ------------------------------------------------------------


@pytest.fixture
def astore(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(LocalStore(tmp_path / "store"), f"local:{tmp_path / 'store'}")


def run_worker(astore: ArtifactStore, func: Any, args: tuple[Any, ...] = (), *, expect: Any = None) -> tuple[int, bytes]:
    prepared = staging.prepare_submission(astore, "wf-1", args, {})
    store = astore.store
    uri = store.put("wf-1/job-a/payload", serialize.encode_payload(
        func, prepared.args, prepared.kwargs, expect=expect, extra_header=prepared.header))
    env = {"SK8S_STORE": astore.spec, "SK8S_PAYLOAD_URI": uri, "SK8S_RESULT_KEY": "wf-1/job-a/result"}
    code = worker.main(env)
    return code, store.get(store.uri_for("wf-1/job-a/result"))


def make_output(out: sk8s.OutDir) -> Path:
    (out / "o.txt").write_text("hello")
    return out / "o.txt"


def test_a_gated_out_result_uploads_and_registers_nothing(astore: ArtifactStore, tmp_path: Path) -> None:
    marker = tmp_path / "gate-saw"  # the gate is pickled into the payload: report through a file

    def reject(value: Path) -> str:
        marker.write_text(value.read_text())  # the gate sees the REAL file, still in scratch
        return "bad output"

    code, blob = run_worker(astore, make_output, expect=reject)
    assert code == 1 and marker.read_text() == "hello"
    with pytest.raises(ExpectationFailed):
        serialize.decode_result(blob)
    assert list(astore.store.list_blobs("artifacts/cas/")) == []  # nothing in the CAS
    assert list(astore.store.list_blobs("workflows/")) == []  # no reference-set entry
    assert list(astore.store.list_blobs("artifacts/index/")) == []


def test_a_passing_gate_then_uploads_and_the_client_sees_artifacts(astore: ArtifactStore, tmp_path: Path) -> None:
    marker = tmp_path / "gate-saw"

    def accept(value: Any) -> bool:
        marker.write_text(type(value).__name__)
        return True

    code, blob = run_worker(astore, make_output, expect=accept)
    result = serialize.decode_result(blob)
    assert code == 0 and marker.read_text() == "PosixPath" and isinstance(result, Artifact)
    assert astore.store.get(result.uri) == b"hello"


def test_a_gated_out_run_keeps_only_input_facts(astore: ArtifactStore, tmp_path: Path) -> None:
    ext = tmp_path / "in.txt"
    ext.write_text("input")

    def work(fq: Path, out: sk8s.OutDir) -> Path:
        (out / "o.txt").write_text("x")
        return out / "o.txt"

    code, _ = run_worker(astore, work, (artifact(str(ext)),), expect=lambda v: False)
    assert code == 1
    assert list(astore.store.list_blobs("artifacts/cas/")) == []  # no output, and inputs are not copied
    assert [i.key for i in astore.store.list_blobs("workflows/")] == []  # nothing beyond inputs recorded...
    idx = [i.key for i in astore.store.list_blobs("artifacts/index/")]
    assert len(idx) == 1  # ...except the staged input's learned hash (a fact about the input)


def test_an_unpicklable_result_fails_before_the_gate_and_any_upload(astore: ArtifactStore, tmp_path: Path) -> None:
    import threading

    marker = tmp_path / "gate-ran"

    def fn(out: sk8s.OutDir) -> Any:
        (out / "o.txt").write_text("x")
        return (out / "o.txt", threading.Lock())

    code, _ = run_worker(astore, fn, expect=lambda v: marker.write_text("ran"))
    assert code == 1 and not marker.exists() and list(astore.store.list_blobs("artifacts/cas/")) == []


# --- dry_run: a preview touches nothing ----------------------------------------------------------------


class Strict:
    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dry_run touched the {self._label}: {name}")


class StrictStore(BlobStore):
    """A store where ANY read or write fails (a hashing fetch, a pin stat, a memo lookup...)."""

    def __getattribute__(self, name: str) -> Any:
        if name in ("uri_for", "__class__", "__dict__"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"dry_run touched the store: {name}")

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str: ...  # pragma: no cover
    def get(self, uri: str) -> bytes: ...  # pragma: no cover
    def delete(self, uri: str) -> None: ...  # pragma: no cover
    def delete_prefix(self, prefix: str) -> None: ...  # pragma: no cover
    def uri_for(self, key: str) -> str:
        return f"strict://{key}"


def strict_cluster(**cfg: Any) -> Cluster:
    cfg.setdefault("image", "sk8s:dev")
    return Cluster(
        Config(namespace="ns1", **cfg), batch_api=Strict("batch API"), core_api=Strict("core API"),
        store=StrictStore(), watch=False,
    )


def container(body: dict[str, Any]) -> dict[str, Any]:
    c: dict[str, Any] = body["spec"]["template"]["spec"]["containers"][0]
    return c


def test_dry_run_with_artifact_args_pins_hashes_and_records_nothing() -> None:
    c = strict_cluster()
    sized = Artifact(uri="gs://bkt/never-stat-me.bam", size=700 * 1024**2, name="a.bam")
    unsized = artifact("gs://bkt/never-stat-me-either.fq")  # unpinned, unsized: a preview must not stat it
    dr = c.submit(f, sized, unsized, dry_run=True)
    assert isinstance(dr, sk8s.DryRun) and dr.n_artifacts == 2 and dr.artifact_input_bytes == 700 * 1024**2
    # ephemeral-storage reflects the DECLARED sizes: 2 x 700 MiB
    assert container(dr.jobs[0])["resources"]["requests"]["ephemeral-storage"] == "1400Mi"
    assert any("no declared size" in n for n in dr.notes)
    assert dr.payload_bytes > 0


def test_dry_run_without_sizes_marks_the_unknown_instead_of_guessing() -> None:
    dr = strict_cluster().submit(f, artifact("gs://bkt/x.fq"), dry_run=True)
    assert dr.n_artifacts == 1 and dr.artifact_input_bytes is None
    assert "resources" not in container(dr.jobs[0]) or not container(dr.jobs[0]).get("resources")
    assert any("1 input artifact(s) have no declared size" in n for n in dr.notes)


def test_dry_run_with_memo_never_consults_the_memo() -> None:
    dr = strict_cluster().submit(f, 1, dry_run=True, memo=True)  # StrictStore would fail a lookup
    assert any("never consults or writes the memo" in n for n in dr.notes)
    assert strict_cluster(memo=True).submit(f, 1, dry_run=True).n_jobs == 1


def test_dry_run_keeps_a_users_ephemeral_request() -> None:
    dr = strict_cluster().submit(
        f, Artifact(uri="gs://b/x", size=10**9), dry_run=True, requests={"ephemeral-storage": "9Gi"})
    assert container(dr.jobs[0])["resources"]["requests"]["ephemeral-storage"] == "9Gi"


def test_dry_run_to_dict_carries_the_staging_fields() -> None:
    d = strict_cluster().submit(f, Artifact(uri="gs://b/x", size=5), dry_run=True).to_dict()
    assert d["n_artifacts"] == 1 and d["artifact_input_bytes"] == 5


# --- the fan-out routing ---------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_api() -> Iterator[None]:
    sk8s.configure()
    yield
    sk8s.configure()


@pytest.mark.parametrize("fan", ["submit_map", "submit_starmap", "map", "starmap"])
@pytest.mark.parametrize("dry", [False, True])
def test_memo_with_batching_is_refused_before_anything_happens(fan: str, dry: bool) -> None:
    sk8s.configure(cluster=strict_cluster())  # a Strict cluster: refusal must precede any use
    items = [(1,), (2,)] if "star" in fan else [1, 2]
    with pytest.raises(ValueError, match="batch_size > 1"):
        getattr(api, fan)(f, items, batch_size=2, memo=True, **({"dry_run": True} if dry and fan.startswith("submit") else {}))


def test_memo_with_batching_is_refused_ahead_of_a_pilot_head() -> None:
    sk8s.configure(cluster=strict_cluster())
    with pytest.raises(ValueError, match="batch_size > 1"):
        api.submit_map(f, [1, 2, 3], batch_size=2, memo=True, pilot=1)


def test_map_dry_run_with_memo_previews_without_a_lookup() -> None:
    sk8s.configure(cluster=strict_cluster())
    dr = api.submit_map(f, [1, 2, 3], memo=True, dry_run=True)
    assert dr.n_jobs == 3 and dr.n_items == 3
