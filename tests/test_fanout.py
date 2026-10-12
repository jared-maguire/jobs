import concurrent.futures
import threading
import time
from typing import Any

import pytest

import sk8s
from sk8s.errors import MapFailed, RemoteException
from sk8s.future import JobFuture
from sk8s.local import LocalCluster


@pytest.fixture(autouse=True)
def local_backend():  # type: ignore[no-untyped-def]
    sk8s.configure(cluster=LocalCluster(max_workers=4))
    yield
    sk8s.configure(cluster=None)


class RecordingBackend:
    def logs(self, job_name: str) -> str:
        return f"logs for {job_name}"

    def cancel(self, job_name: str) -> bool:
        return True


class StubCluster:
    """Records submissions; futures are resolved by the test, not by a worker."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self.futures: list[JobFuture] = []
        self._lock = threading.Lock()

    def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
        fut = JobFuture(f"job-{len(self.futures)}", "wf-stub", backend=RecordingBackend())
        with self._lock:
            self.calls.append((func, args, kwargs))
            self.futures.append(fut)
        return fut

    def n_submitted(self) -> int:
        with self._lock:
            return len(self.futures)


# --- brief tests (real LocalCluster) -----------------------------------------

def test_batched_map_results_match_unbatched() -> None:
    assert sk8s.map(lambda x: x + 1, range(10), batch_size=3) == list(range(1, 11))


def test_batched_map_per_item_errors() -> None:
    def maybe(x: int) -> int:
        if x in (2, 5):
            raise ValueError(f"bad {x}")
        return x

    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(maybe, range(6), batch_size=4)
    assert sorted(i for i, _ in exc_info.value.failures) == [2, 5]
    assert exc_info.value.results == {0: 0, 1: 1, 3: 3, 4: 4}


def test_batched_submit_map_futures() -> None:
    futs = sk8s.submit_map(lambda x: x * 2, range(5), batch_size=2)
    assert len(futs) == 5
    assert [f.result(timeout=60) for f in futs] == [0, 2, 4, 6, 8]


def test_batched_starmap() -> None:
    assert sk8s.starmap(lambda a, b: a * b, [(2, 3), (4, 5)], batch_size=2) == [6, 20]


def test_max_concurrent_caps_inflight() -> None:
    # Completion only: the exact cap is verified deterministically against a
    # stub cluster in test_max_concurrent_blocks_submission_at_cap below.
    def tracked(x: int) -> int:
        return x

    futs = sk8s.submit_map(tracked, range(8), max_concurrent=2)
    assert [f.result(timeout=60) for f in futs] == list(range(8))


# --- batching details ---------------------------------------------------------

def test_batched_forwards_func_kwargs_and_job_options() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    sk8s.submit_map(lambda x, scale=1: x * scale, [1, 2, 3], batch_size=2, scale=10, image="img:1")
    assert len(stub.calls) == 2
    for _, _, kwargs in stub.calls:
        assert kwargs == {"image": "img:1"}  # job option rides on submit()


def test_batched_func_kwargs_reach_function() -> None:
    assert sk8s.map(lambda x, scale=1: x * scale, [1, 2, 3], batch_size=2, scale=10) == [10, 20, 30]
    assert sk8s.starmap(lambda a, b, k=0: a + b + k, [(1, 2), (3, 4)], batch_size=2, k=100) == [103, 107]


def test_batched_item_futures_inherit_parent_identity_and_logs() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    futs = sk8s.submit_map(lambda x: x, range(5), batch_size=2)
    assert stub.n_submitted() == 3  # ceil(5 / 2) jobs
    assert [f.job_name for f in futs] == ["job-0", "job-0", "job-1", "job-1", "job-2"]
    assert all(f.workflow_id == "wf-stub" for f in futs)
    assert futs[3].logs() == "logs for job-1"
    assert not any(f.done() for f in futs)


def test_batched_items_resolve_from_parent_outcomes() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    futs = sk8s.submit_map(lambda x: x, range(3), batch_size=2)
    boom = ValueError("boom")
    stub.futures[0].set_result([("ok", "a"), ("err", boom)])
    stub.futures[1].set_result([("ok", "c")])
    assert futs[0].result(timeout=1) == "a"
    assert futs[1].exception(timeout=1) is boom
    assert futs[2].result(timeout=1) == "c"


def test_whole_batch_failure_propagates_to_every_item_in_chunk() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    futs = sk8s.submit_map(lambda x: x, range(5), batch_size=2)
    infra = RuntimeError("pod evicted")
    stub.futures[0].set_exception(infra)  # chunk 0 = items 0, 1
    stub.futures[1].set_result([("ok", 2), ("ok", 3)])
    stub.futures[2].set_result([("ok", 4)])
    assert futs[0].exception(timeout=1) is infra
    assert futs[1].exception(timeout=1) is infra
    assert [f.result(timeout=1) for f in futs[2:]] == [2, 3, 4]


def test_batched_map_reports_whole_batch_failure_per_item() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    threading.Timer(0.2, lambda: stub.futures[0].set_exception(RuntimeError("gone"))).start()
    threading.Timer(0.2, lambda: stub.futures[1].set_result([("ok", 2)])).start()
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(lambda x: x, range(3), batch_size=2, timeout=10)
    assert [i for i, _ in exc_info.value.failures] == [0, 1]


def test_unpicklable_exception_degrades_to_remote_exception() -> None:
    class Unpicklable(Exception):
        def __reduce__(self) -> Any:
            raise TypeError("cannot pickle")

    def bad(x: int) -> int:
        raise Unpicklable("nope")

    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(bad, range(2), batch_size=2)
    errs = [e for _, e in exc_info.value.failures]
    assert len(errs) == 2
    assert all(isinstance(e, RemoteException) for e in errs)
    assert "nope" in str(errs[0])


def test_empty_iterable() -> None:
    assert sk8s.submit_map(lambda x: x, [], batch_size=3) == []
    assert sk8s.map(lambda x: x, [], max_concurrent=2) == []


def test_batch_size_one_submits_one_job_per_item() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    futs = sk8s.submit_map(lambda x: x, [7, 8], batch_size=1, image="i")
    assert futs == stub.futures
    assert [(a, k) for _, a, k in stub.calls] == [((7,), {"image": "i"}), ((8,), {"image": "i"})]


def test_unbatched_starmap_splats_item_args() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    sk8s.submit_starmap(lambda a, b: a, [(1, 2), (3, 4)])
    assert [a for _, a, _ in stub.calls] == [(1, 2), (3, 4)]


@pytest.mark.parametrize("opt", ["batch_size", "max_concurrent"])
def test_invalid_fanout_values_rejected(opt: str) -> None:
    with pytest.raises(ValueError, match=opt):
        sk8s.submit_map(lambda x: x, [1], **{opt: 0})


# --- max_concurrent wiring ----------------------------------------------------

def _wait_for(pred: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_max_concurrent_blocks_submission_at_cap() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    out: list[Any] = []
    t = threading.Thread(
        target=lambda: out.append(sk8s.submit_map(lambda x: x, range(4), max_concurrent=2))
    )
    t.start()
    assert _wait_for(lambda: stub.n_submitted() == 2)
    time.sleep(0.2)
    assert stub.n_submitted() == 2  # third submit is blocked on the semaphore
    stub.futures[0].set_result(0)
    assert _wait_for(lambda: stub.n_submitted() == 3)
    time.sleep(0.2)
    assert stub.n_submitted() == 3
    stub.futures[1].set_result(1)
    assert _wait_for(lambda: stub.n_submitted() == 4)
    t.join(timeout=5)
    assert not t.is_alive()
    assert len(out[0]) == 4


def test_max_concurrent_counts_batches_not_items() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    t = threading.Thread(
        target=lambda: sk8s.submit_map(lambda x: x, range(6), batch_size=2, max_concurrent=1)
    )
    t.start()
    assert _wait_for(lambda: stub.n_submitted() == 1)
    time.sleep(0.2)
    assert stub.n_submitted() == 1
    stub.futures[0].set_result([("ok", 0), ("ok", 1)])
    assert _wait_for(lambda: stub.n_submitted() == 2)
    stub.futures[1].set_result([("ok", 2), ("ok", 3)])
    assert _wait_for(lambda: stub.n_submitted() == 3)
    t.join(timeout=5)
    assert not t.is_alive()


def test_gather_does_not_mask_non_timeout_error_on_pending_future() -> None:
    from sk8s.api import _gather

    class Pending(JobFuture):
        def done(self) -> bool:
            return False

    fut = Pending("j", "w", backend=RecordingBackend())
    fut.set_exception(RuntimeError("backend exploded"))
    with pytest.raises(MapFailed) as exc_info:
        _gather([fut], timeout=5.0)
    (_, err), = exc_info.value.failures
    assert isinstance(err, RuntimeError)
    assert not isinstance(err, concurrent.futures.TimeoutError)


# --- cancellation -------------------------------------------------------------

def test_cancelling_one_item_cancels_whole_batch_job() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    futs = sk8s.submit_map(lambda x: x, range(4), batch_size=2)
    assert futs[1].cancel() is True
    assert stub.futures[0].cancelled()
    for f in futs[:2]:
        assert f.done() and f.cancelled()
        with pytest.raises(concurrent.futures.CancelledError):
            f.result(timeout=1)
    assert not futs[2].done() and not futs[3].done()  # other batch untouched
    assert futs[0].cancel() is False  # already terminal


def test_cancelled_batch_item_wakes_waiters() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    futs = sk8s.submit_map(lambda x: x, range(2), batch_size=2)
    futs[0].cancel()
    done, not_done = concurrent.futures.wait(futs, timeout=1)
    assert len(done) == 2 and not not_done


def test_cancelling_batch_item_releases_max_concurrent_slot() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    t = threading.Thread(
        target=lambda: sk8s.submit_map(lambda x: x, range(4), batch_size=2, max_concurrent=1)
    )
    t.start()
    assert _wait_for(lambda: stub.n_submitted() == 1)
    time.sleep(0.2)
    assert stub.n_submitted() == 1  # second chunk waits for the slot
    # Item futures are not returned until submission finishes, so cancel the
    # parent job the way an item's cancel() does: through the job future.
    assert stub.futures[0].cancel()
    assert _wait_for(lambda: stub.n_submitted() == 2)
    t.join(timeout=5)
    assert not t.is_alive()


def test_local_cancel_of_batch_item_cancels_siblings() -> None:
    def slow(x: int) -> int:
        import time as _time
        _time.sleep(30)
        return x

    futs = sk8s.submit_map(slow, range(2), batch_size=2)
    assert futs[1].cancel() is True
    assert _wait_for(lambda: all(f.done() for f in futs))
    assert all(f.cancelled() for f in futs)


def test_short_outcome_list_fails_items_instead_of_hanging() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    futs = sk8s.submit_map(lambda x: x, range(3), batch_size=3)
    stub.futures[0].set_result([("ok", 0)])
    for f in futs:
        assert isinstance(f.exception(timeout=1), RuntimeError)


# --- map timeout covers submission --------------------------------------------

def test_map_timeout_bounds_blocking_on_max_concurrent() -> None:
    stub = StubCluster()  # jobs never complete
    sk8s.configure(cluster=stub)
    start = time.monotonic()
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(lambda x: x, range(3), max_concurrent=1, timeout=0.5)
    assert time.monotonic() - start < 1.5
    assert stub.n_submitted() == 1  # later items were never submitted
    failures = exc_info.value.failures
    assert [i for i, _ in failures] == [0, 1, 2]
    assert all(isinstance(e, concurrent.futures.TimeoutError) for _, e in failures)
    assert "not submitted" in str(failures[1][1])


def test_starmap_and_batched_map_timeout_bound_max_concurrent() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    start = time.monotonic()
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(lambda x: x, range(6), batch_size=2, max_concurrent=1, timeout=0.5)
    assert time.monotonic() - start < 1.5
    assert stub.n_submitted() == 1
    assert [i for i, _ in exc_info.value.failures] == [0, 1, 2, 3, 4, 5]
    with pytest.raises(MapFailed):
        sk8s.starmap(lambda a: a, [(1,), (2,)], max_concurrent=1, timeout=0.3)


def test_batched_map_with_expiring_timeout() -> None:
    def nap(seconds: float) -> float:
        import time as _time
        _time.sleep(seconds)
        return seconds

    start = time.monotonic()
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(nap, [3.0, 3.0, 3.0], batch_size=2, timeout=1.0)
    assert time.monotonic() - start < 3.0
    failures = exc_info.value.failures
    assert [i for i, _ in failures] == [0, 1, 2]
    assert all(isinstance(e, concurrent.futures.TimeoutError) for _, e in failures)
