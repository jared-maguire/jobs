"""One watcher thread per Cluster resolves futures from job events. No polling."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterator

JobEvent = tuple[str, str]  # (job_name, phase)
TERMINAL = {"succeeded", "failed", "imagepull"}
_RECENT_TTL = 60.0

log = logging.getLogger("sk8s.watch")


class JobWatcher:
    def __init__(
        self,
        events: Callable[[], Iterator[JobEvent]],
        resolve: Callable[[str, str], None],
        reconnect_delay: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._events = events
        self._resolve = resolve
        self._reconnect_delay = reconnect_delay
        self._clock = clock
        self._lock = threading.Lock()
        self._registered: set[str] = set()
        self._recent_terminal: dict[str, tuple[str, float]] = {}
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sk8s-watch")
        self._thread.start()

    def register(self, job_name: str) -> None:
        with self._lock:
            hit = self._recent_terminal.pop(job_name, None)
            if hit is None or self._clock() - hit[1] >= _RECENT_TTL:
                # Nothing buffered, or the buffered event is stale: wait for a fresh one.
                self._registered.add(job_name)
                return
            phase = hit[0]
        self._safe_resolve(job_name, phase)

    def unregister(self, job_name: str) -> None:
        with self._lock:
            self._registered.discard(job_name)

    def stop(self) -> None:
        self._stopped.set()

    def _safe_resolve(self, name: str, phase: str) -> None:
        # A raising resolver must never kill the stream or strand other futures.
        try:
            self._resolve(name, phase)
        except Exception:
            log.exception("resolve callback failed for job %s (phase %s)", name, phase)

    def _handle(self, name: str, phase: str) -> None:
        if phase not in TERMINAL:
            return
        with self._lock:
            if name in self._registered:
                self._registered.discard(name)
                fire = True
            else:
                now = self._clock()
                self._recent_terminal[name] = (phase, now)
                self._recent_terminal = {
                    k: v for k, v in self._recent_terminal.items()
                    if now - v[1] < _RECENT_TTL
                }
                fire = False
        if fire:
            self._safe_resolve(name, phase)

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                for name, phase in self._events():
                    if self._stopped.is_set():
                        return
                    self._handle(name, phase)
            except Exception:
                log.exception("job event stream failed; reconnecting")
            # Stream ended (normally or not): back off, interruptibly, then reconnect.
            self._stopped.wait(self._reconnect_delay)
