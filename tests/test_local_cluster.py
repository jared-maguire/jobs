import pytest
from concurrent.futures import Future

from sk8s.errors import RemoteTraceback
from sk8s.future import JobFuture
from sk8s.local import LocalCluster


def test_submit_returns_future_with_result() -> None:
    lc = LocalCluster()
    fut = lc.submit(lambda x: x + 1, 41)
    assert isinstance(fut, Future) and isinstance(fut, JobFuture)
    assert fut.job_name.startswith("job-")
    assert fut.result(timeout=60) == 42


def test_none_result() -> None:
    assert LocalCluster().submit(lambda: None).result(timeout=60) is None


def test_function_kwargs_pass_through() -> None:
    fut = LocalCluster().submit(lambda a, b=0: a + b, 1, b=2)
    assert fut.result(timeout=60) == 3


def test_remote_exception_reraised() -> None:
    def boom() -> None:
        raise ValueError("local boom")

    fut = LocalCluster().submit(boom)
    with pytest.raises(ValueError, match="local boom") as exc_info:
        fut.result(timeout=60)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)


def test_closure_capture() -> None:
    n = 10
    assert LocalCluster().submit(lambda: n * 2).result(timeout=60) == 20


def test_job_options_accepted_and_ignored() -> None:
    fut = LocalCluster().submit(lambda: 1, image="whatever:latest", requests={"cpu": "4"})
    assert fut.result(timeout=60) == 1


def test_run_blocks_and_returns_result() -> None:
    assert LocalCluster().run(lambda x, y=0: x * 3 + y, 4, y=5, timeout=60) == 17
