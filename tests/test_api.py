import concurrent.futures
import time
from typing import Any

import pytest

import sk8s
import sk8s.cluster
from sk8s.errors import MapFailed
from sk8s.local import LocalCluster


@pytest.fixture(autouse=True)
def local_backend() -> Any:
    sk8s.configure(cluster=LocalCluster())
    yield
    sk8s.configure(cluster=None)  # full reset: clears cluster and overrides


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace sk8s.cluster.Cluster with a stub; record constructor kwargs."""
    constructed: list[dict[str, Any]] = []

    class StubCluster:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def run(self, func: Any, /, *args: Any, **kwargs: Any) -> str:
            return "built"

    monkeypatch.setattr(sk8s.cluster, "Cluster", StubCluster)
    return constructed


class Swapped:
    def run(self, func: Any, /, *args: Any, **kwargs: Any) -> str:
        return "swapped"


# --- core API ---------------------------------------------------------------

def test_run() -> None:
    assert sk8s.run(lambda x: x * 2, 5) == 10


def test_submit_returns_future() -> None:
    assert sk8s.submit(lambda: "hi").result(timeout=60) == "hi"


def test_map_ordered() -> None:
    assert sk8s.map(lambda x: x ** 2, range(5)) == [0, 1, 4, 9, 16]


def test_map_ordered_when_later_items_finish_first() -> None:
    def napper(x: int) -> int:
        import time as _time
        _time.sleep((4 - x) * 0.3)  # item 0 sleeps longest, item 3 shortest
        return x

    assert sk8s.map(napper, range(4)) == [0, 1, 2, 3]


def test_starmap() -> None:
    assert sk8s.starmap(lambda a, b: a + b, [(1, 2), (3, 4)]) == [3, 7]


def test_map_failure_summary() -> None:
    def maybe_boom(x: int) -> int:
        if x == 2:
            raise ValueError("bad item")
        return x

    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(maybe_boom, range(4))
    assert exc_info.value.total == 4
    assert [i for i, _ in exc_info.value.failures] == [2]
    assert isinstance(exc_info.value.failures[0][1], ValueError)
    assert str(exc_info.value) == (
        "1/4 map items failed; first failure at index 2: ValueError: bad item"
    )


def test_map_failure_carries_the_successes() -> None:
    def maybe_boom(x: int) -> int:
        if x == 2:
            raise ValueError("bad item")
        return x * 10

    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(maybe_boom, range(4))
    assert exc_info.value.results == {0: 0, 1: 10, 3: 30}


def test_starmap_failure_carries_the_successes() -> None:
    with pytest.raises(MapFailed) as exc_info:
        sk8s.starmap(lambda a, b: a // b, [(4, 2), (1, 0), (9, 3)])
    assert exc_info.value.results == {0: 2, 2: 3}


def test_starmap_failure_summary() -> None:
    with pytest.raises(MapFailed) as exc_info:
        sk8s.starmap(lambda a, b: a // b, [(4, 2), (1, 0), (9, 3)])
    assert exc_info.value.total == 3
    assert [i for i, _ in exc_info.value.failures] == [1]
    assert isinstance(exc_info.value.failures[0][1], ZeroDivisionError)


def test_submit_map_harvest_per_item() -> None:
    def maybe_boom(x: int) -> int:
        if x == 1:
            raise ValueError("bad")
        return x

    futs = sk8s.submit_map(maybe_boom, range(3))
    assert futs[0].result(timeout=60) == 0
    assert isinstance(futs[1].exception(timeout=60), ValueError)
    assert futs[2].result(timeout=60) == 2


def test_nested_lambda_workflow() -> None:
    # jobs submitting jobs — the v1 signature move, locally
    def workflow() -> int:
        import sk8s
        from sk8s.local import LocalCluster
        sk8s.configure(cluster=LocalCluster())
        squares = sk8s.map(lambda x: x ** 2, range(4))
        return sum(squares)

    assert sk8s.run(workflow) == 14


# --- option splitting -------------------------------------------------------

def test_function_kwarg_passes_through_map() -> None:
    assert sk8s.map(lambda x, scale: x * scale, range(3), scale=3) == [0, 3, 6]


def test_job_option_is_not_passed_to_function() -> None:
    # name_prefix is a JOB_OPTIONS name: consumed by sk8s, so the zero-arg
    # lambda would raise TypeError if it leaked through.
    assert sk8s.run(lambda: "ok", name_prefix="job-x") == "ok"


# --- timeout ----------------------------------------------------------------

def test_map_timeout_is_one_overall_deadline() -> None:
    def nap(seconds: float) -> float:
        import time as _time
        _time.sleep(seconds)
        return seconds

    # Item 0 finishes at once; items 1-3 sleep past the 2s deadline.
    start = time.monotonic()
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(nap, [0.0, 3.0, 3.0, 3.0], timeout=2.0)
    elapsed = time.monotonic() - start

    # Per-item waits would take ~3 x 2s; one shared deadline takes ~2s.
    assert elapsed < 4.0
    err = exc_info.value
    assert err.total == 4
    assert [i for i, _ in err.failures] == [1, 2, 3]
    assert all(isinstance(e, concurrent.futures.TimeoutError) for _, e in err.failures)


def test_map_timeout_none_waits_for_all() -> None:
    assert sk8s.map(lambda x: x + 1, range(3), timeout=None) == [1, 2, 3]


# --- configure semantics ----------------------------------------------------

def test_configure_cluster_swaps_default(built: list[dict[str, Any]]) -> None:
    sk8s.configure(cluster=Swapped())
    assert sk8s.run(lambda: 1) == "swapped"
    assert built == []  # no default Cluster was constructed


def test_configure_overrides_drop_swapped_cluster_and_merge(
    built: list[dict[str, Any]],
) -> None:
    sk8s.configure(cluster=Swapped())
    sk8s.configure(namespace="team-a")
    sk8s.configure(image="img:1")  # merges with the earlier override
    assert sk8s.run(lambda: 1) == "built"
    assert built == [{"namespace": "team-a", "image": "img:1"}]


def test_configure_full_reset_clears_overrides(built: list[dict[str, Any]]) -> None:
    sk8s.configure(namespace="leak")
    sk8s.configure(cluster=None)  # full reset
    assert sk8s.run(lambda: 1) == "built"
    assert built == [{}]  # the stale "leak" override is gone


def test_configure_rejects_cluster_and_overrides_together() -> None:
    with pytest.raises(ValueError):
        sk8s.configure(cluster=Swapped(), namespace="team-a")


def test_default_cluster_built_lazily_with_overrides(
    built: list[dict[str, Any]],
) -> None:
    sk8s.configure(namespace="team-a")
    assert built == []  # nothing constructed at configure time
    assert sk8s.run(lambda: 1) == "built"
    assert sk8s.run(lambda: 2) == "built"
    assert built == [{"namespace": "team-a"}]  # constructed once, then cached
