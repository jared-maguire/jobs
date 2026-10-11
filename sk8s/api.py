"""Module-level API over an implicit default cluster (requests-Session pattern)."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable

from sk8s.errors import MapFailed
from sk8s.future import JobFuture

_lock = threading.Lock()
_default_cluster: Any | None = None
_config_overrides: dict[str, Any] = {}


def configure(cluster: Any | None = None, **config_overrides: Any) -> None:
    """Set or reset the default cluster used by the module-level API.

    - ``configure(cluster=obj)``: swap ``obj`` in as the default cluster.
      Stored config overrides are left untouched.
    - ``configure(**overrides)``: merge the overrides into the stored ones and
      drop the current default cluster, so the next use builds a new
      ``sk8s.cluster.Cluster`` from the merged overrides.
    - ``configure()`` or ``configure(cluster=None)``: full reset. Clears the
      default cluster and all stored overrides.
    - Passing both ``cluster`` and overrides raises ``ValueError``.
    """
    global _default_cluster, _config_overrides
    if cluster is not None and config_overrides:
        raise ValueError("configure() takes a cluster or config overrides, not both")
    with _lock:
        if cluster is not None:
            _default_cluster = cluster
        elif config_overrides:
            _config_overrides = {**_config_overrides, **config_overrides}
            _default_cluster = None
        else:
            _default_cluster = None
            _config_overrides = {}


def _cluster() -> Any:
    global _default_cluster
    with _lock:
        if _default_cluster is None:
            from sk8s.cluster import Cluster
            _default_cluster = Cluster(**_config_overrides)
        return _default_cluster


def run(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` as a job and return its result.

    Keyword arguments named in ``sk8s.cluster.JOB_OPTIONS`` (e.g. ``image``,
    ``limits``, ``name_prefix``) set job options and are not passed to ``func``.
    ``timeout`` bounds the wait and is never passed to ``func``. Every other
    keyword argument is forwarded to ``func``.
    """
    return _cluster().run(func, *args, **kwargs)


def submit(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
    """Submit ``func`` as a job and return a ``JobFuture`` without waiting.

    Keyword arguments are split as in ``run``: names in
    ``sk8s.cluster.JOB_OPTIONS`` become job options, all others go to ``func``.
    ``timeout`` is accepted and discarded here; pass it to ``future.result()``.
    """
    fut: JobFuture = _cluster().submit(func, *args, **kwargs)
    return fut


def submit_map(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    """Submit ``func(item)`` for each item; return the futures in input order.

    ``opts`` are split as in ``submit``: job-option names configure the jobs,
    all other keyword arguments are forwarded to ``func``.
    """
    return [submit(func, item, **opts) for item in iterable]


def submit_starmap(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    """Submit ``func(*item)`` for each item; return the futures in input order.

    ``opts`` are split as in ``submit``.
    """
    return [submit(func, *item, **opts) for item in iterable]


def _gather(futures: list[JobFuture], timeout: float | None) -> list[Any]:
    """Collect results in input order, waiting at most ``timeout`` in total.

    Every future is visited even after a failure. Items still pending when the
    overall deadline passes are recorded as ``TimeoutError`` failures; items
    already finished keep their results. ``KeyboardInterrupt`` and other
    non-``Exception`` signals propagate immediately.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    results: list[Any] = []
    failures: list[tuple[int, BaseException]] = []
    for i, fut in enumerate(futures):
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            results.append(fut.result(timeout=remaining))
        except Exception as exc:
            if deadline is not None and not fut.done():
                exc = TimeoutError(f"not finished within map timeout of {timeout}s")
            results.append(None)
            failures.append((i, exc))
    if failures:
        raise MapFailed(failures, total=len(futures))
    return results


def map(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any]:
    """Run ``func(item)`` for each item as jobs; return results in input order.

    ``timeout`` (optional) is one overall deadline for the whole call. Other
    ``opts`` are split as in ``submit``. Every future is collected before
    returning; if any failed, raises ``MapFailed`` with the failing indices.
    """
    timeout = opts.pop("timeout", None)
    return _gather(submit_map(func, iterable, **opts), timeout)


def starmap(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any]:
    """Like ``map``, but calls ``func(*item)`` for each item."""
    timeout = opts.pop("timeout", None)
    return _gather(submit_starmap(func, iterable, **opts), timeout)
