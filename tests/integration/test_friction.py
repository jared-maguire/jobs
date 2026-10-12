"""Friction-fix smoke: dry_run's unset-request flag vs. the REAL driver pod, and the empty-log note.

Needs the worker image built from THIS branch, tagged ``sk8s-dev-fric:latest``::

    minikube image build -t sk8s-dev-fric:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_friction.py -v

Override the tag with ``SK8S_FRIC_TEST_IMAGE``. Runs in its OWN namespace (``sk8s-it-fric``,
deleted afterwards). The CLI is exercised as a subprocess with ``HOME`` pointed at a temp dir.
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
from sk8s import explaining, k8s
from sk8s.profile import apply, profile_for
from tests.integration.conftest import requires_cluster, suite_pull_policy

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

IMAGE = os.environ.get("SK8S_FRIC_TEST_IMAGE", "sk8s-dev-fric:latest")
NS = "sk8s-it-fric"


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
    if not _gone(core):
        core.delete_namespace(name=NS)
        assert wait_until(lambda: _gone(core), 120), f"namespace {NS} stuck terminating"
    apply(profile_for("local", namespace=NS, store="k8s"))
    yield NS
    try:
        core.delete_namespace(name=NS)
    except ApiException as e:
        if e.status != 404:
            raise


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


def make_silent_driver() -> Callable[[], int]:
    """Prints nothing at all (closures pickle by value: the worker image lacks this module)."""

    def square(x: int) -> int:
        return x * x

    def driver() -> int:
        import time as _time

        total = sum(sk8s.map(square, [1, 2, 3]))
        _time.sleep(60)  # alive, and silent, while the test reads its (empty) log
        return total

    return driver


def test_dry_run_flags_unset_requests_and_the_real_driver_pod_confirms_it(
    namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SK8S_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("SK8S_STORE", "k8s")
    # configure(store=<url string>) used to be dropped on the floor by Cluster(store=BlobStore).
    sk8s.configure(namespace=NS, image=IMAGE, pull_policy=suite_pull_policy(),
                   ttl_seconds_after_finished=30, store="k8s")
    try:
        preview = sk8s.submit_workflow(make_silent_driver(), dry_run=True)
        assert preview.store == "k8s" and preview.namespace == NS
        assert preview.total_cpu_request == 0 and preview.total_memory_request == 0
        assert preview.unset_requests == ["cpu", "memory"]
        assert any("driver" in n and "Autopilot" in n for n in preview.notes)
        sized = sk8s.submit_workflow(make_silent_driver(), dry_run=True, requests={"cpu": "100m"})
        assert sized.unset_requests == ["memory"]

        handle = sk8s.submit_workflow(make_silent_driver())
    except BaseException:
        sk8s.configure()
        raise
    home = tmp_path / "home"
    home.mkdir()
    try:
        # The real driver Job carries exactly what the preview body said: sk8s sets no resources.
        job = next(
            j for j in k8s.batch_api().list_namespaced_job(NS).items
            if j.metadata.name.startswith("wfdriver-")
        )
        real = job.spec.template.spec.containers[0].resources
        assert not (real and (real.requests or real.limits)), real
        assert "resources" not in preview.jobs[0]["spec"]["template"]["spec"]["containers"][0]

        def running_and_silent() -> dict[str, Any] | None:
            report = sk8s.explain(handle.id)
            drv = report["driver"]
            return report if drv and drv["phase"] == "running" and drv["logs_note"] else None

        report = wait_until(running_and_silent, 150, 2.0)
        assert report, sk8s.explain(handle.id)
        json.dumps(report)
        assert report["driver"]["logs_note"] == explaining.EMPTY_DRIVER_LOG_NOTE
        assert "print() from the driver appears here" in explaining.render(report)

        logs = run_cli(home, "logs", handle.id)
        assert logs.returncode == 0, logs.stderr
        assert logs.stdout.strip() == ""  # stdout stays the log
        assert "driver produced no output; print() from the driver appears here" in logs.stderr
        human = run_cli(home, "explain", handle.id)
        assert human.returncode == 0, human.stderr
        assert "driver produced no output" in human.stdout
    finally:
        sk8s.configure()
        handle.kill()
