import pytest

from tests.fakes import FakeBatchV1, FakeCoreV1


@pytest.fixture(autouse=True)
def _no_real_cluster_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker.main sweeps via the real kube client; unit tests get an empty fake instead."""
    import sk8s.worker as worker

    monkeypatch.setattr(worker, "_core_api", lambda: FakeCoreV1())
    monkeypatch.setattr(worker, "_batch_api", lambda: FakeBatchV1())
