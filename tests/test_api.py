import pytest

import sk8s
from sk8s.errors import MapFailed
from sk8s.local import LocalCluster


@pytest.fixture(autouse=True)
def local_backend():  # type: ignore[no-untyped-def]
    sk8s.configure(cluster=LocalCluster())
    yield
    sk8s.configure(cluster=None)


def test_run() -> None:
    assert sk8s.run(lambda x: x * 2, 5) == 10


def test_submit_returns_future() -> None:
    assert sk8s.submit(lambda: "hi").result(timeout=60) == "hi"


def test_map_ordered() -> None:
    assert sk8s.map(lambda x: x ** 2, range(5)) == [0, 1, 4, 9, 16]


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
