"""Artifact layer on a live cluster with the ConfigMap store (small files).

Needs the worker image built from THIS branch, tagged ``sk8s-dev-artifacts:latest``::

    minikube image build -t sk8s-dev-artifacts:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 SK8S_NAMESPACE=sk8s-it-art SK8S_CONFIG=/tmp/none.toml \\
        KUBECONFIG=.kubeconfig-minikube .venv/bin/pytest tests/integration/test_artifacts.py -v

Override the tag with ``SK8S_ARTIFACTS_TEST_IMAGE``. The suite creates its own namespace
(``sk8s-it-art``, see ``artifacts_env``) via ``sk8s config``'s machinery and deletes it at exit.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

import sk8s
from sk8s import gc, k8s
from sk8s.artifacts import Artifact
from sk8s.cas import ArtifactStore, cas_key
from sk8s.cluster import Cluster
from sk8s.config import load_config
from sk8s.local import LocalCluster
from sk8s.store.configmap import ConfigMapStore
from sk8s.store.retry import update_blob
from tests.integration.artifacts_env import IMAGE, ensure_namespace
from tests.integration.conftest import requires_cluster, suite_pull_policy

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

@pytest.fixture(scope="module")
def ns() -> str:
    return ensure_namespace()  # a namespace of the suite's own, whatever SK8S_NAMESPACE says


def new_cluster(ns: str) -> Cluster:
    return Cluster(load_config(
        namespace=ns, image=IMAGE, pull_policy=suite_pull_policy(), ttl_seconds_after_finished=30,
    ))


@pytest.fixture
def cluster(ns: str) -> Iterator[Cluster]:
    c = new_cluster(ns)
    yield c
    c.close()


def job_names(c: Cluster) -> set[str]:
    return {j.metadata.name for j in c.batch.list_namespaced_job(namespace=c.config.namespace).items}


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


# --- staging round trip ---------------------------------------------------------------------


def test_staging_round_trip_on_the_k8s_store(cluster: Cluster, tmp_path: Path) -> None:
    reads = write(tmp_path, "reads.txt", b"ACGT\n" * 200)
    fq = cluster.artifact_store.put_file(reads, workflow=cluster.workflow_id)
    assert fq.uri.startswith("k8s://") and fq.size == 1000

    def transform(fastq: Path, out: sk8s.OutDir) -> Path:
        import os as _os

        # (plain raises: pytest's assert rewriting would drag _pytest into the pickled function)
        if not (isinstance(fastq, Path) and fastq.name == "reads.txt"):  # real path, original name
            raise RuntimeError(f"not staged as a Path with its name: {fastq!r}")
        if fastq.read_bytes() != b"ACGT\n" * 200:
            raise RuntimeError("staged bytes differ")
        if not _os.access(out.path, _os.W_OK):
            raise RuntimeError("OutDir is not writable")
        (out / "reads.rev.txt").write_bytes(fastq.read_bytes()[::-1])
        return out / "reads.rev.txt"

    fut = cluster.submit(transform, fq)
    job = cluster.batch.read_namespaced_job(name=fut.job_name, namespace=cluster.config.namespace)
    requests = job.spec.template.spec.containers[0].resources.requests
    assert requests["ephemeral-storage"] in ("1024Mi", "1Gi")  # the API normalizes; A2: 2x the 1000 input bytes, floored at 1Gi
    result = fut.result(timeout=240)

    assert isinstance(result, Artifact) and result.name == "reads.rev.txt" and result.size == 1000
    assert result.produced_by == f"{cluster.workflow_id}/{fut.job_name}"
    got = result.download(tmp_path / "back.txt")
    assert got.read_bytes() == (b"ACGT\n" * 200)[::-1]
    assert hashlib.sha256(got.read_bytes()).hexdigest() == result.sha256  # hash-while-streaming agrees
    idx = cluster.artifact_store.read_index(result.sha256 or "")
    assert idx is not None
    assert idx["provenance"]["inputs"] == [fq.sha256]  # lineage: output <- input hash
    assert idx["provenance"]["produced_by"] == result.produced_by


def test_next_job_consumes_the_previous_output_by_hash(cluster: Cluster, tmp_path: Path) -> None:
    src = cluster.artifact_store.put_file(write(tmp_path, "n.txt", b"41"), workflow=cluster.workflow_id)

    def inc(f: Path, out: sk8s.OutDir) -> Path:
        (out / "n.txt").write_text(str(int(f.read_text()) + 1))
        return out / "n.txt"

    one = cluster.run(inc, src, timeout=240)
    two = cluster.run(inc, one, timeout=240)
    assert two.download(tmp_path / "two.txt").read_text() == "43"


def test_missing_input_fails_at_submit_with_no_job(cluster: Cluster) -> None:
    from sk8s.errors import ArtifactNotFound

    before = job_names(cluster)
    with pytest.raises(ArtifactNotFound):
        cluster.submit(lambda f: 1, sk8s.artifact("/does/not/exist.fq"))
    assert job_names(cluster) == before


# --- the real API server's conditional writes -----------------------------------------------------


def fresh_store(ns: str) -> ConfigMapStore:
    return ConfigMapStore(namespace=ns, core_api=k8s.core_api())


def test_create_if_absent_has_one_winner_on_the_real_api(ns: str) -> None:
    store = fresh_store(ns)
    tag = hashlib.sha256(os.urandom(8)).hexdigest()[:10]
    for trial in range(10):
        key = f"artifacts/it-race/{tag}/{trial}"
        gate = threading.Barrier(2)
        wins: list[bool] = []

        def contend(i: int, key: str = key, gate: threading.Barrier = gate) -> None:
            s = fresh_store(ns)
            gate.wait()
            wins.append(s.put_if_absent(key, f"w{i}".encode()))

        ts = [threading.Thread(target=contend, args=(i,)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(wins) == [False, True], (trial, wins)
    store.delete_prefix(f"artifacts/it-race/{tag}")


def test_resource_version_cas_loses_nothing_under_ten_way_contention(ns: str) -> None:
    import json

    tag = hashlib.sha256(os.urandom(8)).hexdigest()[:10]
    key = f"artifacts/it-cas/{tag}"

    def add(member: str) -> Any:
        def mutate(cur: bytes | None) -> bytes | None:
            members = set(json.loads(cur)) if cur else set()
            return None if member in members else json.dumps(sorted(members | {member})).encode()

        return mutate

    gate = threading.Barrier(10)
    errors: list[BaseException] = []

    def go(i: int) -> None:
        s = fresh_store(ns)
        gate.wait()
        try:
            update_blob(s, key, add(f"m{i}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    final = json.loads(fresh_store(ns).read_with_version(key)[0])
    assert final == sorted(f"m{i}" for i in range(10))
    fresh_store(ns).delete_prefix(f"artifacts/it-cas/{tag}")


def test_stale_resource_version_is_refused_and_a_blind_patch_is_not_used(ns: str) -> None:
    s = fresh_store(ns)
    key = f"artifacts/it-stale/{hashlib.sha256(os.urandom(8)).hexdigest()[:10]}"
    s.put_if_absent(key, b"v1")
    _, v1 = s.read_with_version(key)
    assert s.put_if_version(key, b"v2", v1) is True
    assert s.put_if_version(key, b"stale", v1) is False  # 409 Conflict -> False, not an overwrite
    assert s.read_with_version(key)[0] == b"v2"
    assert s.put_if_version(f"{key}-missing", b"x", v1) is False  # 404 -> False, not a create
    s.delete_prefix(key)


def test_two_concurrent_clients_put_the_same_content(ns: str, tmp_path: Path) -> None:
    data = os.urandom(4096)
    results: list[Artifact] = []
    errors: list[BaseException] = []
    gate = threading.Barrier(2)
    wf = f"wf-it-{hashlib.sha256(data).hexdigest()[:6]}"

    def client(i: int) -> None:
        astore = ArtifactStore(fresh_store(ns), "k8s")
        f = write(tmp_path, f"copy{i}.bin", data)
        gate.wait()
        try:
            results.append(astore.put_file(f, workflow=wf, job=f"job-{i}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=client, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert len({a.uri for a in results}) == 1 and len({a.sha256 for a in results}) == 1
    reader = ArtifactStore(fresh_store(ns), "k8s")
    assert reader.store.get(results[0].uri) == data
    idx = reader.read_index(results[0].sha256 or "")
    assert idx is not None and len(idx["locations"]) == 1  # the loser's index merge was a no-op
    reader.store.delete_prefix(f"workflows/{wf}")
    reader.store.delete(results[0].uri)
    reader.store.delete(reader.store.uri_for(f"artifacts/index/sha256/{(results[0].sha256 or '')[:2]}/{results[0].sha256}.json"))


# --- memoization against the real cluster -----------------------------------------------------------


def test_memo_hit_submits_nothing(cluster: Cluster, ns: str, tmp_path: Path) -> None:
    src = cluster.artifact_store.put_file(write(tmp_path, "m.txt", os.urandom(64)), workflow=cluster.workflow_id)

    def work(f: Path, out: sk8s.OutDir) -> Path:
        (out / "o.bin").write_bytes(f.read_bytes() * 2)
        return out / "o.bin"

    first = cluster.submit(work, src, memo=True)
    assert not first.memo_hit
    produced = first.result(timeout=240)
    assert produced.size == 128

    jobs_before = job_names(cluster)
    second = cluster.submit(work, src, memo=True)
    assert second.memo_hit and second.done() and second.result() == produced
    assert job_names(cluster) == jobs_before  # zero new jobs

    # a brand-new client/workflow asking the same question also gets the answer for free
    other = new_cluster(ns)
    try:
        jobs_other = job_names(other)
        again = other.submit(work, src, memo=True)
        assert again.memo_hit and again.result() == produced
        assert job_names(other) == jobs_other
        # ... and it adopted the outputs into ITS reference set (so they outlive the first workflow)
        refs = other.artifact_store.store.read_with_version(f"workflows/{other.workflow_id}/artifacts.json")[0]
        assert produced.sha256 and produced.sha256.encode() in refs
    finally:
        other.close()
    # changing the salt is the by-hand invalidation: it runs again
    third = cluster.submit(work, src, memo=True, memo_salt="v2")
    assert not third.memo_hit
    third.result(timeout=240)


def test_failed_runs_do_not_poison_the_memo(cluster: Cluster) -> None:
    def boom(x: int) -> int:
        raise ValueError("deliberate")

    for _ in range(2):
        fut = cluster.submit(boom, 1, memo=True)
        assert not fut.memo_hit  # the second attempt really ran again
        with pytest.raises(ValueError, match="deliberate"):
            fut.result(timeout=240)


# --- publish on the local store ----------------------------------------------------------------------


def test_publish_on_the_local_store(tmp_path: Path) -> None:
    lc = LocalCluster(tmp_path / "root")

    def produce(out: sk8s.OutDir) -> Path:
        (out / "counts.tsv").write_text("S1\t42\n")
        return out / "counts.tsv"

    art = lc.submit(produce).result(timeout=120)
    dest = tmp_path / "published" / "counts.tsv"
    pub = art.publish(str(dest))
    assert dest.read_text() == "S1\t42\n" and pub.sha256 == art.sha256
    assert lc.store.stat(art.uri) is None  # moved: CAS copy retired
    st = lc.store.stat(f"local:{dest}")  # type: ignore[arg-type]  # the local store reads any local: uri
    assert st is not None and st.metadata["sk8s-artifact-sha256"] == art.sha256
    assert st.metadata["sk8s-produced-by"] == art.produced_by
    assert art.download(tmp_path / "again.tsv").read_text() == "S1\t42\n"  # the old handle still works


# --- GC on the real store --------------------------------------------------------------------------------


def test_sweep_on_the_k8s_store_collects_only_the_unreferenced(ns: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    astore = ArtifactStore(fresh_store(ns), "k8s")
    wf = f"wf-it-gc-{hashlib.sha256(os.urandom(4)).hexdigest()[:6]}"
    kept = astore.put_file(write(tmp_path, "k", os.urandom(32)), workflow=wf, job="j")
    gone = astore.put_file(write(tmp_path, "g", os.urandom(32)))  # nothing references it
    young = astore.put_file(write(tmp_path, "y", os.urandom(32)))
    monkeypatch.setattr(gc, "CAS_MIN_AGE_SECONDS", 0.0)
    time.sleep(1.5)
    report = gc.sweep(astore.store, min_age_seconds=0.0)
    assert report.aborted is None, report.aborted
    assert gone.sha256 in report.deleted and young.sha256 in report.deleted  # (both unreferenced; floor is 0 here)
    assert kept.sha256 not in report.deleted and astore.store.stat(kept.uri) is not None
    assert astore.store.stat(gone.uri) is None
    astore.store.delete_prefix(f"workflows/{wf}")
    gc.sweep(astore.store, min_age_seconds=0.0)  # tidy `kept` too
    assert astore.store.stat(kept.uri) is None
