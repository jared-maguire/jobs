"""Memoization: key stability/sensitivity, entries, and the submit/resolve wiring."""
from __future__ import annotations

import base64
import functools
import json
import os
import queue
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Iterator

import pytest

import sk8s
from sk8s import cas, memo, serialize
from sk8s.artifacts import Artifact, artifact
from sk8s.cas import ArtifactStore
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.local import LocalCluster
from sk8s.store.local import LocalStore
from sk8s.watch import JobEvent
from tests.fakes import FakeBatchV1, FakeCoreV1

H = "ab" * 32


# --- canonical encoding ----------------------------------------------------------------


def test_canonical_distinguishes_types() -> None:
    values = [1, "1", True, 1.0, b"1", None, [1], (1,), {1}, {1: 1}, "", b"", [], {}]
    encoded = [memo.canonical(v) for v in values]
    assert len(set(encoded)) == len(values)


def test_canonical_ignores_dict_and_set_ordering() -> None:
    assert memo.canonical({"a": 1, "b": [2, 3]}) == memo.canonical({"b": [2, 3], "a": 1})
    assert memo.canonical({"x", "y", "z"}) == memo.canonical({"z", "y", "x"})
    assert memo.canonical(frozenset({1, 2})) == memo.canonical(frozenset({2, 1}))
    assert memo.canonical([1, 2]) != memo.canonical([2, 1])


def test_canonical_nested_and_length_prefixed_so_boundaries_cannot_collide() -> None:
    assert memo.canonical(["ab", "c"]) != memo.canonical(["a", "bc"])
    assert memo.canonical({"a": {"b": 1}}) != memo.canonical({"a": {"b": 2}})


def test_canonical_rejects_unknown_objects_but_the_pickle_fallback_takes_them() -> None:
    class Thing:
        def __init__(self) -> None:
            self.x = 1

    with pytest.raises(memo._Opaque):
        memo.canonical(Thing())
    assert memo.canonical_or_pickle(Thing()).startswith(b"P")


def test_unpicklable_argument_is_a_clear_error() -> None:
    import threading

    with pytest.raises(sk8s.Sk8sError, match="cannot compute a memo key"):
        memo.canonical_or_pickle(threading.Lock())


def test_artifact_identity_is_content_when_hashed_else_the_pin() -> None:
    assert memo.identity(Artifact(uri="gs://b/x", sha256=H)) == f"sha256:{H}"
    assert memo.identity(Artifact(uri="gs://b/x", version="77")) == "ext:gs://b/x@77"
    moved = Artifact(uri="gs://b/elsewhere/x", sha256=H, name="x")
    assert memo.canonical(Artifact(uri="gs://cas/x", sha256=H, name="x")) == memo.canonical(moved)
    assert memo.canonical(Artifact(uri="u", sha256=H, name="x")) != memo.canonical(Artifact(uri="u", sha256=H, name="y"))


# --- function fingerprints --------------------------------------------------------------------

THRESHOLD = 5


def _helper(x: int) -> int:
    return x + 1


def uses_global(x: int) -> int:
    return _helper(x) * THRESHOLD


def test_fingerprint_is_stable_and_sensitive_to_the_body() -> None:
    def f(x: int) -> int:
        return x + 1

    def g(x: int) -> int:
        return x + 2

    def f_again(x: int) -> int:
        return x + 1

    assert memo.function_fingerprint(f) == memo.function_fingerprint(f)
    assert memo.function_fingerprint(f) != memo.function_fingerprint(g)
    assert memo.function_fingerprint(f) != memo.function_fingerprint(f_again)  # different qualname


def test_fingerprint_ignores_line_numbers_and_filenames() -> None:
    src = "def f(x):\n    return x * 3\n"
    ns1: dict[str, Any] = {"__name__": "m"}
    ns2: dict[str, Any] = {"__name__": "m"}
    exec(compile(src, "/one/place.py", "exec"), ns1)  # noqa: S102
    exec(compile("\n\n\n" + src, "/another/file.py", "exec"), ns2)  # noqa: S102
    assert memo.function_fingerprint(ns1["f"]) == memo.function_fingerprint(ns2["f"])


def test_fingerprint_sees_constants_defaults_closures_and_global_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def make(k: int, d: int = 1) -> Any:
        def f(x: int = d) -> int:
            return x + k

        return f

    base = memo.function_fingerprint(make(1))
    assert memo.function_fingerprint(make(1)) == base
    assert memo.function_fingerprint(make(2)) != base  # closure value
    assert memo.function_fingerprint(make(1, d=2)) != base  # default
    before = memo.function_fingerprint(uses_global)
    monkeypatch.setitem(uses_global.__globals__, "THRESHOLD", 6)
    assert memo.function_fingerprint(uses_global) != before  # a referenced module constant


def test_fingerprint_follows_called_helper_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    before = memo.function_fingerprint(uses_global)

    def new_helper(x: int) -> int:
        return x + 100

    new_helper.__qualname__ = _helper.__qualname__
    monkeypatch.setitem(uses_global.__globals__, "_helper", new_helper)
    assert memo.function_fingerprint(uses_global) != before


def test_fingerprint_survives_recursion() -> None:
    def fact(n: int) -> int:
        return 1 if n < 2 else n * fact(n - 1)

    assert memo.function_fingerprint(fact) == memo.function_fingerprint(fact)


def test_fingerprint_of_partials_builtins_and_callables() -> None:
    def f(a: int, b: int) -> int:
        return a + b

    assert memo.function_fingerprint(functools.partial(f, 1)) != memo.function_fingerprint(functools.partial(f, 2))
    assert memo.function_fingerprint(len) == memo.function_fingerprint(len)
    assert memo.function_fingerprint(len) != memo.function_fingerprint(sum)

    class C:
        def __call__(self) -> int:
            return 1

    assert isinstance(memo.function_fingerprint(C()), str)


# --- keys ---------------------------------------------------------------------------------------


def key(func: Any = None, args: tuple[Any, ...] = (1,), kwargs: dict[str, Any] | None = None, **kw: Any) -> str:
    def default(x: int) -> int:
        return x

    params: dict[str, Any] = {"image": "img:1", **kw}
    return memo.memo_key(func or default, args, kwargs or {}, **params)


def test_key_is_deterministic_and_a_sha256() -> None:
    assert key() == key() and len(key()) == 64


@pytest.mark.parametrize("change", [
    {"image": "img:2"}, {"salt": "v2"}, {"args": (2,)}, {"kwargs": {"flag": True}},
    {"volumes": ["ref:/ref"]},
])
def test_key_changes_with_each_input(change: dict[str, Any]) -> None:
    assert key(**change) != key()


def test_key_changes_with_the_schema_version(monkeypatch: pytest.MonkeyPatch) -> None:
    before = key()
    monkeypatch.setattr(memo, "MEMO_SCHEMA", memo.MEMO_SCHEMA + 1)
    assert key() != before


def test_key_changes_with_the_python_version(monkeypatch: pytest.MonkeyPatch) -> None:
    before = key()
    monkeypatch.setattr(memo, "_python_minor", lambda: "3.99")
    assert key() != before


def test_key_ignores_kwarg_and_dict_order() -> None:
    assert key(args=({"a": 1, "b": 2},), kwargs={"p": 1, "q": 2}) == key(args=({"b": 2, "a": 1},), kwargs={"q": 2, "p": 1})


def test_key_depends_on_input_artifact_content_not_location() -> None:
    a1 = Artifact(uri="gs://b/cas/1", sha256="11" * 32, name="x.fq")
    a1_moved = Artifact(uri="gs://b/published/x.fq", sha256="11" * 32, name="x.fq")
    a2 = Artifact(uri="gs://b/cas/2", sha256="22" * 32, name="x.fq")
    assert key(args=(a1,)) == key(args=(a1_moved,))
    assert key(args=(a1,)) != key(args=(a2,))
    assert key(args=(a1, a2)) != key(args=(a2, a1))  # positions matter


def test_key_of_unhashed_external_uses_its_pin() -> None:
    pinned1 = Artifact(uri="gs://b/in.fq", version="100")
    pinned2 = Artifact(uri="gs://b/in.fq", version="101")  # overwritten in place
    assert key(args=(pinned1,)) != key(args=(pinned2,))


def test_key_is_stable_across_processes_and_hash_seeds(tmp_path: Path) -> None:
    script = tmp_path / "keyprobe.py"
    script.write_text(textwrap.dedent('''
        import sys
        from sk8s import memo

        THRESHOLD = 3

        def work(items, mode="x"):
            return [i * THRESHOLD for i in items]

        args = ({"banana", "apple", "cherry"}, {"k": frozenset({"a", "b"}), "j": [1, 2]})
        print(memo.memo_key(work, args, {"mode": "y"}, image="img:1", salt="s"))
    '''))
    out = set()
    for seed in ("0", "1", "12345", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": ":".join(sys.path)}
        out.add(subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, check=True).stdout.strip())
    assert len(out) == 1


# --- entries -----------------------------------------------------------------------------------------


@pytest.fixture
def astore(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(LocalStore(tmp_path / "store"), f"local:{tmp_path / 'store'}")


def make_art(astore: ArtifactStore, tmp_path: Path, data: bytes = b"output") -> Artifact:
    f = tmp_path / "o.bam"
    f.write_bytes(data)
    return astore.put_file(f, workflow="wf-1", job="j")


def test_lookup_miss_on_nothing(astore: ArtifactStore) -> None:
    assert memo.lookup(astore, "cd" * 32) is None


def test_write_then_lookup_roundtrip_with_artifacts(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_art(astore, tmp_path)
    value = {"bam": out, "n": 3, "tags": ["a", "b"]}
    assert memo.write(astore, "cd" * 32, value, function="align", produced_by="wf-1/j", image="img", inputs=[]) is True
    hit = memo.lookup(astore, "cd" * 32)
    assert hit is not None and hit.value == value and hit.outputs == [out]
    entry = json.loads(astore.store.read_with_version(cas.memo_entry_key("cd" * 32))[0])
    assert entry["function"] == "align" and entry["outputs"] == [out.to_dict()] and "created_at" in entry


def test_plain_results_memoize_too(astore: ArtifactStore) -> None:
    memo.write(astore, "cd" * 32, 42, function="f", produced_by=None, image="img", inputs=[])
    hit = memo.lookup(astore, "cd" * 32)
    assert hit is not None and hit.value == 42 and hit.outputs == []


def test_dangling_output_is_a_miss(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_art(astore, tmp_path)
    memo.write(astore, "cd" * 32, out, function="f", produced_by=None, image="img", inputs=[])
    astore.store.delete(out.uri)  # collected by the sweeper
    assert memo.lookup(astore, "cd" * 32) is None


def test_output_published_elsewhere_still_hits(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_art(astore, tmp_path)
    memo.write(astore, "cd" * 32, out, function="f", produced_by=None, image="img", inputs=[])
    out.publish(str(tmp_path / "pub" / "o.bam"))  # moves it: the CAS key is retired
    assert memo.lookup(astore, "cd" * 32) is not None


@pytest.mark.parametrize("body", [b"garbled", b"{}", b'{"schema": 99}', b'{"schema":1,"key":"x"}'])
def test_unreadable_entries_are_a_miss_not_a_crash(astore: ArtifactStore, body: bytes) -> None:
    astore.store.put(cas.memo_entry_key("cd" * 32), body)
    assert memo.lookup(astore, "cd" * 32) is None


def test_entry_for_another_key_is_ignored(astore: ArtifactStore) -> None:
    memo.write(astore, "cd" * 32, 1, function="f", produced_by=None, image="i", inputs=[])
    raw = astore.store.read_with_version(cas.memo_entry_key("cd" * 32))[0]
    astore.store.put(cas.memo_entry_key("ef" * 32), raw)  # copied under the wrong name
    assert memo.lookup(astore, "ef" * 32) is None


def test_oversized_result_is_not_memoized(astore: ArtifactStore, caplog: pytest.LogCaptureFixture) -> None:
    assert memo.write(astore, "cd" * 32, "x" * (memo.MAX_INLINE_RESULT + 1), function="f",
                      produced_by=None, image="i", inputs=[]) is False
    assert astore.store.stat(astore.store.uri_for(cas.memo_entry_key("cd" * 32))) is None
    assert "not memoizing" in caplog.text


def test_rewriting_the_same_result_keeps_the_original_timestamp(astore: ArtifactStore) -> None:
    memo.write(astore, "cd" * 32, 1, function="f", produced_by=None, image="i", inputs=[])
    first = astore.store.read_with_version(cas.memo_entry_key("cd" * 32))[0]
    memo.write(astore, "cd" * 32, 1, function="f", produced_by=None, image="i", inputs=[])
    assert astore.store.read_with_version(cas.memo_entry_key("cd" * 32))[0] == first
    memo.write(astore, "cd" * 32, 2, function="f", produced_by=None, image="i", inputs=[])  # a changed result replaces it
    hit = memo.lookup(astore, "cd" * 32)
    assert hit is not None and hit.value == 2


def test_adopt_outputs_records_them_in_the_workflow(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_art(astore, tmp_path)
    memo.adopt_outputs(astore, "wf-new", memo.MemoHit(value=out, key="k", outputs=[out]))
    doc = json.loads(astore.store.read_with_version("workflows/wf-new/artifacts.json")[0])
    assert doc["refs"] == [out.sha256]


# --- Cluster wiring (fake k8s) ----------------------------------------------------------------------

_OPEN: list[Cluster] = []


@pytest.fixture(autouse=True)
def _close_clusters() -> Iterator[None]:
    yield
    while _OPEN:
        _OPEN.pop().close()


class Harness:
    def __init__(self, tmp_path: Path, **cfg: Any) -> None:
        self.batch, self.core = FakeBatchV1(), FakeCoreV1()
        self.store = LocalStore(tmp_path / "store")
        self.events: queue.Queue[JobEvent] = queue.Queue()
        self.cluster = Cluster(
            Config(namespace="ns1", image="sk8s:dev", store=f"local:{self.store.root}", **cfg),
            batch_api=self.batch, core_api=self.core, store=self.store, watcher_events=self._iter,
        )
        _OPEN.append(self.cluster)

    def _iter(self) -> Iterator[JobEvent]:
        while True:
            yield self.events.get()

    def finish(self, job: str, phase: str = "succeeded") -> None:
        import sk8s.worker as worker

        spec = self.batch.jobs[("ns1", job)]
        env = {e.name: e.value for e in spec.spec.template.spec.containers[0].env}
        env["SK8S_STORE"] = f"local:{self.store.root}"
        worker.main(env)
        self.events.put((job, phase))

    def entries(self) -> list[str]:
        return [i.key for i in self.store.list_blobs("artifacts/memo/")]


def double(x: int) -> int:
    return x * 2


def test_memo_hit_skips_submission_entirely(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.cluster.submit(double, 21, memo=True)
    assert not first.memo_hit and len(h.batch.jobs) == 1
    h.finish(first.job_name)
    assert first.result(timeout=10) == 42 and len(h.entries()) == 1
    jobs_before = dict(h.batch.jobs)
    blobs_before = {i.key for i in h.store.list_blobs(f"{h.cluster.workflow_id}/")}
    second = h.cluster.submit(double, 21, memo=True)
    assert second.memo_hit and second.done() and second.result() == 42
    assert dict(h.batch.jobs) == jobs_before  # zero new jobs
    assert {i.key for i in h.store.list_blobs(f"{h.cluster.workflow_id}/")} == blobs_before  # not even a payload
    assert second.job_name.startswith("memo-") and second.logs() == "" and second.cancel() is False


def test_different_args_or_salt_miss(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    f1 = h.cluster.submit(double, 1, memo=True)
    h.finish(f1.job_name)
    f1.result(timeout=10)
    assert not h.cluster.submit(double, 2, memo=True).memo_hit
    assert not h.cluster.submit(double, 1, memo=True, memo_salt="v2").memo_hit
    assert h.cluster.submit(double, 1, memo=True).memo_hit


def test_memo_is_opt_in(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    a = h.cluster.submit(double, 5)
    h.finish(a.job_name)
    a.result(timeout=10)
    assert h.entries() == []  # nothing written without memo=True
    assert not h.cluster.submit(double, 5, memo=True).memo_hit  # and nothing to hit
    assert len(h.batch.jobs) == 2


def test_config_memo_turns_it_on_and_the_call_can_turn_it_off(tmp_path: Path) -> None:
    h = Harness(tmp_path, memo=True)
    a = h.cluster.submit(double, 5)
    h.finish(a.job_name)
    a.result(timeout=10)
    assert h.cluster.submit(double, 5).memo_hit
    assert not h.cluster.submit(double, 5, memo=False).memo_hit


def test_failed_runs_never_write_entries(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def boom(x: int) -> int:
        raise ValueError("no")

    fut = h.cluster.submit(boom, 1, memo=True)
    h.finish(fut.job_name, "failed")
    with pytest.raises(ValueError):
        fut.result(timeout=10)
    assert h.entries() == []
    assert not h.cluster.submit(boom, 1, memo=True).memo_hit  # so the retry really runs


def test_pod_failure_without_a_result_never_writes_an_entry(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    fut = h.cluster.submit(double, 3, memo=True)
    h.events.put((fut.job_name, "failed"))  # the pod died; no result blob
    with pytest.raises(sk8s.PodFailed):
        fut.result(timeout=30)
    assert h.entries() == []


def test_cancelled_jobs_are_never_recorded(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    fut = h.cluster.submit(double, 3, memo=True)
    assert fut.cancel() is True
    assert h.entries() == [] and h.cluster._memo_pending == {}


def test_a_write_failure_does_not_fail_the_successful_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path)
    monkeypatch.setattr(memo, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("store hiccup")))
    fut = h.cluster.submit(double, 3, memo=True)
    h.finish(fut.job_name)
    assert fut.result(timeout=10) == 6


def test_a_hit_with_artifacts_adopts_them_into_the_new_workflow(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    src = tmp_path / "in.txt"
    src.write_text("hello")

    def shout(fq: Path, out: sk8s.OutDir) -> Path:
        (out / "o.txt").write_text(fq.read_text().upper())
        return out / "o.txt"

    a = h.cluster.submit(shout, artifact(str(src)), memo=True)
    h.finish(a.job_name)
    produced = a.result(timeout=10)
    jobs = len(h.batch.jobs)
    # a different client / workflow asks the same question
    h2 = Harness.__new__(Harness)
    h2.batch, h2.core, h2.store, h2.events = FakeBatchV1(), FakeCoreV1(), h.store, queue.Queue()
    h2.cluster = Cluster(
        Config(namespace="ns1", image="sk8s:dev", store=f"local:{h.store.root}"),
        batch_api=h2.batch, core_api=h2.core, store=h2.store, watcher_events=h2._iter,
    )
    _OPEN.append(h2.cluster)
    again = h2.cluster.submit(shout, artifact(str(src)), memo=True)
    assert again.memo_hit and again.result() == produced and len(h.batch.jobs) == jobs and not h2.batch.jobs
    doc = json.loads(h.store.read_with_version(f"workflows/{h2.cluster.workflow_id}/artifacts.json")[0])
    assert doc["refs"] == [produced.sha256]


def test_overwriting_an_input_in_place_changes_the_key_so_it_misses(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    src = tmp_path / "in.txt"
    src.write_text("v1")

    def size(fq: Path) -> int:
        return fq.stat().st_size

    a = h.cluster.submit(size, artifact(str(src)), memo=True)
    h.finish(a.job_name)
    a.result(timeout=10)
    assert h.cluster.submit(size, artifact(str(src)), memo=True).memo_hit
    src.write_text("v2-longer")  # in-place overwrite: a new generation of the same URI
    assert not h.cluster.submit(size, artifact(str(src)), memo=True).memo_hit


def test_a_dangling_entry_recomputes_and_is_rewritten(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def make(out: sk8s.OutDir) -> Path:
        (out / "o.txt").write_text("data")
        return out / "o.txt"

    a = h.cluster.submit(make, memo=True)
    h.finish(a.job_name)
    produced = a.result(timeout=10)
    h.store.delete(produced.uri)  # the sweeper took it
    b = h.cluster.submit(make, memo=True)
    assert not b.memo_hit  # dangling -> recompute
    h.finish(b.job_name)
    assert b.result(timeout=10).sha256 == produced.sha256
    assert h.cluster.submit(make, memo=True).memo_hit  # and the entry is good again


def test_memo_with_batching_is_refused_loudly(tmp_path: Path) -> None:
    from sk8s import api

    h = Harness(tmp_path)
    sk8s.configure(cluster=h.cluster)
    try:
        with pytest.raises(ValueError, match="batch_size"):
            api.submit_map(double, [1, 2, 3], batch_size=2, memo=True)
    finally:
        sk8s.configure()


def test_config_memo_does_not_memoize_batch_wrapper_jobs(tmp_path: Path) -> None:
    from sk8s import api

    h = Harness(tmp_path, memo=True)
    sk8s.configure(cluster=h.cluster)
    try:
        futs = api.submit_map(double, [1, 2], batch_size=2)
        (job,) = {f.job_name for f in futs}
        h.finish(job)
        assert [f.result(timeout=10) for f in futs] == [2, 4]
        assert h.entries() == []
    finally:
        sk8s.configure()


def test_memo_through_submit_map_hits_per_item(tmp_path: Path) -> None:
    from sk8s import api

    h = Harness(tmp_path)
    sk8s.configure(cluster=h.cluster)
    try:
        futs = api.submit_map(double, [1, 2, 3], memo=True)
        assert not any(f.memo_hit for f in futs)
        for f in futs:
            h.finish(f.job_name)
        assert [f.result(timeout=10) for f in futs] == [2, 4, 6]
        jobs = len(h.batch.jobs)
        again = api.submit_map(double, [3, 1, 4], memo=True)  # 3 and 1 were answered before
        assert [f.memo_hit for f in again] == [True, True, False]
        assert len(h.batch.jobs) == jobs + 1
        h.finish(again[2].job_name)
        assert [f.result(timeout=10) for f in again] == [6, 2, 8]
    finally:
        sk8s.configure()


def test_job_options_include_the_memo_names() -> None:
    from sk8s.cluster import JOB_OPTIONS

    assert {"memo", "memo_salt"} <= JOB_OPTIONS


def test_config_memo_from_env_and_file(tmp_path: Path) -> None:
    from sk8s.config import load_config

    assert load_config(path=tmp_path / "none.toml", env={"SK8S_MEMO": "true"}).memo is True
    assert load_config(path=tmp_path / "none.toml", env={}).memo is False
    f = tmp_path / "c.toml"
    f.write_text("memo = true\n")
    assert load_config(path=f, env={}).memo is True


# --- LocalCluster (real subprocess workers) -------------------------------------------------------------


def test_local_cluster_memo_hit_spawns_no_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lc = LocalCluster(tmp_path / "root")
    spawned: list[Any] = []
    real = subprocess.Popen

    def counting(*a: Any, **k: Any) -> Any:
        spawned.append(a)
        return real(*a, **k)

    monkeypatch.setattr("sk8s.local.subprocess.Popen", counting)
    assert lc.submit(double, 8, memo=True).result(timeout=60) == 16
    assert len(spawned) == 1
    again = lc.submit(double, 8, memo=True)
    assert again.memo_hit and again.result() == 16 and len(spawned) == 1


def test_local_cluster_failure_is_not_memoized(tmp_path: Path) -> None:
    lc = LocalCluster(tmp_path / "root")

    def boom() -> None:
        raise RuntimeError("bad")

    with pytest.raises(RuntimeError):
        lc.submit(boom, memo=True).result(timeout=60)
    assert list(lc.store.list_blobs("artifacts/memo/")) == []


# --- review regressions: state the key must see ------------------------------------------------


class _Model:
    def __init__(self, k: int) -> None:
        self.k = k

    def score(self, x: int) -> int:
        return x * self.k


_CONFIG = _Model(2)


def _reads_config(x: int) -> int:
    return _CONFIG.score(x)


def test_bound_method_state_is_part_of_the_key() -> None:
    m = _Model(2)
    before = key(func=m.score, args=(3,))
    m.k = 5
    assert key(func=m.score, args=(3,)) != before


def test_object_globals_are_part_of_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = memo.function_fingerprint(_reads_config)
    monkeypatch.setattr(_CONFIG, "k", 9)
    assert memo.function_fingerprint(_reads_config) != before


def test_class_globals_fingerprint_their_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    before = memo.function_fingerprint(_reads_config)

    def other(self: Any, x: int) -> int:
        return x + 1

    other.__qualname__ = _Model.score.__qualname__
    monkeypatch.setattr(_Model, "score", other)
    assert memo.function_fingerprint(_reads_config) != before


def test_adopt_outputs_reports_a_collected_output(astore: ArtifactStore, tmp_path: Path) -> None:
    out = make_art(astore, tmp_path)
    hit = memo.MemoHit(value=out, key="k", outputs=[out])
    assert memo.adopt_outputs(astore, "wf-x", hit) is True
    astore.store.delete(out.uri)  # swept between lookup and recording
    assert memo.adopt_outputs(astore, "wf-y", hit) is False


# --- composition with expect= (envelope v2) ----------------------------------------------------------


def _lax(r: Any) -> bool:
    return True


def _strict(r: Any) -> bool:
    return bool(r < 10)


def test_the_gate_is_part_of_the_key() -> None:
    assert key(expect=_lax) != key() and key(expect=_lax) != key(expect=_strict)
    assert key(expect=_lax) == key(expect=_lax)


def test_a_gated_out_run_writes_no_entry_and_a_stricter_gate_misses(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    failing = h.cluster.submit(double, 8, memo=True, expect=lambda r: r < 10)  # 16 fails
    h.finish(failing.job_name, "failed")
    with pytest.raises(sk8s.ExpectationFailed):
        failing.result(timeout=10)
    assert h.entries() == []  # gated-out: nothing recorded

    ok = h.cluster.submit(double, 3, memo=True, expect=_lax)
    h.finish(ok.job_name)
    assert ok.result(timeout=10) == 6 and len(h.entries()) == 1
    assert h.cluster.submit(double, 3, memo=True, expect=_lax).memo_hit  # same gate: hit
    again = h.cluster.submit(double, 3, memo=True, expect=_strict)  # a different gate must re-check
    assert not again.memo_hit
    h.finish(again.job_name)
    assert again.result(timeout=10) == 6


class _Base:
    def helper(self, x: int) -> int:
        return x + 1


class _Calc(_Base):
    def run(self, x: int) -> int:
        return self.helper(x) * 2


class _Callable:
    def __call__(self, x: int) -> int:
        return _Base().helper(x)


def test_editing_a_sibling_or_inherited_method_changes_a_bound_methods_key(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _Calc()
    before = memo.function_fingerprint(c.run)

    def new_helper(self: Any, x: int) -> int:
        return x + 100

    new_helper.__qualname__ = _Base.helper.__qualname__
    monkeypatch.setattr(_Base, "helper", new_helper)  # a method of the BASE class run() reaches
    assert memo.function_fingerprint(c.run) != before


def test_editing_a_class_the_callable_object_belongs_to_changes_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    obj = _Callable()
    before = memo.function_fingerprint(obj)

    def new_call(self: Any, x: int) -> int:
        return x * 7

    new_call.__qualname__ = _Callable.__call__.__qualname__
    monkeypatch.setattr(_Callable, "__call__", new_call)
    assert memo.function_fingerprint(obj) != before
