import threading
import time
from typing import Iterator

from sk8s.watch import JobEvent, JobWatcher


def collect_resolver():  # type: ignore[no-untyped-def]
    resolved: dict[str, str] = {}
    done = threading.Event()

    def resolve(name: str, phase: str) -> None:
        resolved[name] = phase
        done.set()

    return resolved, done, resolve


def test_terminal_event_resolves() -> None:
    resolved, done, resolve = collect_resolver()

    def events() -> Iterator[JobEvent]:
        yield ("job-a", "active")
        yield ("job-a", "succeeded")
        while True:
            time.sleep(0.01)
            yield ("job-other", "active")

    w = JobWatcher(events, resolve)
    w.register("job-a")
    assert done.wait(5)
    assert resolved == {"job-a": "succeeded"}
    w.stop()


def test_event_before_registration_still_resolves() -> None:
    resolved, done, resolve = collect_resolver()
    started = threading.Event()

    def events() -> Iterator[JobEvent]:
        yield ("job-b", "failed")
        started.set()
        while True:
            time.sleep(0.01)
            yield ("job-x", "active")

    w = JobWatcher(events, resolve)
    assert started.wait(5)
    w.register("job-b")           # terminal event already passed
    assert done.wait(5)
    assert resolved == {"job-b": "failed"}
    w.stop()


def test_resolve_called_once() -> None:
    calls: list[str] = []
    done = threading.Event()

    def resolve(name: str, phase: str) -> None:
        calls.append(name)
        done.set()

    def events() -> Iterator[JobEvent]:
        yield ("job-c", "succeeded")
        yield ("job-c", "succeeded")
        while True:
            time.sleep(0.01)
            yield ("job-x", "active")

    w = JobWatcher(events, resolve)
    w.register("job-c")
    assert done.wait(5)
    time.sleep(0.1)
    assert calls == ["job-c"]
    w.stop()


def test_stream_crash_reconnects() -> None:
    resolved, done, resolve = collect_resolver()
    attempt = {"n": 0}

    def events() -> Iterator[JobEvent]:
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise ConnectionError("watch dropped")
        yield ("job-d", "succeeded")
        while True:
            time.sleep(0.01)
            yield ("job-x", "active")

    w = JobWatcher(events, resolve, reconnect_delay=0.05)
    w.register("job-d")
    assert done.wait(5)
    assert resolved == {"job-d": "succeeded"}
    w.stop()


def test_raising_resolve_does_not_kill_watcher() -> None:
    bad_raised = threading.Event()
    resolved, done, resolve_ok = collect_resolver()
    gate = threading.Event()

    def resolve(name: str, phase: str) -> None:
        if name == "job-bad":
            bad_raised.set()
            raise RuntimeError("boom")
        resolve_ok(name, phase)

    def events() -> Iterator[JobEvent]:
        yield ("job-bad", "failed")
        gate.wait(5)
        yield ("job-good", "succeeded")
        while True:
            time.sleep(0.01)
            yield ("job-x", "active")

    # A long reconnect_delay proves the stream was NOT torn down by the raise.
    w = JobWatcher(events, resolve, reconnect_delay=30)
    try:
        w.register("job-bad")
        assert bad_raised.wait(5)
        w.register("job-good")
        gate.set()
        assert done.wait(5)
        assert resolved == {"job-good": "succeeded"}
    finally:
        gate.set()
        w.stop()


def test_normal_stream_end_reinvokes_after_delay() -> None:
    stamps: list[float] = []
    three = threading.Event()

    def events() -> Iterator[JobEvent]:
        stamps.append(time.monotonic())
        if len(stamps) >= 3:
            three.set()
        return iter(())

    w = JobWatcher(events, lambda n, p: None, reconnect_delay=0.1)
    try:
        assert three.wait(5)
    finally:
        w.stop()
    w._thread.join(5)
    assert not w._thread.is_alive()
    # Re-invoked each time, and never faster than the delay (no hot loop).
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.09 for g in gaps), gaps


def test_stop_interrupts_reconnect_wait() -> None:
    first = threading.Event()

    def events() -> Iterator[JobEvent]:
        first.set()
        return iter(())

    w = JobWatcher(events, lambda n, p: None, reconnect_delay=60)
    try:
        assert first.wait(5)
    finally:
        w.stop()
    w._thread.join(5)
    assert not w._thread.is_alive()


def test_expired_buffered_event_is_not_resolved() -> None:
    now = {"t": 1000.0}
    buffered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def events() -> Iterator[JobEvent]:
        yield ("job-old", "succeeded")
        # Runs only after the watcher thread has finished handling the event above.
        buffered.set()
        release.wait(5)
        yield ("job-x", "active")

    w = JobWatcher(events, lambda n, p: calls.append(n), clock=lambda: now["t"])
    try:
        assert buffered.wait(5)
        now["t"] += 61
        w.register("job-old")
        assert calls == []
        # Stale entry was discarded, and the job now waits for a fresh terminal event.
        assert "job-old" in w._registered
        assert "job-old" not in w._recent_terminal
    finally:
        release.set()
        w.stop()


def test_fresh_buffered_event_resolves_within_ttl() -> None:
    now = {"t": 1000.0}
    buffered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def events() -> Iterator[JobEvent]:
        yield ("job-new", "succeeded")
        buffered.set()
        release.wait(5)
        yield ("job-x", "active")

    w = JobWatcher(events, lambda n, p: calls.append(n), clock=lambda: now["t"])
    try:
        assert buffered.wait(5)
        now["t"] += 59
        w.register("job-new")
        assert calls == ["job-new"]
    finally:
        release.set()
        w.stop()


def test_registered_returns_a_snapshot_copy() -> None:
    _, _, resolve = collect_resolver()

    def events() -> Iterator[JobEvent]:
        while True:
            time.sleep(0.01)
            yield ("x", "active")

    w = JobWatcher(events, resolve)
    w.register("a")
    w.register("b")
    snap = w.registered()
    assert snap == {"a", "b"}
    snap.add("zzz")  # mutating the snapshot must not touch the watcher
    w.unregister("a")
    assert w.registered() == {"b"}
    w.stop()
