import pytest

from tests.fakes import FakeBatchV1, FakeCoreV1


@pytest.fixture(autouse=True)
def _no_real_cluster_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker.main sweeps via the real kube client; unit tests get an empty fake instead."""
    import sk8s.worker as worker

    monkeypatch.setattr(worker, "_core_api", lambda: FakeCoreV1())
    monkeypatch.setattr(worker, "_batch_api", lambda: FakeBatchV1())


@pytest.fixture()
def serial_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the fan-out submission pool to one worker.

    For tests that assert the ORDER in which jobs reach the cluster (stub bookkeeping
    indexed by submission sequence): with the parallel pool, submission order within a
    fan-out is not deterministic — only the returned futures' order is. The parallel
    path itself is covered by test_parallel_submit.py."""
    from sk8s import api

    monkeypatch.setattr(api, "_SUBMIT_WORKERS", 1)
