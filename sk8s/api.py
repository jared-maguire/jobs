"""Module-level API over an implicit default cluster (requests-Session pattern)."""
from __future__ import annotations

import builtins
import threading
from typing import Any, Callable, Iterable

from sk8s.errors import MapFailed
from sk8s.future import JobFuture

_lock = threading.Lock()
_default_cluster: Any | None = None
_config_overrides: dict[str, Any] = {}


def configure(cluster: Any | None = None, **config_overrides: Any) -> None:
    """Set the default cluster, or configure the one created on first use."""
    global _default_cluster, _config_overrides
    with _lock:
        _default_cluster = cluster
        if config_overrides:
            _config_overrides = config_overrides


def _cluster() -> Any:
    global _default_cluster
    with _lock:
        if _default_cluster is None:
            from sk8s.cluster import Cluster
            _default_cluster = Cluster(**_config_overrides)
        return _default_cluster


def run(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return _cluster().run(func, *args, **kwargs)


def submit(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
    fut: JobFuture = _cluster().submit(func, *args, **kwargs)
    return fut


def submit_map(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    return [submit(func, item, **opts) for item in iterable]


def submit_starmap(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    return [submit(func, *item, **opts) for item in iterable]


def _gather(futures: list[JobFuture], timeout: float | None) -> list[Any]:
    results: list[Any] = []
    failures: list[tuple[int, BaseException]] = []
    for i, fut in enumerate(futures):
        try:
            results.append(fut.result(timeout=timeout))
        except BaseException as exc:  # noqa: BLE001 — summarized in MapFailed
            results.append(None)
            failures.append((i, exc))
    if failures:
        raise MapFailed(failures, total=len(futures))
    return results


def map(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any]:
    timeout = opts.pop("timeout", None)
    return _gather(submit_map(func, iterable, **opts), timeout)


def starmap(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any]:
    timeout = opts.pop("timeout", None)
    return _gather(submit_starmap(func, iterable, **opts), timeout)
