"""Cluster-config integration suite: ``sk8s config`` / ``doctor`` / CLI against a live cluster.

Needs the worker image built from THIS branch, tagged ``sk8s-dev-config:latest``::

    minikube image build -t sk8s-dev-config:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_config_cli.py -v

Override the tag with ``SK8S_CONFIG_TEST_IMAGE``. Every test works in its OWN fresh namespace
(``sk8s-it-cfg*``) and never touches ``default`` or any other namespace's resources. The CLI is
exercised as a subprocess with ``HOME`` pointed at a temp dir, so ``~/.config/sk8s/config.toml``
of the real user is never read or written.
"""
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from sk8s import anchors, k8s
from sk8s.cluster import Cluster
from sk8s.config import load_config
from sk8s.doctor import doctor
from sk8s.profile import apply, profile_for
from tests.integration.conftest import requires_cluster, suite_pull_policy

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

IMAGE = os.environ.get("SK8S_CONFIG_TEST_IMAGE", "sk8s-dev-config:latest")
NS_LIFECYCLE = "sk8s-it-cfg"
NS_BARE = "sk8s-it-cfg-bare"
NS_CLI = "sk8s-it-cfg-cli"


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
    """Delete ``name`` and wait for it to be fully gone (it terminates asynchronously)."""
    if _ns_exists(core, name):
        try:
            core.delete_namespace(name=name)
        except ApiException as e:
            if e.status != 404:
                raise
    assert wait_until(lambda: not _ns_exists(core, name), timeout=120), f"namespace {name} stuck"


@pytest.fixture(scope="module")
def core() -> client.CoreV1Api:
    return k8s.core_api()


@pytest.fixture
def fresh_ns(core: client.CoreV1Api) -> Iterator[Callable[[str], str]]:
    """Hand out a namespace name that starts absent; every one handed out is deleted afterwards."""
    handed: list[str] = []

    def make(name: str) -> str:
        _delete_ns(core, name)  # leftover from an aborted earlier run
        handed.append(name)
        return name

    yield make
    for name in handed:
        _delete_ns(core, name)  # the finalizer: namespace deletion cascades everything in it


def cluster_for(namespace: str, tmp_path: Path) -> Cluster:
    return Cluster(
        load_config(
            tmp_path / "absent.toml",  # never read the real user's config
            namespace=namespace,
            image=IMAGE,
            pull_policy=suite_pull_policy(),
            store="k8s",
            ttl_seconds_after_finished=30,
        )
    )


class Recorder:
    """Transparent API proxy that records every non-read call (the writes apply() makes)."""

    def __init__(self, api: Any, writes: list[str]) -> None:
        self._api = api
        self._writes = writes

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._api, name)
        if callable(attr) and not name.startswith(("read_", "list_", "get_")):
            def recorded(*a: Any, **kw: Any) -> Any:
                self._writes.append(name)
                return attr(*a, **kw)

            return recorded
        return attr


def _snapshot(core: client.CoreV1Api, rbac: client.RbacAuthorizationV1Api, ns: str) -> dict[str, str]:
    return {
        "namespace": core.read_namespace(name=ns).metadata.resource_version,
        "serviceaccount": core.read_namespaced_service_account("sk8s", ns).metadata.resource_version,
        "role": rbac.read_namespaced_role("sk8s", ns).metadata.resource_version,
        "rolebinding": rbac.read_namespaced_role_binding("sk8s", ns).metadata.resource_version,
    }


# --- CLI harness -------------------------------------------------------------------------------


def _kubeconfig() -> str:
    return os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")


def write_user_config(home: Path, **keys: str) -> None:
    cfg_dir = home / ".config" / "sk8s"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    lines = [f'{k} = "{v}"' for k, v in keys.items()]
    (cfg_dir / "config.toml").write_text("\n".join(lines) + "\n")


def run_cli(home: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run the real ``sk8s`` entrypoint in a subprocess whose HOME is ``home``.

    KUBECONFIG is pinned to the real kubeconfig (HOME no longer points at it); every SK8S_* var
    is dropped so only the temp config file speaks.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SK8S_")}
    env["HOME"] = str(home)
    env["KUBECONFIG"] = _kubeconfig()
    return subprocess.run(
        [sys.executable, "-c", "import sys; from sk8s.cli import main; sys.exit(main())", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# --- 1. fresh-namespace lifecycle ---------------------------------------------------------------


def test_fresh_namespace_lifecycle(
    fresh_ns: Callable[[str], str], core: client.CoreV1Api, tmp_path: Path
) -> None:
    ns = fresh_ns(NS_LIFECYCLE)
    prof = profile_for("local", namespace=ns, store="k8s")
    rbac = k8s.rbac_api()

    assert not _ns_exists(core, ns)
    first = apply(prof)  # apply ensures the namespace itself
    assert first[0] == f"Namespace {ns}: created"
    assert all("created" in line for line in first), first
    assert _ns_exists(core, ns)

    checks = doctor(prof)
    assert all(c.ok for c in checks), [(c.name, c.fix_hint) for c in checks if not c.ok]

    # One job, end to end, in the new namespace under the freshly applied ServiceAccount/Role.
    c = cluster_for(ns, tmp_path)
    try:
        assert c.run(lambda: 6 * 7) == 42
    finally:
        c.close()

    # Second apply converges: nothing reported changed, and not a single write call was made.
    before = _snapshot(core, rbac, ns)
    writes: list[str] = []
    second = apply(prof, core_api=Recorder(core, writes), rbac_api=Recorder(rbac, writes))  # type: ignore[arg-type]
    assert writes == [], writes
    assert second == [
        f"Namespace {ns}: exists",
        "ServiceAccount sk8s: unchanged",
        "Role sk8s: unchanged",
        "RoleBinding sk8s: unchanged",
    ]
    assert _snapshot(core, rbac, ns) == before  # resourceVersions untouched
    assert all(c.ok for c in doctor(prof))


# --- 2. doctor red path -------------------------------------------------------------------------


def test_doctor_red_path_via_cli(
    fresh_ns: Callable[[str], str], tmp_path: Path
) -> None:
    ns = fresh_ns(NS_BARE)
    k8s.core_api().create_namespace(body=client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))
    home = tmp_path / "home"
    write_user_config(home, namespace=ns, store="k8s")

    proc = run_cli(home, "doctor")
    out = proc.stdout
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    lines = out.splitlines()

    def status(check: str) -> str:
        return next(ln for ln in lines if check in ln).split()[0]

    assert status("cluster reachable") == "PASS"
    assert status("namespace exists") == "PASS"
    assert status("service account exists") == "FAIL"
    assert status("role permissions") == "FAIL"
    # Each failure carries a specific, actionable hint naming the object and the fix.
    assert f"ServiceAccount 'sk8s' not found in '{ns}'; run: sk8s config" in out
    assert f"Role 'sk8s' not found in '{ns}'; run: sk8s config" in out


def test_doctor_cli_green_after_config(
    fresh_ns: Callable[[str], str], tmp_path: Path
) -> None:
    """Fix path end to end: the bare namespace's red doctor turns green after ``sk8s config``."""
    ns = fresh_ns(NS_BARE)
    home = tmp_path / "home"
    write_user_config(home, namespace=ns, store="k8s")

    assert run_cli(home, "doctor").returncode == 1  # namespace itself absent -> red

    done = run_cli(home, "config")
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert f"Namespace {ns}: created" in done.stdout

    proc = run_cli(home, "doctor")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "FAIL" not in proc.stdout


# --- 3. CLI smoke on the real cluster -------------------------------------------------------------


def test_config_dry_run_defaults_to_sk8s_namespace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()  # no config file, no SK8S_* env: the built-in default applies
    proc = run_cli(home, "config", "--dry-run")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "ensure Namespace sk8s" in proc.stdout
    assert "ServiceAccount sk8s in sk8s" in proc.stdout
    assert not (home / ".config" / "sk8s" / "config.toml").exists()  # dry run writes nothing


def test_workflows_lists_and_kill_removes(
    fresh_ns: Callable[[str], str], core: client.CoreV1Api, tmp_path: Path
) -> None:
    from sk8s import submit_workflow

    ns = fresh_ns(NS_CLI)
    apply(profile_for("local", namespace=ns, store="k8s"))
    home = tmp_path / "home"
    write_user_config(home, namespace=ns, store="k8s", image=IMAGE, pull_policy=suite_pull_policy())

    empty = run_cli(home, "workflows")
    assert empty.returncode == 0, (empty.stdout, empty.stderr)
    assert f"no workflows in namespace {ns}" in empty.stdout

    def sleeper() -> None:
        import time

        time.sleep(300)

    c = cluster_for(ns, tmp_path)
    handle = submit_workflow(sleeper, cluster=c)
    wf = handle.id

    def objects() -> tuple[list[str], list[str]]:
        sel = f"{anchors.WORKFLOW_LABEL}={wf}"
        cms = core.list_namespaced_config_map(ns, label_selector=sel).items
        jobs = k8s.batch_api().list_namespaced_job(ns, label_selector=sel).items
        return [i.metadata.name for i in cms], [i.metadata.name for i in jobs]

    try:
        listed = run_cli(home, "workflows")
        assert listed.returncode == 0, (listed.stdout, listed.stderr)
        row = next(ln for ln in listed.stdout.splitlines() if ln.startswith(wf))
        assert "yes" in row.split()  # DETACHED column
        assert all(objects())  # anchor/blobs and the driver job exist

        killed = run_cli(home, "kill", wf)
        assert killed.returncode == 0, (killed.stdout, killed.stderr)
        assert f"killed {wf}" in killed.stdout
        assert wait_until(lambda: objects() == ([], []), timeout=30), objects()

        after = run_cli(home, "workflows")
        assert wf not in after.stdout

        again = run_cli(home, "kill", wf)  # already gone: a clean error, not a traceback
        assert again.returncode == 1
        assert "unknown workflow" in again.stderr and "Traceback" not in again.stderr
    finally:
        handle.kill()  # no-op once deleted; never leaks a detached workflow on failure
        handle.close()
        c.close(delete_workflow=False)
