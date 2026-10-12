"""Guardrails integration suite: sandboxes and budgets against a live cluster.

Needs the worker image built from THIS branch (a detached driver parses ``SK8S_BUDGET`` and
enforces it in the pod), tagged ``sk8s-dev-guard:latest``::

    minikube image build -t sk8s-dev-guard:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_guard.py -v

Override the tag with ``SK8S_GUARD_TEST_IMAGE``. Sandboxes are named ``itguard*`` (namespaces
``sk8s-sbx-itguard*``) and the workflow test uses ``sk8s-it-guard``; every one is deleted
afterwards. The CLI runs as a subprocess with ``HOME`` and the working directory in a temp dir,
so the real user's config is never read or written and the sandbox config files land in the temp
dir. The store is ``k8s`` (blobs are ConfigMaps in the namespace); store-prefix cascading on
bucket/directory stores is covered by the unit suite.
"""
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from kubernetes import client
from kubernetes.client import ApiException

import sk8s
from sk8s import k8s, sandbox
from sk8s.budget import Budget
from sk8s.cluster import Cluster
from sk8s.config import load_config
from sk8s.profile import apply, profile_for
from tests.integration.conftest import requires_cluster, suite_pull_policy

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(400)]

IMAGE = os.environ.get("SK8S_GUARD_TEST_IMAGE", "sk8s-dev-guard:latest")
NS_WORKFLOW = "sk8s-it-guard"
SANDBOXES = ("itguard", "itguard-old")


def wait_until(pred: Callable[[], Any], timeout: float, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    value = pred()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = pred()
    return value


def _ns_exists(core: client.CoreV1Api, name: str) -> bool:
    try:
        core.read_namespace(name=name)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _delete_ns(core: client.CoreV1Api, name: str) -> None:
    if _ns_exists(core, name):
        try:
            core.delete_namespace(name=name)
        except ApiException as e:
            if e.status != 404:
                raise
    assert wait_until(lambda: not _ns_exists(core, name), timeout=180), f"namespace {name} stuck"


@pytest.fixture(scope="module")
def core() -> client.CoreV1Api:
    return k8s.core_api()


@pytest.fixture(autouse=True)
def clean_slate(core: client.CoreV1Api) -> Iterator[None]:
    def wipe() -> None:
        for name in SANDBOXES:
            _delete_ns(core, sandbox.namespace_for(name))
        _delete_ns(core, NS_WORKFLOW)

    wipe()  # leftovers of an aborted earlier run
    yield
    wipe()


def _kubeconfig() -> str:
    return os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A HOME whose sk8s config names the dev image: what a sandbox inherits from."""
    h = tmp_path / "home"
    (h / ".config" / "sk8s").mkdir(parents=True)
    (h / ".config" / "sk8s" / "config.toml").write_text(
        f'store = "k8s"\nimage = "{IMAGE}"\npull_policy = "{suite_pull_policy()}"\n'
        "ttl_seconds_after_finished = 30\n"
    )
    (tmp_path / "cwd").mkdir()
    return h


def run_cli(home: Path, *argv: str, sandbox_config: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SK8S_")}
    env["HOME"] = str(home)
    env["KUBECONFIG"] = _kubeconfig()
    if sandbox_config is not None:
        env["SK8S_CONFIG"] = str(sandbox_config)
    return subprocess.run(
        [sys.executable, "-c", "import sys; from sk8s.cli import main; sys.exit(main())", *argv],
        env=env, cwd=home.parent / "cwd", capture_output=True, text=True, timeout=180,
    )


def _age(core: client.CoreV1Api, name: str) -> None:
    """Pretend the sandbox was created 3 days ago and expired 2 days ago."""
    now = datetime.datetime.now(datetime.timezone.utc)
    core.patch_namespace(sandbox.namespace_for(name), {"metadata": {"annotations": {
        sandbox.ANN_CREATED: (now - datetime.timedelta(days=3)).isoformat(),
        sandbox.ANN_EXPIRES: (now - datetime.timedelta(days=2)).isoformat(),
    }}})


def test_sandbox_lifecycle_budget_and_cascade(home: Path, core: client.CoreV1Api) -> None:
    cwd = home.parent / "cwd"
    ns = sandbox.namespace_for("itguard")

    # create: labelled namespace + RBAC + ttl annotation + isolated config + export line
    done = run_cli(home, "sandbox", "create", "--name", "itguard", "--budget-usd", "0.001", "--ttl", "1h")
    assert done.returncode == 0, (done.stdout, done.stderr)
    cfg_file = cwd / ".sk8s-sandbox-itguard.toml"
    assert f"export SK8S_CONFIG={cfg_file}" in done.stdout.splitlines()
    nsobj = core.read_namespace(name=ns)
    assert nsobj.metadata.labels["sk8s/kind"] == "sandbox"
    assert sandbox.ANN_EXPIRES in nsobj.metadata.annotations
    # the user's own config was not touched
    assert "sk8s-sbx" not in (home / ".config" / "sk8s" / "config.toml").read_text()

    listed = run_cli(home, "sandbox", "list", "--json")
    assert listed.returncode == 0 and '"itguard"' in listed.stdout, (listed.stdout, listed.stderr)

    # doctor works inside it (the profile apply did the RBAC)
    doc = run_cli(home, "doctor", sandbox_config=cfg_file)
    assert doc.returncode == 0, (doc.stdout, doc.stderr)

    # a job runs inside the sandbox with the isolated config
    cfg = load_config(cfg_file, env={})
    assert cfg.namespace == ns and cfg.budget == Budget(max_estimated_usd=0.001)
    c = Cluster(cfg)
    try:
        assert c.run(lambda: 6 * 7) == 42  # no cpu request: costs nothing, within any budget
        # budget rejection: 1 core for an assumed hour is $0.04 > $0.001; nothing is created
        jobs_before = len(c.batch.list_namespaced_job(ns).items)
        with pytest.raises(sk8s.BudgetExceeded) as ei:
            c.run(lambda: 1, requests={"cpu": "1"})
        assert "max_estimated_usd" in str(ei.value)
        assert len(c.batch.list_namespaced_job(ns).items) == jobs_before
        # the same job previews (dry_run) with the verdict instead of raising
        dr = c.submit(lambda: 1, requests={"cpu": "1"}, dry_run=True)
        assert dr.budget is not None and not dr.budget.ok
        assert core.list_namespaced_config_map(ns).items  # the job's anchor/blobs live in the sandbox
    finally:
        c.close()

    # delete cascades: namespace (and everything in it) and the local config file
    gone = run_cli(home, "sandbox", "delete", "itguard")
    assert gone.returncode == 0, (gone.stdout, gone.stderr)
    assert wait_until(lambda: not _ns_exists(core, ns), timeout=180)
    assert not cfg_file.exists()
    assert '"itguard"' not in run_cli(home, "sandbox", "list", "--json").stdout


def test_reaping_expired_sandboxes_respects_active_jobs(home: Path, core: client.CoreV1Api) -> None:
    ns = sandbox.namespace_for("itguard-old")
    assert run_cli(home, "sandbox", "create", "--name", "itguard-old").returncode == 0
    cfg_file = home.parent / "cwd" / ".sk8s-sandbox-itguard-old.toml"
    c = Cluster(load_config(cfg_file, env={}))
    try:
        fut = c.submit(time.sleep, 120)  # an active job pins the sandbox
        _age(core, "itguard-old")
        batch = k8s.batch_api()
        report = sandbox.reap(core_api=core, batch_api=batch, apps_api=k8s.apps_api())
        assert "itguard-old" not in report.deleted
        assert any(k["sandbox"] == "itguard-old" and "active jobs" in k["reason"] for k in report.kept)
        assert _ns_exists(core, ns)

        # the job ends: now the same sweep (via the public clean()) reaps it
        fut.cancel()
        assert wait_until(lambda: not batch.list_namespaced_job(ns).items, timeout=60)
    finally:
        c.close(delete_workflow=False)
    deleted = sk8s.clean(ns)  # cluster-wide sandbox sweep rides along the workflow sweep
    assert deleted == []  # no workflow expired; the sandbox did
    assert wait_until(lambda: not _ns_exists(core, ns), timeout=180)


# --- budgets inside a detached workflow ------------------------------------------------------------------


def _wf_cluster(core: client.CoreV1Api) -> Cluster:
    prof = profile_for("local", namespace=NS_WORKFLOW, store="k8s")
    apply(prof)
    return Cluster(load_config(
        Path("/nonexistent"), env={}, namespace=NS_WORKFLOW, image=IMAGE,
        pull_policy=suite_pull_policy(), store="k8s", ttl_seconds_after_finished=30,
    ))


def _driver_fanout() -> Any:
    """A driver defined as a closure, so it pickles by value (a module-level function of this test
    module would pickle by reference to ``tests.*``, which the worker image does not have)."""
    def driver(n: int, cpu: str) -> list[int]:
        import sk8s as inner

        return inner.map(lambda x: x * 2, range(n), requests={"cpu": cpu})

    return driver


def test_budget_travels_to_a_detached_driver_and_bites_there(core: client.CoreV1Api) -> None:
    # 4 jobs x 1 core x 1h assumed = 4 cpu-hours > 1: the DRIVER's nested map raises in the pod
    c = _wf_cluster(core)
    handle = sk8s.submit_workflow(
        _driver_fanout(), 4, "1", cluster=c, budget=Budget(max_cpu_hours=1.0), keep_results=True,
    )
    try:
        with pytest.raises(sk8s.BudgetExceeded) as ei:
            handle.result(timeout=240, delete=False)
        assert "max_cpu_hours" in str(ei.value)
        workers = [
            j for j in c.batch.list_namespaced_job(NS_WORKFLOW).items
            if not j.metadata.name.startswith("wfdriver")
        ]
        assert workers == []  # the fan-out was refused whole, before its first job
    finally:
        handle.kill()
        c.close(delete_workflow=False)

    # the same driver inside its budget (and capped to 2 concurrent pods) completes
    c2 = _wf_cluster(core)
    ok = sk8s.submit_workflow(
        _driver_fanout(), 4, "10m", cluster=c2,
        budget=Budget(max_cpu_hours=1.0, max_concurrent_pods=2),
    )
    try:
        assert ok.result(timeout=240) == [0, 2, 4, 6]
    finally:
        c2.close(delete_workflow=False)
