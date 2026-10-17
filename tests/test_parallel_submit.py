"""Parallel fan-out submission (api._submit_many's bounded pool, Plan 12 Workstream C).

What must SURVIVE parallelism: the returned futures' input order, the budget's
max_concurrent_pods cap, memo-hit short-circuiting, and clean error unwinding.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

import sk8s
from sk8s.budget import Budget
from sk8s.future import JobFuture
from tests.test_memo import Harness as MemoHarness
from tests.test_memo import _close_clusters  # noqa: F401  (autouse fixture)


@pytest.fixture(autouse=True)
def reset_api() -> Iterator[None]:
    sk8s.configure()
    yield
    sk8s.configure()


def double(x: int) -> int:
    return x * 2


class _Backend:
    def logs(self, job_name: str) -> str:
        return ""

    def cancel(self, job_name: str) -> bool:
        return True


# --- order preservation ---------------------------------------------------------------


class OverlapCluster:
    """Proves submissions overlap: every submit() rendezvouses in pairs (which only a
    parallel caller can satisfy), then early items finish their submission LAST."""

    def __init__(self, n_items: int) -> None:
        self.barrier = threading.Barrier(2, timeout=10)  # breaks (test fails) if serial
        self.n_items = n_items

    def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
        self.barrier.wait()
        time.sleep((self.n_items - args[0]) * 0.01)
        fut = JobFuture(f"job-{args[0]}", "wf-overlap", backend=_Backend())
        fut.set_result(func(*args))
        return fut


def test_parallel_fan_out_keeps_futures_in_input_order() -> None:
    c = OverlapCluster(8)
    sk8s.configure(cluster=c)
    futs = sk8s.submit_map(double, range(8))
    # futures[i] belongs to item i even though submissions completed in scrambled order
    assert [f.job_name for f in futs] == [f"job-{i}" for i in range(8)]
    assert [f.result(timeout=5) for f in futs] == [x * 2 for x in range(8)]


def test_parallel_batched_fan_out_keeps_item_order() -> None:
    class Scramble:
        """Chunks that come first finish their submission last."""

        def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
            chunk = args[1]  # _run_batch(func, chunk, star, kwargs)
            time.sleep((9 - chunk[0]) * 0.005)
            fut = JobFuture(f"job-{chunk[0]}", "wf-scramble", backend=_Backend())
            fut.set_result([("ok", x * 10) for x in chunk])
            return fut

    sk8s.configure(cluster=Scramble())
    futs = sk8s.submit_map(lambda x: x * 10, range(9), batch_size=2)
    assert [f.result(timeout=5) for f in futs] == [x * 10 for x in range(9)]


# --- budget concurrency cap -----------------------------------------------------------


class MeteredCluster:
    """Counts unresolved jobs; each resolves a moment later on a timer thread."""

    def __init__(self, settle: float = 0.03) -> None:
        self._lock = threading.Lock()
        self.settle = settle
        self.open = 0
        self.peak = 0
        self.submitted = 0

    def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
        with self._lock:
            self.open += 1
            self.submitted += 1
            self.peak = max(self.peak, self.open)
        fut = JobFuture(f"job-{self.submitted}", "wf-metered", backend=_Backend())

        def close(_: Any) -> None:
            with self._lock:
                self.open -= 1

        # Registered before the fan-out's slot-release callback, so `open` is decremented
        # before a new submission can be admitted: `peak` is an honest in-flight count.
        fut.add_done_callback(close)
        threading.Timer(self.settle, fut.set_result, args=(func(*args),)).start()
        return fut


def test_budget_concurrency_cap_holds_under_parallel_fan_out() -> None:
    c = MeteredCluster()
    sk8s.configure(cluster=c)
    results = sk8s.map(
        lambda x: x + 1, range(24), budget=Budget(max_concurrent_pods=3), timeout=30
    )
    assert results == [x + 1 for x in range(24)]
    assert c.submitted == 24
    assert c.peak <= 3  # racing submitters never overran the budget's cap
    assert c.peak >= 2  # and the cap was actually exercised, not one-at-a-time


def test_explicit_max_concurrent_holds_under_parallel_fan_out() -> None:
    c = MeteredCluster()
    sk8s.configure(cluster=c)
    futs = sk8s.submit_map(lambda x: x, range(16), max_concurrent=2)
    assert [f.result(timeout=30) for f in futs] == list(range(16))
    assert c.submitted == 16 and c.peak <= 2


# --- memo hits ------------------------------------------------------------------------


def test_memo_hits_short_circuit_in_parallel_fan_out(tmp_path: Path) -> None:
    h = MemoHarness(tmp_path)
    sk8s.configure(cluster=h.cluster)
    first = sk8s.submit_map(double, range(10), memo=True)
    assert not any(f.memo_hit for f in first) and len(h.batch.jobs) == 10
    for f in first:
        h.finish(f.job_name)
    assert [f.result(timeout=10) for f in first] == [x * 2 for x in range(10)]
    jobs = len(h.batch.jobs)
    again = sk8s.submit_map(double, list(range(10)), memo=True)
    assert all(f.memo_hit for f in again)  # parallel lookups; no Job created for any hit
    assert len(h.batch.jobs) == jobs
    assert [f.result(timeout=10) for f in again] == [x * 2 for x in range(10)]
    mixed = sk8s.submit_map(double, [7, 11, 3, 12], memo=True)  # hits keep their positions
    assert [f.memo_hit for f in mixed] == [True, False, True, False]
    assert len(h.batch.jobs) == jobs + 2


# --- error unwinding ------------------------------------------------------------------


def test_submission_error_raises_the_lowest_indexed_failure() -> None:
    class Boom:
        def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
            raise RuntimeError(f"boom {args[0]}")

    sk8s.configure(cluster=Boom())
    with pytest.raises(RuntimeError, match="boom 0"):
        sk8s.submit_map(lambda x: x, range(8))


@pytest.mark.timeout(30)
def test_submission_error_does_not_deadlock_a_bounded_fan_out() -> None:
    class FirstBoom:
        """Item 0 fails; every other job would hold its max_concurrent slot forever."""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.pending: list[JobFuture] = []

        def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
            if args[0] == 0:
                raise RuntimeError("boom 0")
            fut = JobFuture(f"job-{args[0]}", "wf-firstboom", backend=_Backend())
            with self._lock:
                self.pending.append(fut)  # never resolved
            return fut

    sk8s.configure(cluster=FirstBoom())
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="boom 0"):
        sk8s.submit_map(lambda x: x, range(6), max_concurrent=2)
    # The caller was blocked waiting for a slot that would never free; the failure
    # must wake it instead of deadlocking the fan-out.
    assert time.monotonic() - start < 10
