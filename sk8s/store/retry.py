"""The ONE bounded, jittered retry helper for optimistic (conditional) store writes.

Spike (SPIKE-CCW.md, E2/E7): under 10-way contention on one hot key, GCS and the ConfigMap
store both converge with no lost updates, but a writer can lose ~5 races per success (worst
case 8-9 for one increment, 38 over five). An unbounded loop has no fairness, so every
optimistic loop in sk8s goes through here: a hard attempt cap and full-jitter exponential
backoff (sleep ~ U(0, min(cap, base * 2**attempt))) so that contenders de-synchronise.

Mutations passed to ``update_blob`` MUST be idempotent set-style merges. The Python clients
auto-retry some failures; if a conditional write lands server-side but the ack is lost, the
retry loses against our own write and the loop re-runs the mutation on the new content. An
idempotent merge makes that harmless; a counter increment would be applied twice.
"""
from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Callable, TypeVar

from sk8s.errors import BlobNotFound, RetryExhausted

if TYPE_CHECKING:
    from sk8s.store.base import BlobStore

T = TypeVar("T")

MAX_ATTEMPTS = 12
BASE_DELAY = 0.05  # seconds
MAX_DELAY = 2.0  # seconds; the cap on one sleep, so 12 attempts bound a loop near 10s


def backoff_delay(
    attempt: int,
    *,
    base: float = BASE_DELAY,
    cap: float = MAX_DELAY,
    rng: Callable[[float, float], float] = random.uniform,
) -> float:
    """Full-jitter delay before retry number ``attempt`` (0-based)."""
    return rng(0.0, min(cap, base * (2 ** attempt)))


def retry_conflicts(
    attempt_fn: Callable[[], T | None],
    *,
    what: str,
    max_attempts: int = MAX_ATTEMPTS,
    base: float = BASE_DELAY,
    cap: float = MAX_DELAY,
    sleep: Callable[[float], None] | None = None,
    rng: Callable[[float, float], float] = random.uniform,
) -> T:
    """Call ``attempt_fn`` until it returns non-None, sleeping jittered between losses.

    ``attempt_fn`` returns None when it lost a race (a precondition failure) and a value
    when it won. Raises RetryExhausted after ``max_attempts`` losses."""
    pause = sleep if sleep is not None else time.sleep  # looked up late so tests can patch it
    for attempt in range(max_attempts):
        result = attempt_fn()
        if result is not None:
            return result
        if attempt < max_attempts - 1:
            pause(backoff_delay(attempt, base=base, cap=cap, rng=rng))
    raise RetryExhausted(
        f"{what}: lost the race {max_attempts} times in a row (a very hot key?); giving up "
        f"rather than spinning. Retrying later is safe."
    )


def update_blob(
    store: BlobStore,
    key: str,
    mutate: Callable[[bytes | None], bytes | None],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
    rng: Callable[[float, float], float] = random.uniform,
) -> bytes | None:
    """Optimistic read-modify-write of one blob: the single entry point for manifest updates.

    ``mutate(current)`` receives the current bytes (None if the blob does not exist) and
    returns the new bytes, or None for "no change needed". It may run several times, so it
    must be a pure, idempotent set-style merge. Returns the bytes now stored (the winning
    write, or the unchanged current content)."""

    def attempt() -> tuple[bytes | None] | None:
        try:
            current, version = store.read_with_version(key)
        except BlobNotFound:
            new = mutate(None)
            if new is None:
                return (None,)
            return (new,) if store.put_if_absent(key, new) else None
        new = mutate(current)
        if new is None:
            return (current,)
        return (new,) if store.put_if_version(key, new, version) else None

    won = retry_conflicts(
        attempt, what=f"updating {key!r}", max_attempts=max_attempts, sleep=sleep, rng=rng
    )
    return won[0]
