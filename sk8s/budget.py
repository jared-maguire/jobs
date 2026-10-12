"""Budgets: client-side guardrails on what a submission may ask for.

A :class:`Budget` is checked at ADMISSION against the submission's own preview (the same totals
``dry_run=True`` reports), before anything is created, and caps fan-out concurrency while it runs.
It is estimate-based admission, not metering: nothing here observes what pods actually consume
or accumulates spend across submissions (that needs the event log of the durable interpreter).

``cpu_hours`` and ``usd`` are estimates: ``cpu request x n_jobs x assumed_job_hours`` and that
times the documented ``sk8s.explaining.CPU_HOUR_USD`` rate. sk8s cannot know how long a job will
run, so ``assumed_job_hours`` (default 1h) is the budget's own statement of it.
"""
from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from sk8s.errors import BudgetExceeded

_FIELDS = (
    "max_concurrent_pods", "max_cpu_hours", "max_estimated_usd", "assumed_job_hours",
    "assumed_cpu_when_unset",
)


def _non_negative(name: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"budget {name} must be a number, got {value!r}")
    if math.isnan(value) or value < 0:
        raise ValueError(f"budget {name} must be >= 0, got {value!r}")


UNCOSTED_NOTE = (
    "no cpu request or limit on these jobs, so their cost estimates to 0 and the cpu-hour / usd "
    "limits cannot see them: set requests= (or limits=), or set the budget's "
    "assumed_cpu_when_unset to charge them a fixed number of cores"
)


@dataclass(frozen=True)
class BudgetVerdict:
    """A budget measured against one submission. ``ok`` is False iff ``violations`` is non-empty.

    ``estimated_*`` are estimates (see the module docstring). ``effective_max_concurrent`` is the
    concurrency cap that applies (the tighter of the caller's ``max_concurrent`` and the budget's
    ``max_concurrent_pods``; None when neither is set) and ``peak_pods`` the most pods the
    submission can have at once under it. ``budget`` is the limits that were applied (a dict).
    """

    ok: bool
    budget: dict[str, Any]
    n_jobs: int
    peak_pods: int
    estimated_cpu_hours: float
    estimated_usd: float
    effective_max_concurrent: int | None = None
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Jobs that request no cpu at all while a cost limit is set and no assumed_cpu_when_unset
    # charges them: the jobs the cost limits cannot see. 0 when nothing slips through.
    uncosted_jobs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def raise_if_exceeded(self, hint: str | None = None) -> None:
        if not self.ok:
            raise BudgetExceeded(self.violations, self.to_dict(), hint=hint)


@dataclass(frozen=True)
class Budget:
    """Limits for a submission; every field is optional (None = unlimited).

    - ``max_concurrent_pods``: a CAP, not an admission test. ``map``/``starmap``/``submit_map``
      take ``min(max_concurrent, max_concurrent_pods)`` per call, so submission blocks rather
      than fails. It is enforced per fan-out call, not across separate calls or plain
      ``submit()`` loops.
    - ``max_cpu_hours`` / ``max_estimated_usd``: admission tests on the estimate; exceeding
      them raises :class:`sk8s.BudgetExceeded` before anything is created.
    - ``assumed_job_hours``: how long each job is assumed to run for the estimate (default 1.0).
    - ``assumed_cpu_when_unset``: a job that requests no cpu (``requests=``/``limits=``) is
      charged this many cores. The default None keeps the cost limits BLIND to such jobs: they
      estimate to 0 and pass any ``max_cpu_hours`` / ``max_estimated_usd`` (admission logs a
      warning once per cluster, and the verdict carries a note). Set it (e.g. 1) wherever the
      cost limits are meant to be a real guard, as in a sandbox.
    """

    max_concurrent_pods: int | None = None
    max_cpu_hours: float | None = None
    max_estimated_usd: float | None = None
    assumed_job_hours: float = 1.0
    assumed_cpu_when_unset: float | None = None

    def __post_init__(self) -> None:
        pods = self.max_concurrent_pods
        if pods is not None:
            if isinstance(pods, bool) or not isinstance(pods, int):
                raise TypeError(f"budget max_concurrent_pods must be an integer, got {pods!r}")
            if pods < 1:
                raise ValueError(f"budget max_concurrent_pods must be >= 1, got {pods!r}")
        _non_negative("max_cpu_hours", self.max_cpu_hours)
        _non_negative("max_estimated_usd", self.max_estimated_usd)
        _non_negative("assumed_job_hours", self.assumed_job_hours)
        _non_negative("assumed_cpu_when_unset", self.assumed_cpu_when_unset)
        for name in ("assumed_job_hours", "assumed_cpu_when_unset"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"budget {name} must be finite, got {value!r}")
        if self.assumed_job_hours <= 0:
            raise ValueError(f"budget assumed_job_hours must be > 0, got {self.assumed_job_hours!r}")
        if self.assumed_cpu_when_unset is not None and self.assumed_cpu_when_unset <= 0:
            raise ValueError(
                f"budget assumed_cpu_when_unset must be > 0, got {self.assumed_cpu_when_unset!r}"
            )

    @classmethod
    def coerce(cls, value: Any) -> Budget | None:
        """A ``Budget`` from a Budget, a mapping, a JSON object string, or None (-> None)."""
        if value is None or isinstance(value, Budget):
            return value
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValueError(f"budget is not valid JSON: {e}") from None
        if not isinstance(value, Mapping):
            raise TypeError(f"budget must be a Budget, a mapping or None, not {type(value).__name__}")
        unknown = sorted(set(value) - set(_FIELDS))
        if unknown:
            raise ValueError(f"unknown budget field(s) {unknown}; known: {list(_FIELDS)}")
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        """The set fields only (``assumed_job_hours`` always), as JSON/TOML-friendly scalars."""
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def cap_concurrency(self, max_concurrent: int | None) -> int | None:
        """The caller's ``max_concurrent`` tightened by ``max_concurrent_pods`` (the min)."""
        cap = self.max_concurrent_pods
        if cap is None:
            return max_concurrent
        return cap if max_concurrent is None else min(max_concurrent, cap)

    def evaluate(
        self, *, n_jobs: int, cpu_cores: float, max_concurrent: int | None = None,
        unrequested_jobs: int = 0,
    ) -> BudgetVerdict:
        """Measure a submission of ``n_jobs`` jobs requesting ``cpu_cores`` cores IN TOTAL
        (summed over all jobs, as ``DryRun.total_cpu_request``) against this budget.

        ``unrequested_jobs`` of those jobs request no cpu at all: each is charged
        ``assumed_cpu_when_unset`` cores on top (if that is set), else it goes uncosted (see the
        verdict's ``uncosted_jobs``).
        """
        from sk8s.explaining import CPU_HOUR_USD  # lazy: explaining imports the cluster

        assumed = self.assumed_cpu_when_unset
        charged = unrequested_jobs if assumed is not None else 0
        if assumed is not None:
            cpu_cores = cpu_cores + unrequested_jobs * assumed
        cpu_hours = cpu_cores * self.assumed_job_hours
        usd = cpu_hours * CPU_HOUR_USD
        effective = self.cap_concurrency(max_concurrent)
        peak = n_jobs if effective is None else min(n_jobs, effective)
        violations: list[str] = []
        if self.max_cpu_hours is not None and cpu_hours > self.max_cpu_hours:
            violations.append(
                f"estimated {cpu_hours:.4g} cpu-hours ({cpu_cores:.4g} cores x "
                f"{self.assumed_job_hours:g}h assumed) exceeds max_cpu_hours={self.max_cpu_hours:g}"
            )
        if self.max_estimated_usd is not None and usd > self.max_estimated_usd:
            violations.append(
                f"estimated ${usd:.4g} ({cpu_hours:.4g} cpu-hours at ${CPU_HOUR_USD:g}/cpu-hour) "
                f"exceeds max_estimated_usd={self.max_estimated_usd:g}"
            )
        notes: list[str] = []
        cost_limited = self.max_cpu_hours is not None or self.max_estimated_usd is not None
        uncosted = unrequested_jobs if (cost_limited and assumed is None) else 0
        if uncosted:
            notes.append(UNCOSTED_NOTE)
        if cost_limited and charged:
            notes.append(
                f"{charged} job(s) request no cpu; each is charged assumed_cpu_when_unset="
                f"{assumed:g} cores"
            )
        if effective is not None and effective < (n_jobs if max_concurrent is None else max_concurrent):
            notes.append(
                f"concurrency capped at {effective} pods by the budget's max_concurrent_pods="
                f"{self.max_concurrent_pods}"
            )
        return BudgetVerdict(
            ok=not violations, budget=self.to_dict(), n_jobs=n_jobs, peak_pods=peak,
            estimated_cpu_hours=cpu_hours, estimated_usd=usd, effective_max_concurrent=effective,
            violations=violations, notes=notes, uncosted_jobs=uncosted,
        )
