"""Module-level API over an implicit default cluster (requests-Session pattern)."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import threading
import time
import traceback
from typing import Any, Callable, Iterable, Literal, overload

import cloudpickle  # type: ignore[import-untyped]  # no py.typed marker upstream

from sk8s import expect as expectations
from sk8s.budget import Budget
from sk8s.dryrun import DryRun, resource_totals
from sk8s.errors import MapFailed, PilotFailed, RemoteException
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

    ``submit_workflow``/``attach`` build their own cluster from the stored overrides
    and deliberately ignore a cluster installed here (see ``sk8s.workflows``).
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


def _new_cluster(**extra: Any) -> Any:
    """A new, unshared Cluster built from the configure() overrides (one per workflow)."""
    from sk8s.cluster import Cluster
    with _lock:
        overrides = {**_config_overrides, **extra}
    return Cluster(**overrides)


def run(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` as a job and return its result.

    Keyword arguments named in ``sk8s.cluster.JOB_OPTIONS`` (e.g. ``image``,
    ``limits``, ``name_prefix``) set job options and are not passed to ``func``.
    ``timeout`` bounds the wait and is never passed to ``func``. Every other
    keyword argument is forwarded to ``func``.

    ``sk8s.Artifact`` arguments reach ``func`` as local ``Path`` s, a parameter annotated
    ``sk8s.OutDir`` gets an output directory, and returned OutDir paths come back as Artifacts;
    ``memo=True`` answers a repeat call from the recorded result without running anything
    (see docs/artifacts.md).
    """
    return _cluster().run(func, *args, **kwargs)


def _fan_budget(opts: dict[str, Any]) -> Budget | None:
    """The budget governing a fan-out: its own ``budget=`` option, else the default cluster's
    ``Config.budget``. A cluster object with no ``config`` (a stub, ``LocalCluster``) has none."""
    explicit = Budget.coerce(opts.get("budget"))
    if explicit is not None:
        return explicit
    budget = getattr(getattr(_cluster(), "config", None), "budget", None)
    return budget if isinstance(budget, Budget) else None


def _admit_fan_out(budget: Budget, n_items: int, opts: dict[str, Any]) -> None:
    """Raise ``BudgetExceeded`` if the WHOLE fan-out (all its jobs, ``pilot`` included) is over
    ``budget``. Pure arithmetic on the options; runs before the first job is submitted."""
    batch_size = opts.get("batch_size", 1)
    max_concurrent = opts.get("max_concurrent")
    _positive("batch_size", batch_size)
    _positive("max_concurrent", max_concurrent)
    n_jobs = -(-n_items // batch_size)
    cpu, _ = resource_totals(opts.get("requests"), opts.get("limits"))
    budget.evaluate(
        n_jobs=n_jobs, cpu_cores=cpu * n_jobs, max_concurrent=max_concurrent
    ).raise_if_exceeded()


def _dry_run_with(call: Callable[[Any], DryRun]) -> DryRun:
    """Run ``call(cluster)`` for a preview. Uses the default cluster when one already exists;
    otherwise a throwaway ``Cluster`` that never opens a watch stream and is closed after, so a
    preview from a cold start contacts nothing and leaves no thread or shared state behind."""
    with _lock:
        existing = _default_cluster
    if existing is not None:
        return call(existing)
    cluster = _new_cluster(watch=False)
    try:
        return call(cluster)
    finally:
        cluster.close(delete_workflow=False)


@overload
def submit(
    func: Callable[..., Any], /, *args: Any, dry_run: Literal[True], **kwargs: Any
) -> DryRun: ...


@overload
def submit(
    func: Callable[..., Any], /, *args: Any, dry_run: Literal[False] = ..., **kwargs: Any
) -> JobFuture: ...


def submit(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture | DryRun:
    """Submit ``func`` as a job and return a ``JobFuture`` without waiting.

    Keyword arguments are split as in ``run``: names in
    ``sk8s.cluster.JOB_OPTIONS`` become job options, all others go to ``func``.
    ``timeout`` is accepted and discarded here; pass it to ``future.result()``.

    ``dry_run=True`` builds everything (validation, mounts, image/store/namespace, the full
    serialized Job body) but creates nothing, and returns a :class:`sk8s.DryRun` instead.
    """
    if kwargs.get("dry_run"):
        return _dry_run_with(lambda c: c.dry_run(func, *args, **kwargs))
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
    expect: Callable[..., Any] | None = None,
) -> list[tuple[str, Any]]:
    """Run ``func`` over ``items`` inside one job.

    Returns one outcome per item, in order: ``("ok", value)`` or ``("err", exc)``.
    ``expect`` is checked per item (a batch is one job, but the expectation is about each
    item's result): a rejected item becomes ``("err", ExpectationFailed)``.
    Module-level so it serializes by reference rather than by value.
    """
    kw = kwargs or {}
    out: list[tuple[str, Any]] = []
    for item in items:
        try:
            value = func(*item, **kw) if star else func(item, **kw)
            verdict = None if expect is None else expectations.check(expect, value)
            out.append(("ok", value) if verdict is None else ("err", verdict.error))
        except BaseException as exc:  # noqa: BLE001
            out.append(_safe_outcome(exc))
    return out


def _positive(name: str, value: int | None) -> None:
    if name == "pilot" and (not isinstance(value, int) or isinstance(value, bool)):
        raise TypeError(f"pilot must be an integer item count, got {value!r}")
    if value is not None and value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


class _ItemFuture(JobFuture):
    """Per-item future of a batched job.

    It resolves from the batch job's result. ``cancel()`` cancels the whole
    batch job (the unit that actually runs), which in turn cancels every item.
    """

    _parent: JobFuture | None = None

    def cancel(self) -> bool:
        if self.done() or self._parent is None:
            return False
        return self._parent.cancel()


def _unsubmitted(fut: JobFuture | None = None) -> JobFuture:
    """A resolved future for an item that was never submitted (deadline hit)."""
    fut = fut if fut is not None else JobFuture("unsubmitted", "unsubmitted", _NullBackend())
    fut.set_exception(concurrent.futures.TimeoutError(
        "not submitted before the map timeout: no max_concurrent slot freed up in time"
    ))
    return fut


def _submit_many(
    func: Callable[..., Any],
    iterable: Iterable[Any],
    star: bool,
    *,
    deadline: float | None = None,
    timeout: float | None = None,
    **opts: Any,
) -> list[JobFuture]:
    """Shared body of the submit_map family.

    ``deadline`` (a ``time.monotonic()`` value, set by ``map``/``starmap``) bounds
    how long submission may block on ``max_concurrent``. Once it passes, no more
    jobs are submitted and every remaining item gets an already-failed future
    carrying ``concurrent.futures.TimeoutError``.
    """
    pilot = opts.pop("pilot", None)
    if pilot is not None:
        _positive("pilot", pilot)
        return _submit_piloted(func, iterable, star, pilot, deadline, timeout, opts)
    batch_size: int = opts.pop("batch_size", 1)
    max_concurrent: int | None = opts.pop("max_concurrent", None)
    _positive("batch_size", batch_size)
    _positive("max_concurrent", max_concurrent)
    budget = _fan_budget(opts)
    if budget is not None:  # a cap, not a failure: submission blocks until a slot frees up
        max_concurrent = budget.cap_concurrency(max_concurrent)
    items = list(iterable)
    sem = threading.BoundedSemaphore(max_concurrent) if max_concurrent else None
    expired = False

    def submit_job(fn: Callable[..., Any], *args: Any, **kw: Any) -> JobFuture | None:
        # Waits while ``max_concurrent`` jobs are unresolved; the slot is freed
        # when the job's future completes (result, error or cancel). Returns None
        # if ``deadline`` passes first.
        nonlocal expired
        if expired:
            return None
        if sem is not None:
            if deadline is None:
                sem.acquire()
            elif not sem.acquire(timeout=max(0.0, deadline - time.monotonic())):
                expired = True
                return None
        try:
            fut: JobFuture = _cluster().submit(fn, *args, **kw)
        except BaseException:
            if sem is not None:
                sem.release()
            raise
        if sem is not None:
            fut.add_done_callback(lambda _: sem.release())
        return fut

    if batch_size == 1:
        out: list[JobFuture] = []
        for item in items:
            fut = submit_job(func, *item, **opts) if star else submit_job(func, item, **opts)
            out.append(fut if fut is not None else _unsubmitted())
        return out

    # Batched: one job per chunk; per-item futures are resolved from the chunk's
    # result. Names in JOB_OPTIONS configure the job; every other keyword goes to
    # ``func`` (inside the batch), matching the unbatched split.
    from sk8s.cluster import JOB_OPTIONS

    job_opts = {k: v for k, v in opts.items() if k in JOB_OPTIONS}
    # The expectation is per item, so it rides inside the batch rather than on the job.
    expect = job_opts.pop("expect", None)
    if expect is not None:
        expectations.require_callable(expect)
    # Only passed when set: a batch job without a gate keeps the exact v2 call shape
    # (func, chunk, star, kwargs), so it still runs on a worker image that predates expect=.
    # Positional, not a keyword: ``expect=`` to submit() is the JOB option and would be
    # consumed by the cluster as the gate of the whole batch job.
    extra = (expect,) if expect is not None else ()
    func_kwargs = {k: v for k, v in opts.items() if k not in JOB_OPTIONS}
    item_futures: list[JobFuture] = []
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        parent = submit_job(_run_batch, func, chunk, star, func_kwargs, *extra, **job_opts)
        if parent is None:
            item_futures.extend(_unsubmitted() for _ in chunk)
            continue
        group = [_ItemFuture(parent.job_name, parent.workflow_id, parent._backend)  # noqa: SLF001
                 for _ in chunk]
        for fut in group:
            fut._parent = parent  # noqa: SLF001
        item_futures.extend(group)
        parent.add_done_callback(_make_unpack(group))
    return item_futures


def _submit_piloted(
    func: Callable[..., Any],
    iterable: Iterable[Any],
    star: bool,
    pilot: int,
    deadline: float | None,
    timeout: float | None,
    opts: dict[str, Any],
) -> list[JobFuture]:
    """``pilot=n``: submit the first ``n`` items, wait for ALL of them (results and any
    ``expect=`` checks), and submit the remainder only if every one is green.

    The pilot is an ordinary fan-out (``batch_size``/``max_concurrent`` apply to it), so a
    pilot failure leaves exactly the pilot's jobs behind and nothing else. The pilot wait
    shares ``deadline`` with the rest of the call; ``submit_map`` has no deadline, so there
    ``timeout=`` (otherwise discarded) bounds the pilot wait only.
    """
    items = list(iterable)
    head, tail = items[:pilot], items[pilot:]
    wait_deadline = deadline if deadline is not None else _deadline(timeout)
    pilot_futures = _submit_many(func, head, star, deadline=deadline, timeout=timeout, **opts)
    _, failures, succeeded = _collect(pilot_futures, timeout, wait_deadline)
    if failures:
        raise PilotFailed(failures, pilot=len(head), total=len(items), results=succeeded)
    if not tail:
        return pilot_futures
    return pilot_futures + _submit_many(
        func, tail, star, deadline=deadline, timeout=timeout, **opts
    )


def _fan_out(
    func: Callable[..., Any],
    iterable: Iterable[Any],
    star: bool,
    *,
    deadline: float | None = None,
    timeout: float | None = None,
    **opts: Any,
) -> list[JobFuture] | DryRun:
    """Entry of the submit_map family: ``dry_run`` is popped and answered FIRST, before the
    ``pilot`` branch of ``_submit_many`` could submit and wait on anything for real.

    ``memo=True`` with ``batch_size > 1`` is refused HERE, before either path, so a preview
    reports the same error a real run would (and a ``pilot`` head never submits first)."""
    batch = opts.get("batch_size", 1)
    if opts.get("memo") and isinstance(batch, int) and batch > 1:
        raise ValueError(
            "memo=True is not supported with batch_size > 1: a batch is one job whose result "
            "mixes per-item successes and failures, and failures must never be recorded. "
            "Use batch_size=1 to memoize each item."
        )
    if opts.pop("dry_run", False):
        return _dry_run_many(func, list(iterable), star, opts)
    items = list(iterable)
    budget = _fan_budget(opts)
    if budget is not None:
        _admit_fan_out(budget, len(items), opts)  # before the first job (or pilot) exists
    return _submit_many(func, items, star, deadline=deadline, timeout=timeout, **opts)


def _dry_run_many(
    func: Callable[..., Any], items: list[Any], star: bool, opts: dict[str, Any],
) -> DryRun:
    """Preview a fan-out: ONE representative job body plus counts and totals for the rest.

    Mirrors ``_submit_many``'s job shapes (unbatched: ``func(*item)``; batched: ``_run_batch``
    with ``expect`` riding as the conditional fifth positional) so ``payload_bytes`` is honest.
    ``pilot`` is validated and noted but changes nothing: the whole fan-out is previewed.
    """
    from sk8s.cluster import JOB_OPTIONS

    pilot = opts.pop("pilot", None)
    if pilot is not None:
        _positive("pilot", pilot)
    batch_size: int = opts.pop("batch_size", 1)
    max_concurrent: int | None = opts.pop("max_concurrent", None)
    _positive("batch_size", batch_size)
    _positive("max_concurrent", max_concurrent)
    if batch_size == 1:
        args: tuple[Any, ...] = ()
        if items:
            args = tuple(items[0]) if star else (items[0],)
        one = _dry_run_with(lambda c: c.dry_run(func, *args, **opts))
    else:
        job_opts = {k: v for k, v in opts.items() if k in JOB_OPTIONS}
        expect = job_opts.pop("expect", None)
        if expect is not None:
            expectations.require_callable(expect)
        extra = (expect,) if expect is not None else ()
        func_kwargs = {k: v for k, v in opts.items() if k not in JOB_OPTIONS}
        chunk = items[:batch_size]
        one = _dry_run_with(
            lambda c: c.dry_run(_run_batch, func, chunk, star, func_kwargs, *extra, **job_opts)
        )
    n_jobs = -(-len(items) // batch_size)  # ceil
    notes = list(one.notes)
    if batch_size > 1:
        notes.append(
            f"batch_size={batch_size}: {len(items)} item(s) are packed into {n_jobs} job(s) of up "
            "to that many items each"
        )
    if n_jobs > 1:
        notes.append(
            f"only the first job's body is shown; the other {n_jobs - 1} differ in name, payload "
            "and result keys"
        )
    if max_concurrent:
        notes.append(f"max_concurrent={max_concurrent}: at most that many jobs are unresolved at once")
    if pilot is not None:
        notes.append(
            f"pilot={pilot}: a real run submits the first {min(pilot, len(items))} item(s) and "
            "waits for all of them to pass (results and expect=) before submitting the rest; "
            "nothing was submitted here"
        )
    verdict = one.budget
    if verdict is not None:  # the single-job verdict becomes the whole fan-out's
        notes = [n for n in notes if not n.startswith("BUDGET EXCEEDED")]
        verdict = Budget(**verdict.budget).evaluate(
            n_jobs=n_jobs, cpu_cores=one.total_cpu_request * n_jobs, max_concurrent=max_concurrent
        )
        if not verdict.ok:
            notes.append("BUDGET EXCEEDED (a real submission would raise BudgetExceeded): "
                         + "; ".join(verdict.violations))
    return dataclasses.replace(
        one,
        jobs=one.jobs if n_jobs else [],
        n_jobs=n_jobs,
        total_cpu_request=one.total_cpu_request * n_jobs,
        total_memory_request=one.total_memory_request * n_jobs,
        n_items=len(items),
        notes=notes,
        budget=verdict,
    )


def _make_unpack(group: list[_ItemFuture]) -> Callable[[concurrent.futures.Future[Any]], None]:
    def settle(fut: JobFuture, *, result: Any = None, exc: BaseException | None = None) -> None:
        try:
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(result)
        except concurrent.futures.InvalidStateError:
            pass  # already terminal; keep that state

    def cancel_item(fut: JobFuture) -> None:
        # Bypass _ItemFuture.cancel (which delegates to the parent) and mark
        # the item itself cancelled, waking any waiters.
        if concurrent.futures.Future.cancel(fut):
            fut.set_running_or_notify_cancel()

    def unpack(parent: concurrent.futures.Future[Any]) -> None:
        if parent.cancelled():
            for fut in group:
                cancel_item(fut)
            return
        try:
            outcomes = parent.result()
        except BaseException as exc:  # noqa: BLE001 — whole batch failed
            for fut in group:
                settle(fut, exc=exc)
            return
        if len(outcomes) != len(group):
            bad = RuntimeError(
                f"batch job returned {len(outcomes)} outcomes for {len(group)} items"
            )
            for fut in group:
                settle(fut, exc=bad)
            return
        for fut, (kind, value) in zip(group, outcomes):
            if kind == "ok":
                settle(fut, result=value)
            else:
                settle(fut, exc=value)

    return unpack


class _NullBackend:
    """Backend for futures that never had a job (nothing to log or cancel)."""

    def logs(self, job_name: str) -> str:
        return ""

    def cancel(self, job_name: str) -> bool:
        return False


@overload
def submit_map(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[True], **opts: Any
) -> DryRun: ...


@overload
def submit_map(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[False] = ..., **opts: Any
) -> list[JobFuture]: ...


def submit_map(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture] | DryRun:
    """Submit ``func(item)`` for each item; return the futures in input order.

    ``opts`` are split as in ``submit``: job-option names configure the jobs,
    all other keyword arguments are forwarded to ``func``. Two fan-out controls
    are consumed here and never reach ``func``:

    - ``batch_size`` (default 1): pack up to N items into each job. Each item
      still gets its own future, resolved from its job's result; an exception
      raised by one item fails only that item, while a failure of the whole job
      fails every item in it. Cancelling an item future cancels its whole job, so
      every item in that job ends up cancelled.
    - ``max_concurrent`` (default None): at most this many jobs may be unresolved
      at once. Submission blocks the caller until a slot frees up, with no
      time limit (``map``/``starmap`` bound this wait by their ``timeout``). An active
      ``Budget.max_concurrent_pods`` tightens it (the min of the two applies).
    - ``pilot`` (default None): submit only the first N items and WAIT for all N
      (results and any ``expect=`` checks). All green: the remainder is submitted and
      the full list of futures returned. Any failure: raises ``PilotFailed`` and the
      remainder is never submitted. Because of this, ``submit_map(pilot=...)`` BLOCKS
      until the pilot finishes, and the futures for the non-pilot items exist only after
      the pilot passes. ``timeout=`` (discarded without ``pilot``) bounds this wait.

    ``expect=`` (a job option) is applied to every item's result. ``expect`` and ``pilot``
    are reserved names: they are never forwarded to ``func``.

    ``dry_run=True`` submits nothing and returns a :class:`sk8s.DryRun`: one representative
    Job body, ``n_jobs`` (batches, with ``batch_size``) and request totals; 10k bodies are
    never serialized.
    """
    return _fan_out(func, iterable, False, **opts)


@overload
def submit_starmap(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[True], **opts: Any
) -> DryRun: ...


@overload
def submit_starmap(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[False] = ..., **opts: Any
) -> list[JobFuture]: ...


def submit_starmap(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture] | DryRun:
    """Submit ``func(*item)`` for each item; return the futures in input order.

    ``opts`` are split as in ``submit``; ``batch_size``, ``max_concurrent``, ``pilot`` and
    ``dry_run`` behave as in ``submit_map``.
    """
    return _fan_out(func, iterable, True, **opts)


def _deadline(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + timeout


def _collect(
    futures: list[JobFuture], timeout: float | None, deadline: float | None = None
) -> tuple[list[Any], list[tuple[int, BaseException]], dict[int, Any]]:
    """Collect results in input order, waiting at most ``timeout`` in total.

    ``deadline`` (a ``time.monotonic()`` value) is the overall cutoff; it
    defaults to ``timeout`` from now. ``map`` passes the one it started
    submission with, so submission and collection share a single budget.

    Returns ``(results, failures, succeeded)`` (``succeeded`` maps index -> value for the
    items that completed; ``results`` holds ``None`` for failed ones). Every future is visited even after a failure.
    Items still pending when the deadline passes are recorded as
    ``concurrent.futures.TimeoutError`` failures; items already finished keep
    their results. ``KeyboardInterrupt`` and other non-``Exception`` signals
    propagate immediately.
    """
    if deadline is None:
        deadline = _deadline(timeout)
    results: list[Any] = []
    failures: list[tuple[int, BaseException]] = []
    succeeded: dict[int, Any] = {}
    for i, fut in enumerate(futures):
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            value = fut.result(timeout=remaining)
            results.append(value)
            succeeded[i] = value
        except Exception as exc:
            if (
                deadline is not None
                and not fut.done()
                and isinstance(exc, concurrent.futures.TimeoutError)
            ):
                exc = concurrent.futures.TimeoutError(f"not finished within map timeout of {timeout}s")
            results.append(None)
            failures.append((i, exc))
    return results, failures, succeeded


def _gather(
    futures: list[JobFuture], timeout: float | None, deadline: float | None = None
) -> list[Any]:
    """``_collect`` that raises ``MapFailed`` if any item failed."""
    results, failures, succeeded = _collect(futures, timeout, deadline)
    if failures:
        raise MapFailed(failures, total=len(futures), results=succeeded)
    return results


@overload
def map(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[True], **opts: Any
) -> DryRun: ...


@overload
def map(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[False] = ..., **opts: Any
) -> list[Any]: ...


def map(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any] | DryRun:
    """Run ``func(item)`` for each item as jobs; return results in input order.

    ``dry_run=True`` runs nothing and returns a :class:`sk8s.DryRun` (see ``submit_map``).

    ``timeout`` (optional) is one overall deadline for the whole call, covering
    submission (including waiting on ``max_concurrent``) and collection. Other
    ``opts`` are split as in ``submit``. Every future is collected before
    returning; if any failed, raises ``MapFailed`` with the failing indices in
    ``.failures`` and every success in ``.results`` (``{index: value}``); its message
    counts expectation failures (from ``expect=``) separately from errors.

    ``pilot=n`` runs the first n items first and submits the rest only if all n pass
    (raising ``PilotFailed`` otherwise); the pilot shares the ``timeout`` budget.
    """
    timeout = opts.pop("timeout", None)
    deadline = _deadline(timeout)
    futures = _fan_out(func, iterable, False, deadline=deadline, timeout=timeout, **opts)
    if isinstance(futures, DryRun):
        return futures
    return _gather(futures, timeout, deadline)


@overload
def starmap(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[True], **opts: Any
) -> DryRun: ...


@overload
def starmap(
    func: Callable[..., Any], iterable: Iterable[Any], *, dry_run: Literal[False] = ..., **opts: Any
) -> list[Any]: ...


def starmap(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any] | DryRun:
    """Like ``map``, but calls ``func(*item)`` for each item."""
    timeout = opts.pop("timeout", None)
    deadline = _deadline(timeout)
    futures = _fan_out(func, iterable, True, deadline=deadline, timeout=timeout, **opts)
    if isinstance(futures, DryRun):
        return futures
    return _gather(futures, timeout, deadline)
