import concurrent.futures
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from sk8s.errors import RemoteTraceback
from sk8s.future import JobFuture
from sk8s.local import LocalCluster


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    """Block until a worker has started (signalled by writing a marker file)."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"worker never wrote {path}"
        time.sleep(0.02)


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


def test_cancel_running_job_stays_cancelled(tmp_path: Path) -> None:
    started = tmp_path / "started"

    def sleeper(marker: str) -> None:
        import time as _time
        from pathlib import Path as _Path

        _Path(marker).write_text("started")
        _time.sleep(30)

    lc = LocalCluster()
    fut = lc.submit(sleeper, str(started))
    _wait_for(started)

    t0 = time.monotonic()
    assert fut.cancel() is True
    assert fut.cancelled()

    # Drain the pool: the worker thread returns only after its process exits.
    lc._pool.shutdown(wait=True)
    assert time.monotonic() - t0 < 20, "cancel did not kill the worker promptly"
    assert fut.cancelled(), "future must stay cancelled after the worker exits"


def test_cancel_queued_job_never_runs(tmp_path: Path) -> None:
    started = tmp_path / "started"
    ran = tmp_path / "ran"

    def blocker(marker: str) -> None:
        import time as _time
        from pathlib import Path as _Path

        _Path(marker).write_text("started")
        _time.sleep(30)

    def writer(marker: str) -> None:
        from pathlib import Path as _Path

        _Path(marker).write_text("ran")

    lc = LocalCluster(max_workers=1)
    blocker_fut = lc.submit(blocker, str(started))
    _wait_for(started)

    queued = lc.submit(writer, str(ran))  # stuck behind the single worker slot
    assert queued.cancel() is True
    assert queued.cancelled()

    assert blocker_fut.cancel() is True
    lc._pool.shutdown(wait=True)  # pool thread for `queued` runs and must stand down

    assert not ran.exists(), "a job cancelled while queued must never spawn"
    assert queued.cancelled()


def test_run_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    lc = LocalCluster()
    submitted: list[JobFuture] = []
    real_submit: Callable[..., JobFuture] = lc.submit

    def tracking_submit(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
        fut = real_submit(func, *args, **kwargs)
        submitted.append(fut)
        return fut

    monkeypatch.setattr(lc, "submit", tracking_submit)

    def sleeper() -> None:
        import time as _time

        _time.sleep(30)

    # concurrent.futures.TimeoutError is not the builtin on every supported Python.
    with pytest.raises(concurrent.futures.TimeoutError):
        lc.run(sleeper, timeout=0.5)

    # Clean up the still-running job so the test does not leave a 30s worker behind.
    assert submitted[0].cancel() is True
