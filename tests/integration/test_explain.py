"""explain / dry_run integration suite: a live detached workflow, a stuck pod, a cold-start preview.

Needs the worker image built from THIS branch, tagged ``sk8s-dev-explain:latest``::

    minikube image build -t sk8s-dev-explain:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_explain.py -v

Override the tag with ``SK8S_EXPLAIN_TEST_IMAGE``. Everything runs in its OWN namespace
(``sk8s-it-explain``, provisioned with ``sk8s config``'s machinery and deleted afterwards).
The CLI is exercised as a subprocess with ``HOME`` pointed at a temp dir.
"""
import json
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
from sk8s import anchors, k8s
from sk8s.cluster import Cluster
from sk8s.config import load_config
from sk8s.profile import apply, profile_for
from tests.integration.conftest import requires_cluster, suite_pull_policy

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

IMAGE = os.environ.get("SK8S_EXPLAIN_TEST_IMAGE", "sk8s-dev-explain:latest")
NS = "sk8s-it-explain"
BAD_IMAGE = "registry.invalid/sk8s-does-not-exist:1"


def wait_until(pred: Callable[[], Any], timeout: float, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    value = pred()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = pred()
    return value


def _gone(core: client.CoreV1Api) -> bool:
    try:
        core.read_namespace(name=NS)
        return False
    except ApiException as e:
        if e.status == 404:
            return True
        raise


@pytest.fixture(scope="module")
def namespace() -> Iterator[str]:
    core = k8s.core_api()
    if not _gone(core):  # leftover from an aborted run
        core.delete_namespace(name=NS)
        assert wait_until(lambda: _gone(core), 120), f"namespace {NS} stuck terminating"
    apply(profile_for("local", namespace=NS, store="k8s"))
    yield NS
    try:
        core.delete_namespace(name=NS)
    except ApiException as e:
        if e.status != 404:
            raise


def new_cluster(tmp_path: Path, **kw: Any) -> Cluster:
    return Cluster(
        load_config(
            tmp_path / "absent.toml", namespace=NS, image=kw.pop("image", IMAGE),
            pull_policy=kw.pop("pull_policy", suite_pull_policy()), store="k8s",
            ttl_seconds_after_finished=30,
        ),
        **kw,
    )


def run_cli(home: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SK8S_")}
    env["HOME"] = str(home)
    env["KUBECONFIG"] = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
    env["SK8S_NAMESPACE"] = NS
    env["SK8S_STORE"] = "k8s"
    return subprocess.run(
        [sys.executable, "-c", "import sys; from sk8s.cli import main; sys.exit(main())", *argv],
        env=env, capture_output=True, text=True, timeout=120,
    )


def make_driver() -> Callable[[], int]:
    """Closures pickle by value: the worker image has sk8s but not this test module."""

    def square(x: int) -> int:
        return x * x

    def driver() -> int:
        import time as _time

        print("driver started", flush=True)
        results = sk8s.map(square, [1, 2, 3])
        print(f"children done: {results}", flush=True)
        _time.sleep(90)  # stay alive so explain sees a live, healthy workflow
        return sum(results)

    return driver


def _namespace_objects(c: Cluster) -> tuple[list[str], list[str]]:
    jobs = sorted(j.metadata.name for j in c.batch.list_namespaced_job(NS).items)
    cms = sorted(m.metadata.name for m in c.core.list_namespaced_config_map(NS).items)
    return jobs, cms


# --- 1. a healthy, live, detached workflow ------------------------------------------------------


def test_explain_a_live_detached_workflow(
    namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SK8S_CONFIG", str(tmp_path / "absent.toml"))  # never the user's config
    monkeypatch.setenv("SK8S_STORE", "k8s")
    sk8s.configure(namespace=NS, image=IMAGE, pull_policy=suite_pull_policy(),
                   ttl_seconds_after_finished=30)
    try:
        handle = sk8s.submit_workflow(make_driver(), requests={"cpu": "100m"})
    finally:
        sk8s.configure()
    try:
        # Explain from a COLD cluster handle: nothing but cluster + store state is consulted.
        fresh = new_cluster(tmp_path, watch=False)

        def healthy() -> dict[str, Any] | None:
            report = sk8s.explain(handle.id, cluster=fresh)
            logs = report["driver"]["logs_tail"] or []
            done = report["counts"]["succeeded"] >= 3 and any("children done" in line for line in logs)
            return report if done and report["driver"]["phase"] == "running" else None

        report = wait_until(healthy, 150, 2.0)
        assert report, sk8s.explain(handle.id, cluster=fresh)
        json.dumps(report)
        assert report["workflow"]["id"] == handle.id and report["workflow"]["detached"] is True
        assert report["driver"]["phase"] == "running"
        assert "driver started" in report["driver"]["logs_tail"]
        assert report["blocking"] == [], report["blocking"]
        assert report["warnings"] == [], report["warnings"]
        phases = [j["phase"] for j in report["jobs"] if j["role"] == "job"]
        assert phases.count("succeeded") >= 3
        assert report["store"]["blob_count"] is not None and report["store"]["blob_count"] >= 1
        assert report["estimated_cost_label"] == "rough" and report["estimated_cost_usd"] >= 0

        # The CLI sees the same thing, as text and as JSON.
        home = tmp_path / "home"
        home.mkdir()
        human = run_cli(home, "explain", handle.id)
        assert human.returncode == 0, human.stderr
        assert handle.id in human.stdout and "nothing is blocking progress" in human.stdout
        as_json = run_cli(home, "explain", handle.id, "--json")
        assert as_json.returncode == 0, as_json.stderr
        assert json.loads(as_json.stdout)["workflow"]["id"] == handle.id
        listing = json.loads(run_cli(home, "workflows", "--json").stdout)
        assert handle.id in [w["id"] for w in listing]
        missing = run_cli(home, "explain", "wf-doesnotexist")
        assert missing.returncode == 1 and "unknown workflow" in missing.stderr
        fresh.close(delete_workflow=False)
    finally:
        handle.kill()


# --- 2. a stuck pod: the interpreted reason and the blocking list -------------------------------


def test_explain_a_pod_stuck_on_a_bad_image(namespace: str, tmp_path: Path) -> None:
    # watch=False: no watcher thread, so nothing resolves or deletes the stuck job under us.
    c = new_cluster(tmp_path, image=BAD_IMAGE, pull_policy="IfNotPresent", watch=False)
    try:
        c.submit(lambda: 9, name_prefix="stuck")

        def stuck() -> dict[str, Any] | None:
            report = sk8s.explain(c.workflow_id, cluster=c)
            return report if report["blocking"] else None

        report = wait_until(stuck, 120, 2.0)
        assert report, sk8s.explain(c.workflow_id, cluster=c)
        (job,) = report["jobs"]
        assert job["phase"] == "pending"
        assert job["waiting_code"] in ("ErrImagePull", "ImagePullBackOff")
        assert BAD_IMAGE in job["waiting_reason"] and "cannot pull image" in job["waiting_reason"]
        assert any(BAD_IMAGE in b and "cannot pull image" in b for b in report["blocking"])
        assert report["counts"] == {"running": 0, "pending": 1, "succeeded": 0, "failed": 0, "total": 1}

        home = tmp_path / "home"
        home.mkdir()
        out = run_cli(home, "explain", c.workflow_id)
        assert out.returncode == 0, out.stderr  # a report, not a check: exit 0 even when unhealthy
        assert "blocking:" in out.stdout and BAD_IMAGE in out.stdout
    finally:
        c.close(delete_workflow=True)


# --- 3. dry_run on a live cluster ----------------------------------------------------------------


def test_dry_run_creates_nothing(namespace: str, tmp_path: Path) -> None:
    c = new_cluster(tmp_path)
    try:
        before = _namespace_objects(c)
        one = c.submit(lambda: 4, dry_run=True, requests={"cpu": "250m", "memory": "64Mi"})
        sk8s.configure(cluster=c)
        try:
            many = sk8s.map(lambda x: x * x, range(100), dry_run=True, batch_size=10, requests={"cpu": "1"})
        finally:
            sk8s.configure()
        assert one.n_jobs == 1 and one.image == IMAGE and one.namespace == NS
        assert one.jobs[0]["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
        assert many.n_jobs == 10 and many.n_items == 100 and many.total_cpu_request == 10.0
        assert len(many.jobs) == 1
        assert _namespace_objects(c) == before  # no job, no anchor, no payload blob
        assert not [m for m in before[1] if m.startswith(anchors.anchor_name(c.workflow_id))]
        # ...and the very same options really submit afterwards.
        assert c.run(lambda: 36, requests={"cpu": "250m", "memory": "64Mi"}) == 36
    finally:
        c.close()
