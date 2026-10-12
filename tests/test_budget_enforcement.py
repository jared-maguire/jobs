"""Budget enforcement: admission at submit/dry_run, the concurrency cap, workflow propagation."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import sk8s
from sk8s import workflows
from sk8s.budget import Budget
from sk8s.config import Config, load_config
from tests.test_cluster import Harness, _close_clusters  # noqa: F401  (autouse fixture)
from tests.test_dry_run import env, nothing_written, strict_cluster
from tests.test_fanout import StubCluster, _wait_for


def double(x: int) -> int:
    return x * 2


@pytest.fixture(autouse=True)
def reset_api() -> Any:
    sk8s.configure()
    yield
    sk8s.configure()


def untouched(h: Harness) -> bool:
    return not h.batch.jobs and not h.core.configmaps and not any(h.store.root.rglob("*"))


# --- Cluster.submit / dry_run ---------------------------------------------------------------------------


def test_dry_run_reports_the_verdict_instead_of_raising(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path, budget=Budget(max_cpu_hours=1.0))
    dr = c.submit(double, 1, dry_run=True, requests={"cpu": "4"})
    assert dr.budget is not None and not dr.budget.ok
    assert dr.budget.estimated_cpu_hours == pytest.approx(4.0)
    assert "max_cpu_hours" in dr.budget.violations[0]
    assert any("BUDGET EXCEEDED" in n for n in dr.notes)
    assert dr.to_dict()["budget"]["ok"] is False
    assert nothing_written(tmp_path)


def test_dry_run_without_a_budget_has_no_verdict(tmp_path: Path) -> None:
    assert strict_cluster(tmp_path).submit(double, 1, dry_run=True).budget is None


def test_dry_run_within_budget_is_ok(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path, budget=Budget(max_cpu_hours=8.0))
    dr = c.submit(double, 1, dry_run=True, requests={"cpu": "2"})
    assert dr.budget is not None and dr.budget.ok
    assert not any("BUDGET" in n for n in dr.notes)


def test_submit_over_budget_raises_before_creating_anything(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.cluster.config = Config(namespace="ns1", image="sk8s:dev", budget=Budget(max_estimated_usd=0.001))
    with pytest.raises(sk8s.BudgetExceeded) as ei:
        h.cluster.submit(double, 1, requests={"cpu": "2"})
    assert "max_estimated_usd" in str(ei.value)
    assert untouched(h)


def test_submit_within_budget_runs_and_carries_the_budget_to_the_pod(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    b = Budget(max_cpu_hours=5.0, max_concurrent_pods=3)
    h.cluster.config = Config(namespace="ns1", image="sk8s:dev", budget=b)
    fut = h.cluster.submit(double, 21, requests={"cpu": "1"})
    h.finish_job(fut.job_name)
    assert fut.result(timeout=10) == 42
    job = h.batch.jobs[("ns1", fut.job_name)]
    pod_env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    # a nested Cluster in the pod builds its Config from this env: same budget, same enforcement
    assert load_config(path=Path("/nonexistent"), env=pod_env).budget == b


def test_no_budget_means_no_budget_env(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    fut = h.cluster.submit(double, 1)
    job = h.batch.jobs[("ns1", fut.job_name)]
    assert "SK8S_BUDGET" not in {e.name for e in job.spec.template.spec.containers[0].env}


def test_per_submission_budget_option_overrides_config_and_is_not_a_func_kwarg(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.cluster.config = Config(namespace="ns1", image="sk8s:dev", budget=Budget(max_cpu_hours=100))
    with pytest.raises(sk8s.BudgetExceeded):
        h.cluster.submit(double, 1, requests={"cpu": "2"}, budget=Budget(max_cpu_hours=1))
    assert untouched(h)
    fut = h.cluster.submit(lambda x, **kw: (x, kw), 1, requests={"cpu": "2"}, budget={"max_cpu_hours": 3})
    h.finish_job(fut.job_name)
    assert fut.result(timeout=10) == (1, {})  # budget= was consumed as a job option


def test_jobs_without_cpu_requests_pass_a_cost_budget_with_a_note(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path, budget=Budget(max_estimated_usd=0.0001))
    dr = c.submit(double, 1, dry_run=True)
    assert dr.budget is not None and dr.budget.ok and any("requests" in n for n in dr.budget.notes)


# --- fan-out --------------------------------------------------------------------------------------------


def test_map_family_checks_the_whole_fan_out_before_submitting_any_job() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    b = Budget(max_cpu_hours=4.0)  # one job (1 core) is fine; five are not
    for fn in (sk8s.submit_map, sk8s.map):
        with pytest.raises(sk8s.BudgetExceeded) as ei:
            fn(double, range(5), requests={"cpu": "1"}, budget=b)
        assert ei.value.verdict["n_jobs"] == 5
    with pytest.raises(sk8s.BudgetExceeded):
        sk8s.submit_starmap(double, [(i,) for i in range(5)], requests={"cpu": "1"}, budget=b)
    assert stub.n_submitted() == 0


def test_fan_out_counts_batches_not_items() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    # 10 items in batches of 5 = 2 jobs x 1 core = 2 cpu-hours: fits in 2.0
    futs = sk8s.submit_map(double, range(10), batch_size=5, requests={"cpu": "1"},
                           budget=Budget(max_cpu_hours=2.0))
    assert len(futs) == 10 and stub.n_submitted() == 2


def test_default_cluster_config_budget_applies_to_fan_outs() -> None:
    stub = StubCluster()
    stub.config = Config(budget=Budget(max_estimated_usd=0.01))  # type: ignore[attr-defined]
    sk8s.configure(cluster=stub)
    with pytest.raises(sk8s.BudgetExceeded):
        sk8s.submit_map(double, range(10), requests={"cpu": "1"})
    assert stub.n_submitted() == 0


def test_pilot_fan_outs_are_checked_whole_up_front() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    with pytest.raises(sk8s.BudgetExceeded):
        sk8s.submit_map(double, range(20), pilot=2, requests={"cpu": "1"}, budget=Budget(max_cpu_hours=5))
    assert stub.n_submitted() == 0  # not even the pilot


def test_map_dry_run_carries_the_fan_out_verdict_and_cap(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(
        tmp_path, budget=Budget(max_cpu_hours=3.0, max_concurrent_pods=4)))
    dr = sk8s.map(double, range(10), dry_run=True, requests={"cpu": "1"}, max_concurrent=8)
    assert dr.budget is not None and not dr.budget.ok
    assert dr.budget.n_jobs == 10 and dr.budget.estimated_cpu_hours == pytest.approx(10.0)
    assert dr.budget.effective_max_concurrent == 4 and dr.budget.peak_pods == 4
    assert any("capped at 4" in n for n in dr.notes + dr.budget.notes)
    ok = sk8s.map(double, range(2), dry_run=True, requests={"cpu": "1"})
    assert ok.budget is not None and ok.budget.ok


def test_max_concurrent_pods_caps_a_map_that_set_no_max_concurrent() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    t = threading.Thread(
        target=lambda: sk8s.submit_map(double, range(4), budget=Budget(max_concurrent_pods=2))
    )
    t.start()
    assert _wait_for(lambda: stub.n_submitted() == 2)
    time.sleep(0.2)
    assert stub.n_submitted() == 2  # blocked, not failed
    stub.futures[0].set_result(0)
    assert _wait_for(lambda: stub.n_submitted() == 3)
    stub.futures[1].set_result(2)
    assert _wait_for(lambda: stub.n_submitted() == 4)
    t.join(timeout=5)
    assert not t.is_alive()


def test_the_tighter_of_max_concurrent_and_the_budget_wins() -> None:
    stub = StubCluster()
    sk8s.configure(cluster=stub)
    t = threading.Thread(
        target=lambda: sk8s.submit_map(
            double, range(4), max_concurrent=1, budget=Budget(max_concurrent_pods=3))
    )
    t.start()
    assert _wait_for(lambda: stub.n_submitted() == 1)
    time.sleep(0.2)
    assert stub.n_submitted() == 1
    stub.futures[0].set_result(0)
    assert _wait_for(lambda: stub.n_submitted() == 2)
    for i in (1, 2):  # still one at a time: the budget's looser 3 does not widen max_concurrent=1
        stub.futures[i].set_result(i)
        assert _wait_for(lambda: stub.n_submitted() == i + 2)
    stub.futures[3].set_result(3)
    t.join(timeout=5)
    assert not t.is_alive()


def test_config_budget_caps_the_default_clusters_fan_outs() -> None:
    stub = StubCluster()
    stub.config = Config(budget=Budget(max_concurrent_pods=1))  # type: ignore[attr-defined]
    sk8s.configure(cluster=stub)
    t = threading.Thread(target=lambda: sk8s.submit_map(double, range(3)))
    t.start()
    assert _wait_for(lambda: stub.n_submitted() == 1)
    time.sleep(0.2)
    assert stub.n_submitted() == 1
    stub.futures[0].set_result(0)
    assert _wait_for(lambda: stub.n_submitted() == 2)
    stub.futures[1].set_result(0)
    t.join(timeout=5)
    assert not t.is_alive()


def test_clusters_without_a_config_are_unaffected_by_the_budget_machinery() -> None:
    sk8s.configure(cluster=StubCluster())  # no .config attribute at all
    assert len(sk8s.submit_map(double, range(3))) == 3


# --- detached workflows ---------------------------------------------------------------------------------


def test_submit_workflow_budget_is_checked_and_travels_to_the_driver(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    over = Budget(max_cpu_hours=0.5)
    with pytest.raises(sk8s.BudgetExceeded):
        sk8s.submit_workflow(double, 1, cluster=h.cluster, requests={"cpu": "1"}, budget=over)
    assert not h.batch.jobs and not h.core.configmaps

    h2 = Harness(tmp_path / "two")
    fits = Budget(max_cpu_hours=5.0, max_concurrent_pods=2)
    handle = sk8s.submit_workflow(double, 1, cluster=h2.cluster, requests={"cpu": "1"}, budget=fits)
    (job,) = h2.batch.jobs.values()
    pod_env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    assert Budget.coerce(json.loads(pod_env["SK8S_BUDGET"])) == fits
    assert handle.id == h2.cluster.workflow_id


def test_submit_workflow_dry_run_shows_the_verdict(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = workflows.submit_workflow(double, 1, cluster=c, dry_run=True, requests={"cpu": "9"},
                                   budget=Budget(max_cpu_hours=1))
    assert dr.budget is not None and not dr.budget.ok
    assert env(dr.jobs[0])["SK8S_BUDGET"]
    assert nothing_written(tmp_path)

