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
