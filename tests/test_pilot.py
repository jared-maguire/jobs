"""pilot=: run the first n items, gate the rest on all n being green."""
import threading
import time
from typing import Any

import pytest

import sk8s
from sk8s.errors import ExpectationFailed, MapFailed, PilotFailed
from sk8s.future import JobFuture
from sk8s.local import LocalCluster
from sk8s import expect as expectations


class _Backend:
    def logs(self, job_name: str) -> str:
        return ""

    def cancel(self, job_name: str) -> bool:
        return True


class InlineCluster:
    """Runs every submitted job synchronously (expect= applied like the worker would) and
    records what was submitted, so tests can assert on exactly which items ever ran."""

    def __init__(self) -> None:
        self.ran: list[Any] = []
        self.kwargs_seen: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
        expect = kwargs.pop("expect", None)
        with self._lock:
            self.ran.append(args)
            self.kwargs_seen.append(dict(kwargs))
        fut = JobFuture(f"job-{len(self.ran)}", "wf-inline", backend=_Backend())
        try:
            value = func(*args, **{k: v for k, v in kwargs.items() if k not in sk8s.cluster.JOB_OPTIONS})
            verdict = None if expect is None else expectations.check(expect, value)
            if verdict is not None:
                fut.set_exception(verdict.error)
            else:
                fut.set_result(value)
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut


class ManualCluster:
    """Futures stay pending until the test resolves them."""

    def __init__(self) -> None:
        self.futures: list[JobFuture] = []
        self._lock = threading.Lock()

    def submit(self, func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
        fut = JobFuture(f"job-{len(self.futures)}", "wf-manual", backend=_Backend())
        with self._lock:
            self.futures.append(fut)
        return fut

    def n(self) -> int:
        with self._lock:
            return len(self.futures)


@pytest.fixture()
def inline():  # type: ignore[no-untyped-def]
    c = InlineCluster()
    sk8s.configure(cluster=c)
    yield c
    sk8s.configure(cluster=None)


@pytest.fixture()
def manual():  # type: ignore[no-untyped-def]
    c = ManualCluster()
    sk8s.configure(cluster=c)
    yield c
    sk8s.configure(cluster=None)


def double(x: int) -> int:
    return x * 2


def bad_for_3(x: int) -> int:
    if x == 3:
        raise ValueError("three is bad")
    return x


def gate(x: int) -> str | None:
    return None if x < 100 else f"{x} is too big"


# --- green pilot -------------------------------------------------------------

def test_green_pilot_runs_the_rest_and_preserves_order(inline, serial_submission: None) -> None:  # type: ignore[no-untyped-def]
    assert sk8s.map(double, range(10), pilot=3) == [x * 2 for x in range(10)]
    assert [a[0] for a in inline.ran] == list(range(10))  # pilot first, then the remainder


def test_starmap_pilot(inline) -> None:  # type: ignore[no-untyped-def]
    assert sk8s.starmap(pow, [(2, 1), (2, 2), (2, 3), (2, 4)], pilot=2) == [2, 4, 8, 16]


def test_pilot_is_not_forwarded_to_jobs_or_the_function(inline) -> None:  # type: ignore[no-untyped-def]
    sk8s.map(double, range(4), pilot=2)
    assert all("pilot" not in kw for kw in inline.kwargs_seen)


def test_pilot_larger_than_input_is_the_whole_run(inline) -> None:  # type: ignore[no-untyped-def]
    assert sk8s.map(double, range(3), pilot=50) == [0, 2, 4]


def test_empty_input(inline) -> None:  # type: ignore[no-untyped-def]
    assert sk8s.map(double, [], pilot=3) == []
    assert inline.ran == []


# --- failing pilot -----------------------------------------------------------

def test_failed_pilot_submits_nothing_further(inline) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(bad_for_3, range(48), pilot=5)
    e = exc_info.value
    assert [(i, type(x)) for i, x in e.failures] == [(3, ValueError)]
    assert (e.pilot, e.total) == (5, 48)
    assert len(inline.ran) == 5  # the other 43 were never submitted


def test_pilot_waits_for_all_n_before_judging(inline) -> None:  # type: ignore[no-untyped-def]
    def fail_all(x: int) -> int:
        raise ValueError(x)

    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(fail_all, range(10), pilot=4)
    assert [i for i, _ in exc_info.value.failures] == [0, 1, 2, 3]
    assert len(inline.ran) == 4


def test_pilot_expectation_failure_is_a_pilot_failure(inline) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(double, [1, 2, 60, 4, 5, 6], pilot=3, expect=lambda r: gate(r))
    (idx, err), = exc_info.value.failures
    assert idx == 2 and isinstance(err, ExpectationFailed)
    assert "1 failed their expectation" in str(exc_info.value)
    assert len(inline.ran) == 3


def test_pilot_green_then_later_expectation_failure_is_plain_map_failed(inline) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(double, [1, 2, 3, 4, 90, 6], pilot=3, expect=lambda r: gate(r))
    e = exc_info.value
    assert not isinstance(e, PilotFailed)
    assert [i for i, _ in e.failures] == [4]  # index is the position in the full input
    assert isinstance(e.failures[0][1], ExpectationFailed)
    assert "1 failed their expectation" in str(e)
    assert len(inline.ran) == 6


def test_whole_run_pilot_failing_is_pilot_failed(inline) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed):
        sk8s.map(bad_for_3, range(5), pilot=5)


# --- composition ---------------------------------------------------------------

def test_pilot_with_batch_size(inline) -> None:  # type: ignore[no-untyped-def]
    assert sk8s.map(double, range(10), pilot=4, batch_size=3) == [x * 2 for x in range(10)]
    # pilot of 4 items = 2 batch jobs (3 + 1), then the remaining 6 = 2 batch jobs
    assert len(inline.ran) == 4


def test_failed_pilot_with_batch_size_submits_no_further_batches(inline) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(bad_for_3, range(30), pilot=4, batch_size=2)
    assert [i for i, _ in exc_info.value.failures] == [3]
    assert len(inline.ran) == 2  # two pilot batches only


def test_pilot_with_max_concurrent(inline) -> None:  # type: ignore[no-untyped-def]
    assert sk8s.map(double, range(8), pilot=2, max_concurrent=2) == [x * 2 for x in range(8)]


def test_pilot_timeout_is_a_pilot_failure_and_stops_the_run(manual) -> None:  # type: ignore[no-untyped-def]
    t0 = time.monotonic()
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(double, range(20), pilot=3, timeout=0.3)
    assert time.monotonic() - t0 < 5
    assert len(exc_info.value.failures) == 3
    assert manual.n() == 3  # remainder never submitted


def test_pilot_shares_the_overall_deadline(manual) -> None:  # type: ignore[no-untyped-def]
    def resolve_pilot_soon() -> None:
        while manual.n() < 2:
            time.sleep(0.01)
        time.sleep(0.2)
        for i, fut in enumerate(manual.futures[:2]):
            fut.set_result(i)

    threading.Thread(target=resolve_pilot_soon, daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(double, range(5), pilot=2, timeout=0.6)  # the 3 remainder jobs never resolve
    elapsed = time.monotonic() - t0
    assert 0.5 < elapsed < 2.0  # one 0.6s budget for pilot + rest, not 0.6 each
    assert [i for i, _ in exc_info.value.failures] == [2, 3, 4]
    assert manual.n() == 5


# --- submit_map ------------------------------------------------------------------

def test_submit_map_pilot_blocks_until_the_pilot_is_green(manual, serial_submission: None) -> None:  # type: ignore[no-untyped-def]
    returned = threading.Event()
    out: list[Any] = []

    def call() -> None:
        out.extend(sk8s.submit_map(double, range(6), pilot=2))
        returned.set()

    threading.Thread(target=call, daemon=True).start()
    deadline = time.monotonic() + 5
    while manual.n() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    assert manual.n() == 2 and not returned.is_set()  # blocked; remainder not yet submitted
    for fut in manual.futures[:2]:
        fut.set_result("ok")
    assert returned.wait(5)
    assert len(out) == 6 and manual.n() == 6  # one future per item, input order
    assert out[0] is manual.futures[0] and out[5] is manual.futures[5]


def test_submit_map_pilot_failure_raises_and_submits_nothing_more(inline) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed):
        sk8s.submit_map(bad_for_3, range(10), pilot=4)
    assert len(inline.ran) == 4


def test_submit_map_pilot_honours_timeout_for_the_pilot_wait(manual) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed):
        sk8s.submit_map(double, range(10), pilot=2, timeout=0.2)
    assert manual.n() == 2


def test_submit_starmap_pilot(inline) -> None:  # type: ignore[no-untyped-def]
    futs = sk8s.submit_starmap(pow, [(2, 1), (2, 2), (2, 3)], pilot=1)
    assert [f.result(timeout=5) for f in futs] == [2, 4, 8]


# --- validation --------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1])
def test_pilot_must_be_positive(inline, bad: int) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="pilot"):
        sk8s.map(double, range(3), pilot=bad)
    assert inline.ran == []


@pytest.mark.parametrize("bad", [1.5, "3", True])
def test_pilot_must_be_an_int(inline, bad: object) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TypeError, match="pilot"):
        sk8s.map(double, range(3), pilot=bad)
    assert inline.ran == []


# --- the napkin, end to end on the local backend ---------------------------------------

def test_napkin_pilot_green_then_quarantine_one_sample(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sk8s.configure(cluster=LocalCluster(max_workers=4))
    try:
        def percent_mapped(sample: int) -> float:
            return 0.61 if sample == 23 else 0.97

        def mapped_over_90(frac: float) -> str | None:
            return None if frac >= 0.9 else f"{frac:.0%} mapped (need 90%)"

        with pytest.raises(MapFailed) as exc_info:
            sk8s.map(percent_mapped, range(1, 31), pilot=3, expect=mapped_over_90)
        e = exc_info.value
        assert [i for i, _ in e.failures] == [22]  # S23 (index 22): quarantined, the rest ran
        assert "1 failed their expectation, 0 errored" in str(e)
    finally:
        sk8s.configure(cluster=None)


def test_pilot_failed_results_are_the_pilot_successes(inline) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(bad_for_3, range(10), pilot=5)
    assert exc_info.value.results == {0: 0, 1: 1, 2: 2, 4: 4}


def test_pilot_timeout_message_names_the_real_timeout(manual) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.map(double, range(5), pilot=2, timeout=0.2)
    msgs = {str(e) for _, e in exc_info.value.failures}
    assert msgs == {"not finished within map timeout of 0.2s"}
    assert "Nones" not in str(exc_info.value)


def test_submit_map_pilot_timeout_message_names_the_real_timeout(manual) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PilotFailed) as exc_info:
        sk8s.submit_map(double, range(5), pilot=2, timeout=0.2)
    assert {str(e) for _, e in exc_info.value.failures} == {
        "not finished within map timeout of 0.2s"
    }


def test_batched_map_without_expect_keeps_the_v2_call_shape(manual) -> None:  # type: ignore[no-untyped-def]
    # A batch job with no gate must not mention expect at all, so it still runs on a worker
    # image that predates expect=: _run_batch(func, chunk, star, kwargs), four positionals.
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_submit = manual.submit

    def recording(func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
        calls.append((args, kwargs))
        return real_submit(func, *args, **kwargs)

    manual.submit = recording  # type: ignore[method-assign]
    sk8s.submit_map(double, range(4), batch_size=2, image="img:1")
    assert len(calls) == 2
    for args, kwargs in calls:
        assert len(args) == 4  # (func, chunk, star, func_kwargs)
        assert "expect" not in kwargs and kwargs == {"image": "img:1"}


def test_batched_map_with_expect_rides_inside_the_batch_not_as_the_job_option(manual) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_submit = manual.submit

    def recording(func: Any, /, *args: Any, **kwargs: Any) -> JobFuture:
        calls.append((args, kwargs))
        return real_submit(func, *args, **kwargs)

    manual.submit = recording  # type: ignore[method-assign]
    gate_fn = lambda r: None  # noqa: E731
    sk8s.submit_map(double, range(2), batch_size=2, expect=gate_fn)
    (args, kwargs), = calls
    assert len(args) == 5 and args[4] is gate_fn  # the per-item gate is a _run_batch argument
    assert "expect" not in kwargs  # NOT the job option: that would gate the batch's outcome list
