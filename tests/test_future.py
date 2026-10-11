import concurrent.futures

from sk8s.future import JobFuture


class _ResolvingBackend:
    """cancel() lets a 'watcher' resolve the future before reporting success."""

    def __init__(self) -> None:
        self.fut: JobFuture | None = None

    def logs(self, job_name: str) -> str:
        return ""

    def cancel(self, job_name: str) -> bool:
        assert self.fut is not None
        self.fut.set_result("finished first")
        return True


def test_cancel_losing_race_to_resolution_returns_false() -> None:
    backend = _ResolvingBackend()
    fut = JobFuture("job-x", "wf-x", backend=backend)
    backend.fut = fut
    assert concurrent.futures.Future.set_running_or_notify_cancel(fut) is True  # running
    assert fut.cancel() is False  # must not raise InvalidStateError
    assert fut.result(timeout=1) == "finished first"
