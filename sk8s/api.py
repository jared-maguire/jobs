"""Module-level API over an implicit default cluster (requests-Session pattern)."""
from __future__ import annotations

import concurrent.futures
import threading
import time
import traceback
from typing import Any, Callable, Iterable

import cloudpickle  # type: ignore[import-untyped]  # no py.typed marker upstream

from sk8s.errors import MapFailed, RemoteException
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


def _safe_outcome(exc: BaseException) -> tuple[str, BaseException]:
    """Wrap ``exc`` as an ``("err", exc)`` outcome that survives pickling.

    An exception that cannot be pickled would sink the whole batch result, so it
    degrades to a ``RemoteException`` carrying its type name, message and traceback.
    """
    try:
        cloudpickle.loads(cloudpickle.dumps(exc))
        return ("err", exc)
    except Exception:  # noqa: BLE001
        return ("err", RemoteException(
            f"{type(exc).__module__}.{type(exc).__qualname__}",
            str(exc),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ))


def _run_batch(
    func: Callable[..., Any],
    items: list[Any],
    star: bool,
    kwargs: dict[str, Any] | None = None,
) -> list[tuple[str, Any]]:
    """Run ``func`` over ``items`` inside one job.

    Returns one outcome per item, in order: ``("ok", value)`` or ``("err", exc)``.
    Module-level so it serializes by reference rather than by value.
    """
    kw = kwargs or {}
    out: list[tuple[str, Any]] = []
    for item in items:
        try:
            out.append(("ok", func(*item, **kw) if star else func(item, **kw)))
        except BaseException as exc:  # noqa: BLE001
            out.append(_safe_outcome(exc))
    return out


def _positive(name: str, value: int | None) -> None:
    if value is not None and value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _submit_many(
    func: Callable[..., Any], iterable: Iterable[Any], star: bool, **opts: Any
) -> list[JobFuture]:
    batch_size: int = opts.pop("batch_size", 1)
    max_concurrent: int | None = opts.pop("max_concurrent", None)
    _positive("batch_size", batch_size)
    _positive("max_concurrent", max_concurrent)
    items = list(iterable)
    sem = threading.BoundedSemaphore(max_concurrent) if max_concurrent else None

    def submit_job(fn: Callable[..., Any], *args: Any, **kw: Any) -> JobFuture:
        # Blocks while ``max_concurrent`` jobs are unresolved; the slot is freed
        # when the job's future completes (result, error or cancel).
        if sem is not None:
            sem.acquire()
        try:
            fut = submit(fn, *args, **kw)
        except BaseException:
            if sem is not None:
                sem.release()
            raise
        if sem is not None:
            fut.add_done_callback(lambda _: sem.release())
        return fut

    if batch_size == 1:
        if star:
            return [submit_job(func, *item, **opts) for item in items]
        return [submit_job(func, item, **opts) for item in items]

    # Batched: one job per chunk; per-item futures are resolved from the chunk's
    # result. Names in JOB_OPTIONS configure the job; every other keyword goes to
    # ``func`` (inside the batch), matching the unbatched split.
    from sk8s.cluster import JOB_OPTIONS

    job_opts = {k: v for k, v in opts.items() if k in JOB_OPTIONS}
    func_kwargs = {k: v for k, v in opts.items() if k not in JOB_OPTIONS}
    item_futures: list[JobFuture] = [
        JobFuture("batched", "batched", backend=_NullBackend()) for _ in items
    ]
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        parent = submit_job(_run_batch, func, chunk, star, func_kwargs, **job_opts)
        group = item_futures[start:start + len(chunk)]
        for fut in group:
            fut.job_name = parent.job_name
            fut.workflow_id = parent.workflow_id
            fut._backend = parent._backend  # noqa: SLF001
        parent.add_done_callback(_make_unpack(group))
    return item_futures


def _make_unpack(group: list[JobFuture]) -> Callable[[concurrent.futures.Future[Any]], None]:
    def settle(fut: JobFuture, *, result: Any = None, exc: BaseException | None = None) -> None:
        try:
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(result)
        except concurrent.futures.InvalidStateError:
            pass  # already terminal (e.g. cancelled by the caller); keep that state

    def unpack(parent: concurrent.futures.Future[Any]) -> None:
        try:
            outcomes = parent.result()
        except BaseException as exc:  # noqa: BLE001 — whole batch failed
            for fut in group:
                settle(fut, exc=exc)
            return
        for fut, (kind, value) in zip(group, outcomes):
            if kind == "ok":
                settle(fut, result=value)
            else:
                settle(fut, exc=value)

    return unpack


class _NullBackend:
    """Placeholder backend for item futures until the parent job exists."""

    def logs(self, job_name: str) -> str:
        return ""

    def cancel(self, job_name: str) -> bool:
        return False


def submit_map(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    """Submit ``func(item)`` for each item; return the futures in input order.

    ``opts`` are split as in ``submit``: job-option names configure the jobs,
    all other keyword arguments are forwarded to ``func``. Two fan-out controls
    are consumed here and never reach ``func``:

    - ``batch_size`` (default 1): pack up to N items into each job. Each item
      still gets its own future, resolved from its job's result; an exception
      raised by one item fails only that item, while a failure of the whole job
      fails every item in it. Cancelling an item future cancels its whole job.
    - ``max_concurrent`` (default None): at most this many jobs may be unresolved
      at once. Submission blocks the caller until a slot frees up.
    """
    return _submit_many(func, iterable, star=False, **opts)


def submit_starmap(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    """Submit ``func(*item)`` for each item; return the futures in input order.

    ``opts`` are split as in ``submit``; ``batch_size`` and ``max_concurrent``
    behave as in ``submit_map``.
    """
    return _submit_many(func, iterable, star=True, **opts)


def _gather(futures: list[JobFuture], timeout: float | None) -> list[Any]:
    """Collect results in input order, waiting at most ``timeout`` in total.

    Every future is visited even after a failure. Items still pending when the
    overall deadline passes are recorded as ``concurrent.futures.TimeoutError`` failures; items
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
            if (
                deadline is not None
                and not fut.done()
                and isinstance(exc, concurrent.futures.TimeoutError)
            ):
                exc = concurrent.futures.TimeoutError(f"not finished within map timeout of {timeout}s")
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
