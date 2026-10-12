"""QC gates (expect=) end to end on the local backend (real worker subprocesses)."""
import pytest

import sk8s
from sk8s.errors import ExpectationFailed, MapFailed, RemoteTraceback
from sk8s.local import LocalCluster


@pytest.fixture(autouse=True)
def local_backend():  # type: ignore[no-untyped-def]
    sk8s.configure(cluster=LocalCluster(max_workers=4))
    yield
    sk8s.configure(cluster=None)


def mapped(sample: int) -> float:
    return {23: 0.61}.get(sample, 0.97)


def mapped_gate(frac: float) -> str | None:
    return None if frac >= 0.9 else f"only {frac:.0%} mapped (need >= 90%)"


def test_run_with_passing_expectation() -> None:
    assert sk8s.run(lambda x: x + 1, 1, expect=lambda r: r == 2 or "no") == 2


def test_run_with_failing_expectation_raises_typed_error() -> None:
    with pytest.raises(ExpectationFailed) as exc_info:
        sk8s.run(mapped, 23, expect=mapped_gate)
    assert exc_info.value.message == "only 61% mapped (need >= 90%)"
    assert exc_info.value.result_repr == "0.61"
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)


def test_submit_with_failing_expectation() -> None:
    fut = sk8s.submit(mapped, 23, expect=mapped_gate)
    with pytest.raises(ExpectationFailed):
        fut.result(timeout=60)


def test_map_expectation_failures_are_per_item_and_counted_separately() -> None:
    def picky(x: int) -> float:
        if x == 1:
            raise ValueError("crash")
        return mapped(x)

    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(picky, [1, 22, 23, 24], expect=mapped_gate)
    e = exc_info.value
    assert sorted(i for i, _ in e.failures) == [0, 2]
    assert isinstance(dict(e.failures)[2], ExpectationFailed)
    assert isinstance(dict(e.failures)[0], ValueError)
    assert "1 failed their expectation, 1 errored" in str(e)


def test_starmap_expectation() -> None:
    assert sk8s.starmap(lambda a, b: a + b, [(1, 2), (3, 4)], expect=lambda r: r > 0 or "neg") == [3, 7]
    with pytest.raises(MapFailed) as exc_info:
        sk8s.starmap(lambda a, b: a - b, [(1, 2), (5, 4)], expect=lambda r: r > 0 or "neg")
    assert [i for i, _ in exc_info.value.failures] == [0]


def test_batched_map_applies_expectation_per_item() -> None:
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(mapped, [22, 23, 24, 25], batch_size=2, expect=mapped_gate)
    e = exc_info.value
    assert [i for i, _ in e.failures] == [1]
    err = e.failures[0][1]
    assert isinstance(err, ExpectationFailed) and err.result_repr == "0.61"


def test_batched_map_passing_expectation_returns_results() -> None:
    assert sk8s.map(mapped, [22, 24], batch_size=2, expect=mapped_gate) == [0.97, 0.97]


def test_batched_submit_map_expectation_failure_is_per_item_future() -> None:
    futs = sk8s.submit_map(mapped, [22, 23], batch_size=2, expect=mapped_gate)
    assert futs[0].result(timeout=60) == 0.97
    with pytest.raises(ExpectationFailed):
        futs[1].result(timeout=60)


def test_non_callable_expect_raises_before_any_work() -> None:
    with pytest.raises(TypeError, match="callable"):
        sk8s.run(lambda: 1, expect=5)
    with pytest.raises(TypeError, match="callable"):
        sk8s.map(lambda x: x, [1, 2], batch_size=2, expect=5)
