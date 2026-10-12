"""Artifact arguments through Cluster.submit (fake k8s) and LocalCluster (real subprocess workers)."""
from __future__ import annotations

import json
import queue
from pathlib import Path
from typing import Any, Iterator

import pytest

import sk8s
from sk8s import serialize
from sk8s.artifacts import Artifact, artifact
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import ArtifactNotFound
from sk8s.local import LocalCluster
from sk8s.store.local import LocalStore
from sk8s.watch import JobEvent
from tests.fakes import FakeBatchV1, FakeCoreV1

_OPEN: list[Cluster] = []


@pytest.fixture(autouse=True)
def _close_clusters() -> Iterator[None]:
    yield
    while _OPEN:
        _OPEN.pop().close()


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.batch = FakeBatchV1()
        self.core = FakeCoreV1()
        self.store = LocalStore(tmp_path / "store")
        self.events: queue.Queue[JobEvent] = queue.Queue()
        self.cluster = Cluster(
            Config(namespace="ns1", image="sk8s:dev", store=f"local:{self.store.root}"),
            batch_api=self.batch, core_api=self.core, store=self.store,
            watcher_events=self._iter,
        )
        _OPEN.append(self.cluster)

    def _iter(self) -> Iterator[JobEvent]:
        while True:
            yield self.events.get()

    def finish_job(self, job_name: str) -> None:
        import sk8s.worker as worker

        job = self.batch.jobs[("ns1", job_name)]
        env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
        env["SK8S_STORE"] = f"local:{self.store.root}"
        worker.main(env)
        self.events.put((job_name, "succeeded"))

    def container(self, job_name: str) -> Any:
        return self.batch.jobs[("ns1", job_name)].spec.template.spec.containers[0]


def ext(tmp_path: Path, name: str = "s1.fq.gz", data: bytes = b"hello world") -> Path:
    p = tmp_path / "ext" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    return p


def test_submit_records_artifacts_in_the_payload_header(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    f = ext(tmp_path)
    fut = h.cluster.submit(lambda fq: 1, artifact(str(f)))
    payload = h.store.get(h.store.uri_for(f"{h.cluster.workflow_id}/{fut.job_name}/payload"))
    header = json.loads(payload.split(b"\n", 1)[0])
    (rec,) = header["artifacts"]
    assert rec["uri"] == f"local:{f}" and rec["size"] == 11 and rec["version"]


def test_submit_without_artifacts_has_no_artifact_header_and_no_ephemeral_request(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    payload = h.store.get(h.store.uri_for(f"{h.cluster.workflow_id}/{fut.job_name}/payload"))
    assert "artifacts" not in json.loads(payload.split(b"\n", 1)[0])
    assert h.container(fut.job_name).resources is None


def test_ephemeral_storage_is_twice_the_known_input_size(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # sparse files: the pin stats sizes, nothing is read
    big, small = ext(tmp_path, "big.bin", b""), ext(tmp_path, "small.bin", b"y")
    with open(big, "r+b") as fh:
        fh.truncate(600 * 1024**2)
    fut = h.cluster.submit(lambda a, b: 1, artifact(str(big)), artifact(str(small)))
    res = h.container(fut.job_name).resources
    assert res.requests["ephemeral-storage"] == "1201Mi"  # ceil(2 * (600MiB + 1B) / 1MiB)


def test_explicit_ephemeral_request_is_not_overridden(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    f = ext(tmp_path)
    fut = h.cluster.submit(lambda a: 1, artifact(str(f)), requests={"ephemeral-storage": "40Gi", "cpu": "2"})
    assert h.container(fut.job_name).resources.requests == {"ephemeral-storage": "40Gi", "cpu": "2"}
    fut2 = h.cluster.submit(lambda a: 1, artifact(str(f)), limits={"ephemeral-storage": "9Gi"})
    assert "ephemeral-storage" not in (h.container(fut2.job_name).resources.requests or {})


def test_existing_requests_are_kept_alongside_the_ephemeral_one(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    f = ext(tmp_path)
    fut = h.cluster.submit(lambda a: 1, artifact(str(f)), requests={"cpu": "2"})
    assert h.container(fut.job_name).resources.requests == {"cpu": "2", "ephemeral-storage": "1024Mi"}


def test_huge_inputs_warn_that_generic_ephemeral_volumes_are_not_implemented(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    h = Harness(tmp_path)
    f = ext(tmp_path)
    huge = Artifact(uri=f"local:{f}", size=60 * 1024**3, version="v", name="huge.bam")
    with caplog.at_level(logging.WARNING, logger="sk8s.cluster"):
        h.cluster.submit(lambda a: 1, huge)
    assert any("generic ephemeral" in r.message for r in caplog.records)


def test_missing_input_fails_the_submit_and_creates_no_job(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(ArtifactNotFound):
        h.cluster.submit(lambda a: 1, artifact(str(tmp_path / "ghost.fq")))
    assert h.batch.jobs == {}
    assert list(h.store.list_blobs(f"{h.cluster.workflow_id}/")) == []  # no orphan payload


def test_cluster_run_end_to_end_with_fake_k8s(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    f = ext(tmp_path)

    def shout(fq: Path, out: sk8s.OutDir) -> Path:
        (out / "loud.txt").write_text(fq.read_text().upper())
        return out / "loud.txt"

    fut = h.cluster.submit(shout, artifact(str(f)))
    h.finish_job(fut.job_name)
    result = fut.result(timeout=10)
    assert isinstance(result, Artifact) and h.store.get(result.uri) == b"HELLO WORLD"
    assert result.produced_by == f"{h.cluster.workflow_id}/{fut.job_name}"


# --- LocalCluster: real subprocess workers, no fakes -------------------------------------


def test_local_cluster_stage_in_and_out(tmp_path: Path) -> None:
    lc = LocalCluster(tmp_path / "root")
    f = ext(tmp_path)

    def shout(fq: Path, out: sk8s.OutDir) -> Path:
        (out / "loud.txt").write_text(fq.read_text().upper())
        return out / "loud.txt"

    result = lc.submit(shout, artifact(str(f))).result(timeout=60)
    assert isinstance(result, Artifact)
    assert result.size == 11 and result.name == "loud.txt"
    assert lc.store.get(result.uri) == b"HELLO WORLD"
    idx = lc.artifact_store.read_index(result.sha256 or "")
    assert idx is not None and idx["provenance"]["produced_by"].startswith(lc.workflow_id)
    # the next job consumes the first job's output Artifact, by hash, from the CAS
    def count(fq: Path) -> int:
        return len(fq.read_text())

    assert lc.submit(count, result).result(timeout=60) == 11


def test_local_cluster_in_place_overwrite_between_submit_and_start_is_refused(tmp_path: Path) -> None:
    from sk8s.errors import ArtifactChanged

    lc = LocalCluster(tmp_path / "root")
    f = ext(tmp_path)
    pinned = lc.artifact_store  # noqa: F841
    from sk8s import staging

    a = staging.pin(lc.artifact_store, artifact(str(f)))
    f.write_bytes(b"HELLO WORLD")  # same size, overwritten in place after the pin
    with pytest.raises(ArtifactChanged):
        lc.submit(lambda fq: 1, a).result(timeout=60)
