"""Budgets: the Budget value, its verdict arithmetic, BudgetExceeded, and the Config field."""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import pytest

import sk8s
from sk8s.budget import Budget
from sk8s.config import Config, load_config
from sk8s.explaining import CPU_HOUR_USD


def test_budget_defaults_are_all_unlimited() -> None:
    b = Budget()
    assert b.max_concurrent_pods is None and b.max_cpu_hours is None and b.max_estimated_usd is None
    assert b.assumed_job_hours == 1.0


@pytest.mark.parametrize("kw", [
    {"max_concurrent_pods": 0}, {"max_concurrent_pods": True}, {"max_concurrent_pods": 1.5},
    {"max_cpu_hours": -1}, {"max_estimated_usd": -0.01}, {"assumed_job_hours": 0},
])
def test_budget_rejects_nonsense(kw: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        Budget(**kw)  # type: ignore[arg-type]


def test_coerce_accepts_budget_mapping_json_and_none() -> None:
    b = Budget(max_cpu_hours=2.0)
    assert Budget.coerce(b) is b
    assert Budget.coerce(None) is None
    assert Budget.coerce({"max_concurrent_pods": 3}) == Budget(max_concurrent_pods=3)
    assert Budget.coerce(json.dumps(b.to_dict())) == b
    with pytest.raises(ValueError, match="unknown budget field"):
        Budget.coerce({"max_pods": 3})
    with pytest.raises(ValueError, match="not valid JSON"):
        Budget.coerce("{nope")
    with pytest.raises(TypeError):
        Budget.coerce(5)


def test_to_dict_omits_unset_and_roundtrips() -> None:
    b = Budget(max_estimated_usd=1.5, max_concurrent_pods=4)
    assert b.to_dict() == {"max_concurrent_pods": 4, "max_estimated_usd": 1.5, "assumed_job_hours": 1.0}
    assert Budget.coerce(b.to_dict()) == b


def test_evaluate_within_budget() -> None:
    v = Budget(max_cpu_hours=10, max_estimated_usd=1.0).evaluate(n_jobs=4, cpu_cores=8.0)
    assert v.ok and v.violations == []
    assert v.estimated_cpu_hours == pytest.approx(8.0)  # 8 cores x 1h assumed
    assert v.estimated_usd == pytest.approx(8.0 * CPU_HOUR_USD)


def test_evaluate_cpu_hours_exceeded_names_the_numbers() -> None:
    v = Budget(max_cpu_hours=2.0, assumed_job_hours=0.5).evaluate(n_jobs=10, cpu_cores=10.0)
    assert not v.ok
    assert v.estimated_cpu_hours == pytest.approx(5.0)
    assert len(v.violations) == 1 and "max_cpu_hours" in v.violations[0]
    assert "5" in v.violations[0] and "2" in v.violations[0]


def test_evaluate_usd_exceeded() -> None:
    v = Budget(max_estimated_usd=0.001).evaluate(n_jobs=1, cpu_cores=1.0)
    assert not v.ok and "max_estimated_usd" in v.violations[0]


def test_exactly_at_the_limit_is_allowed() -> None:
    assert Budget(max_cpu_hours=4.0).evaluate(n_jobs=2, cpu_cores=4.0, ).estimated_cpu_hours == 4.0
    assert Budget(max_cpu_hours=4.0).evaluate(n_jobs=2, cpu_cores=4.0).ok


def test_concurrency_is_a_cap_never_a_violation() -> None:
    v = Budget(max_concurrent_pods=3).evaluate(n_jobs=100, cpu_cores=0.0, max_concurrent=None)
    assert v.ok and v.effective_max_concurrent == 3 and v.peak_pods == 3
    v = Budget(max_concurrent_pods=3).evaluate(n_jobs=100, cpu_cores=0.0, max_concurrent=10)
    assert v.effective_max_concurrent == 3 and v.peak_pods == 3
    v = Budget(max_concurrent_pods=3).evaluate(n_jobs=100, cpu_cores=0.0, max_concurrent=2)
    assert v.effective_max_concurrent == 2 and v.peak_pods == 2  # the caller's tighter value wins
    v = Budget(max_concurrent_pods=30).evaluate(n_jobs=2, cpu_cores=0.0)
    assert v.peak_pods == 2


def test_no_cpu_request_is_noted_when_cost_limits_are_set() -> None:
    v = Budget(max_estimated_usd=1.0).evaluate(n_jobs=1, cpu_cores=0.0)
    assert v.ok and any("requests" in n for n in v.notes)
    v = Budget(max_concurrent_pods=2).evaluate(n_jobs=1, cpu_cores=0.0)
    assert not v.notes


def test_cap_concurrency_helper() -> None:
    assert Budget(max_concurrent_pods=3).cap_concurrency(None) == 3
    assert Budget(max_concurrent_pods=3).cap_concurrency(8) == 3
    assert Budget(max_concurrent_pods=3).cap_concurrency(2) == 2
    assert Budget().cap_concurrency(None) is None
    assert Budget().cap_concurrency(5) == 5


def test_budget_exceeded_is_typed_exported_and_pickles() -> None:
    v = Budget(max_cpu_hours=1.0).evaluate(n_jobs=1, cpu_cores=4.0)
    with pytest.raises(sk8s.BudgetExceeded) as ei:
        v.raise_if_exceeded()
    assert isinstance(ei.value, sk8s.Sk8sError)
    assert ei.value.violations == v.violations
    assert ei.value.verdict["estimated_cpu_hours"] == pytest.approx(4.0)
    assert "max_cpu_hours" in str(ei.value)
    again = pickle.loads(pickle.dumps(ei.value))
    assert isinstance(again, sk8s.BudgetExceeded) and again.violations == v.violations
    assert str(again) == str(ei.value)
    assert "BudgetExceeded" in sk8s.__all__ and "Budget" in sk8s.__all__


def test_raise_if_exceeded_is_silent_when_ok() -> None:
    Budget(max_cpu_hours=10).evaluate(n_jobs=1, cpu_cores=1.0).raise_if_exceeded()


# --- Config field --------------------------------------------------------------------------------


def test_config_budget_defaults_none_and_coerces_mappings() -> None:
    assert Config().budget is None
    assert Config(budget={"max_cpu_hours": 3}).budget == Budget(max_cpu_hours=3)  # type: ignore[arg-type]


def test_config_budget_layers(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text("[budget]\nmax_cpu_hours = 2.5\nmax_concurrent_pods = 4\n")
    assert load_config(path=f, env={}).budget == Budget(max_cpu_hours=2.5, max_concurrent_pods=4)
    env = {"SK8S_BUDGET": json.dumps({"max_estimated_usd": 0.5})}
    assert load_config(path=f, env=env).budget == Budget(max_estimated_usd=0.5)
    kw = load_config(path=f, env=env, budget=Budget(max_cpu_hours=1.0))
    assert kw.budget == Budget(max_cpu_hours=1.0)


def test_config_budget_bad_env_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="SK8S_BUDGET"):
        load_config(path=Path("/nonexistent"), env={"SK8S_BUDGET": "{nope"})
