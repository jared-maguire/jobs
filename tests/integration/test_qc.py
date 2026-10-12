"""QC gates and pilots against a real cluster: expect=, ExpectationFailed, pilot=.

Needs a worker image built from this branch (the expectation runs in the worker):
``minikube image build -t sk8s-dev-qc:latest -f docker/Dockerfile.dev .`` and
``SK8S_TEST_IMAGE=sk8s-dev-qc:latest``.
"""
import sys

import cloudpickle
import pytest

import sk8s
from sk8s import serialize
from sk8s.errors import ExpectationFailed, MapFailed, PilotFailed, RemoteTraceback
from tests.integration.conftest import requires_cluster

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(600)]

# The helpers below live in this (test-only) module, which the worker image does not have.
cloudpickle.register_pickle_by_value(sys.modules[__name__])


def percent_mapped(sample: int) -> float:
    return 0.61 if sample == 23 else 0.97


def mapped_gate(frac: float) -> str | None:
    return None if frac >= 0.9 else f"only {frac:.0%} mapped (need >= 90%)"


@pytest.fixture()
def default_cluster(cluster):  # type: ignore[no-untyped-def]
    sk8s.configure(cluster=cluster)
    yield cluster
    sk8s.configure(cluster=None)


def jobs_named(cluster, prefix: str) -> list[str]:  # type: ignore[no-untyped-def]
    jobs = cluster.batch.list_namespaced_job(
        namespace=cluster.config.namespace,
        label_selector=f"sk8s/workflow-id={cluster.workflow_id}",
    ).items
    return [j.metadata.name for j in jobs if j.metadata.name.startswith(prefix)]


def test_passing_expectation(cluster) -> None:  # type: ignore[no-untyped-def]
    assert cluster.run(percent_mapped, 1, expect=mapped_gate, timeout=240) == 0.97


def test_failing_expectation_is_typed_with_message_intact(cluster) -> None:  # type: ignore[no-untyped-def]
    fut = cluster.submit(percent_mapped, 23, expect=mapped_gate, keep_results=True)
    with pytest.raises(ExpectationFailed) as exc_info:
        fut.result(timeout=240)
    e = exc_info.value
    assert e.message == "only 61% mapped (need >= 90%)"
    assert e.result_repr == "0.61"
    assert isinstance(e.__cause__, RemoteTraceback)
    # The rejected result is still stored (keep_results) and inspectable.
    key = f"{cluster.workflow_id}/{fut.job_name}/result"
    assert serialize.load_result_value(cluster.store.get(cluster.store.uri_for(key))) == 0.61


def test_function_crash_is_not_an_expectation_failure(cluster) -> None:  # type: ignore[no-untyped-def]
    def boom() -> int:
        raise KeyError("remote key")

    with pytest.raises(KeyError):
        cluster.run(boom, expect=lambda r: "never reached", timeout=240)


def test_pilot_green_then_full_run(default_cluster) -> None:  # type: ignore[no-untyped-def]
    out = sk8s.map(
        percent_mapped, [1, 2, 3, 4, 5, 6], pilot=2, expect=mapped_gate,
        name_prefix="qc-green", timeout=420,
    )
    assert out == [0.97] * 6
    assert len(jobs_named(default_cluster, "qc-green")) == 6


def test_pilot_failure_submits_nothing_further(default_cluster) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(
            percent_mapped, [1, 23, 3, 4, 5, 6], pilot=2, expect=mapped_gate,
            name_prefix="qc-red", timeout=420,
        )
    (idx, err), = exc_info.value.failures
    assert idx == 1 and isinstance(err, ExpectationFailed)
    assert err.message == "only 61% mapped (need >= 90%)"
    assert len(jobs_named(default_cluster, "qc-red")) == 2  # the other 4 were never created


def test_quarantine_after_green_pilot(default_cluster) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(
            percent_mapped, [1, 2, 3, 23, 5, 6], pilot=2, expect=mapped_gate,
            name_prefix="qc-quar", timeout=420,
        )
    e = exc_info.value
    assert not isinstance(e, PilotFailed)
    assert [i for i, _ in e.failures] == [3]
    assert "1 failed their expectation, 0 errored" in str(e)
    assert len(jobs_named(default_cluster, "qc-quar")) == 6
