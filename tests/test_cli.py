import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client import ApiException

import sk8s.cleaning as cleaning
import sk8s.config as config_mod
import sk8s.doctor as doctor_mod
import sk8s.images as images
import sk8s.k8s as k8s
import sk8s.profile as profile_mod
import sk8s.workflows as workflows
from sk8s import anchors, cli
from sk8s.doctor import Check
from sk8s.errors import Sk8sError
from tests.fakes import FakeBatchV1, FakeCoreV1, FakeRbacV1, FakeStorageV1

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

NS = "team-a"


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated config path and env; the kube context namespace is 'ctx-ns'."""
    for key in list(os.environ):
        if key.startswith("SK8S_"):
            monkeypatch.delenv(key)
    path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(k8s, "context_namespace", lambda: "ctx-ns")
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "local")
    monkeypatch.setattr(profile_mod, "active_context", lambda: None)  # never the dev's real one
    return path


def _fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCoreV1, FakeBatchV1, FakeRbacV1]:
    core, batch, rbac = FakeCoreV1(), FakeBatchV1(), FakeRbacV1()
    monkeypatch.setattr(k8s, "core_api", lambda: core)
    monkeypatch.setattr(k8s, "batch_api", lambda: batch)
    monkeypatch.setattr(k8s, "rbac_api", lambda: rbac)
    return core, batch, rbac


def _storage(monkeypatch: pytest.MonkeyPatch, classes: tuple[str, ...] = ()) -> FakeStorageV1:
    storage = FakeStorageV1(classes)
    monkeypatch.setattr(k8s, "storage_api", lambda: storage)
    return storage


# --- usage errors / parser wiring ----------------------------------------------------------------


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        cli.run(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith("sk8s ")


@pytest.mark.parametrize("argv", [
    [],
    ["bogus"],
    ["logs"],
    ["kill"],
    ["image"],
    ["image", "build"],  # --tag required
    ["image", "build", "--tag", "t"],  # no deps
    ["image", "build", "--tag", "t", "--pip", "x", "--channel", "c"],  # channel w/o conda
    ["image", "push"],
    ["clean", "--older-than", "garbage"],
    ["config", "--no-such-flag"],
])
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as e:
        cli.run(argv)
    assert e.value.code == 2


def test_image_build_wiring(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_build(tag: str, **kw: Any) -> str:
        calls.append((tag, kw))
        return tag

    monkeypatch.setattr(images, "build", fake_build)
    rc = cli.run([
        "image", "build", "--tag", "me/img:1", "--pip", "numpy", "--pip", "pandas",
        "--conda", "scipy", "--channel", "conda-forge", "--channel", "bioconda", "--push",
    ])
    assert rc == 0
    assert calls == [("me/img:1", {
        "pip": ["numpy", "pandas"], "conda": ["scipy"],
        "channels": ["conda-forge", "bioconda"], "push": True,
    })]
    assert "me/img:1" in capsys.readouterr().out


def test_image_build_base_parses_and_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    ns = cli.build_parser().parse_args(
        ["image", "build", "--tag", "t:1", "--base", "sk8s:dev", "--pip", "pysam"]
    )
    assert ns.base == "sk8s:dev"
    assert cli.build_parser().parse_args(["image", "build", "--tag", "t", "--pip", "x"]).base is None

    built: list[dict[str, Any]] = []
    monkeypatch.setattr(images, "build", lambda tag, **kw: built.append(kw) or tag)
    assert cli.run(["image", "build", "--tag", "t:1", "--base", "b:1", "--pip", "x"]) == 0
    assert built[0]["base"] == "b:1"


def test_image_build_base_with_conda_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("docker ran"))
    rc = cli.run(["image", "build", "--tag", "t:1", "--base", "b:1", "--conda", "x"])
    assert rc == 1
    assert "base cannot be combined with conda" in capsys.readouterr().err


def test_image_push_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr(images, "push", lambda tag, **kw: pushed.append(tag))
    assert cli.run(["image", "push", "--tag", "me/img:1"]) == 0
    assert pushed == ["me/img:1"]


def test_image_failure_is_exit_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(tag: str, **kw: Any) -> None:
        raise Sk8sError("docker push failed (exit 1):\ndenied")

    monkeypatch.setattr(images, "push", boom)
    assert cli.run(["image", "push", "--tag", "t"]) == 1
    err = capsys.readouterr().err
    assert "denied" in err and "Traceback" not in err


def test_kill_wiring(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    killed: list[str] = []

    class Handle:
        def __init__(self, wf: str) -> None:
            self.wf = wf

        def kill(self) -> None:
            killed.append(self.wf)

    attached: list[str] = []

    def fake_attach(wf: str, cluster: Any = None) -> Handle:
        attached.append(wf)
        return Handle(wf)

    monkeypatch.setattr(workflows, "attach", fake_attach)
    assert cli.run(["kill", "wf-abc"]) == 0
    assert attached == ["wf-abc"] and killed == ["wf-abc"]
    assert "wf-abc" in capsys.readouterr().out


def test_kill_unknown_workflow_exit_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def attach(wf: str, cluster: Any = None) -> Any:
        raise Sk8sError(f"unknown workflow {wf}")

    monkeypatch.setattr(workflows, "attach", attach)
    assert cli.run(["kill", "wf-x"]) == 1
    assert "unknown workflow wf-x" in capsys.readouterr().err


def test_api_error_is_exit_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def attach(wf: str, cluster: Any = None) -> Any:
        raise ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(workflows, "attach", attach)
    assert cli.run(["kill", "wf-x"]) == 1
    err = capsys.readouterr().err
    assert "403" in err and "Traceback" not in err


# --- clean / durations ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,days", [
    ("7d", 7.0), ("36h", 1.5), ("3", 3.0), ("0", 0.0), ("1.5d", 1.5), ("12H", 0.5), (" 2d ", 2.0),
])
def test_parse_duration_days(text: str, days: float) -> None:
    assert cli.parse_duration_days(text) == pytest.approx(days)


@pytest.mark.parametrize("text", ["", "d", "h", "abc", "-1", "-3d", "1w", "7 d", "1e3", "nan", "inf", "7dd"])
def test_parse_duration_rejects_garbage(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_duration_days(text)


@pytest.mark.parametrize("flag,expected", [
    (["--older-than", "36h"], 1.5), (["--older-than", "7d"], 7.0),
    (["--older-than", "2"], 2.0), ([], None),
])
def test_clean_wiring(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    flag: list[str], expected: float | None,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_clean(namespace: str | None = None, **kw: Any) -> list[str]:
        calls.append((namespace, kw))
        return ["wf-1", "wf-2"]

    monkeypatch.setattr(cleaning, "clean", fake_clean)
    assert cli.run(["clean", *flag]) == 0
    ((ns, kw),) = calls
    report = kw.pop("report")  # clean() also sweeps the artifact CAS; the CLI collects its report
    stores = kw.pop("extra_stores")  # ... of the configured store too
    assert (ns, kw) == ("ctx-ns", {"older_than_days": expected}) and hasattr(report, "summary")
    assert len(stores) == 1
    out = capsys.readouterr().out
    assert "wf-1" in out and "wf-2" in out


def test_clean_prints_the_artifact_sweep_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_clean(namespace: str | None = None, **kw: Any) -> list[str]:
        kw["report"].deleted.append("ab" * 32)
        return []

    monkeypatch.setattr(cleaning, "clean", fake_clean)
    assert cli.run(["clean"]) == 0
    out = capsys.readouterr().out
    assert "artifact sweep: deleted 1 unreachable" in out and "nothing to clean" not in out


def test_clean_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cleaning, "clean", lambda *a, **k: [])
    assert cli.run(["clean"]) == 0
    assert "nothing" in capsys.readouterr().out


def test_clean_uses_configured_namespace(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    env.parent.mkdir(parents=True)
    env.write_text('namespace = "from-file"\n')
    seen: list[Any] = []

    def fake_clean(ns: str | None = None, **k: Any) -> list[str]:
        seen.append(ns)
        return []

    monkeypatch.setattr(cleaning, "clean", fake_clean)
    cli.run(["clean"])
    assert seen == ["from-file"]


# --- config --------------------------------------------------------------------------------------


def test_config_dry_run_prints_plan_and_applies_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    core, _, rbac = _fakes(monkeypatch)
    monkeypatch.setattr(profile_mod, "apply", lambda *a, **k: pytest.fail("apply called"))
    assert cli.run(["config", "--namespace", NS, "--store", "k8s", "--dry-run"]) == 0
    expected = profile_mod.plan(profile_mod.profile_for("local", namespace=NS, store="k8s"))
    assert capsys.readouterr().out.splitlines() == expected
    assert core.actions == [] and rbac.actions == []
    assert not env.exists()


def test_config_applies_then_writes_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    core, _, rbac = _fakes(monkeypatch)
    assert cli.run(["config", "--namespace", NS, "--store", "local:/tmp/x"]) == 0
    assert ("create", "Namespace", NS) in core.actions
    assert ("create", "Role", "sk8s") in rbac.actions
    assert tomllib.loads(env.read_text()) == {
        "namespace": NS, "store": "local:/tmp/x", "image_prefix": "",
    }
    out = capsys.readouterr().out
    assert "created" in out and str(env) in out


def test_config_honours_sk8s_config_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path, tmp_path: Path
) -> None:
    _fakes(monkeypatch)
    alt = tmp_path / "alt" / "c.toml"
    monkeypatch.setenv("SK8S_CONFIG", str(alt))
    assert cli.run(["config", "--namespace", NS, "--store", "local:/tmp/x"]) == 0
    assert tomllib.loads(alt.read_text())["namespace"] == NS
    assert not env.exists()  # the default file is untouched
    assert str(alt) in capsys.readouterr().out
    # and the same file is what the commands read back
    assert cli._load_cfg().namespace == NS


def test_config_second_run_converges(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    core, _, rbac = _fakes(monkeypatch)
    cli.run(["config", "--namespace", NS])
    n_core, n_rbac = len(core.actions), len(rbac.actions)
    first = env.read_text()
    cli.run(["config", "--namespace", NS])
    assert (len(core.actions), len(rbac.actions)) == (n_core, n_rbac)  # no further writes
    assert env.read_text() == first


def test_config_defaults_namespace_to_sk8s_ignoring_context_and_reads_store_from_config(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    _fakes(monkeypatch)
    cli.run(["config"])  # the kube context says 'ctx-ns'; config still targets the dedicated ns
    data = tomllib.loads(env.read_text())
    assert data["namespace"] == "sk8s" and data["store"] == "k8s"


def test_config_dry_run_without_namespace_plans_sk8s_namespace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    core, _, rbac = _fakes(monkeypatch)
    assert cli.run(["config", "--dry-run"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "ensure Namespace sk8s"
    assert "default" not in out[0] and "ctx-ns" not in "".join(out)
    assert core.actions == [] and rbac.actions == [] and not env.exists()


def test_config_reuses_already_configured_namespace(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    _fakes(monkeypatch)
    env.parent.mkdir(parents=True)
    env.write_text('namespace = "team-b"\n')
    cli.run(["config"])
    assert tomllib.loads(env.read_text())["namespace"] == "team-b"


def test_config_help_documents_sk8s_default(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.run(["config", "--help"])
    assert "default: sk8s" in " ".join(capsys.readouterr().out.split())


def test_doctor_without_config_uses_context_namespace_then_sk8s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _doctor_with(monkeypatch, [Check("x", True)])
    cli.run(["doctor"])
    assert seen[-1].namespace == "ctx-ns"  # context explicitly sets one
    monkeypatch.setattr(k8s, "context_namespace", lambda: None)
    cli.run(["doctor"])
    assert seen[-1].namespace == "sk8s"  # never "default"


def test_config_provider_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    _fakes(monkeypatch)
    seen: list[tuple[str, dict[str, Any]]] = []
    real = profile_mod.profile_for

    def spy(provider: str, **kw: Any) -> Any:
        seen.append((provider, kw))
        return real(provider, **kw)

    monkeypatch.setattr(profile_mod, "profile_for", spy)
    cli.run(["config", "--dry-run", "--provider", "generic", "--namespace", NS])
    cli.run(["config", "--dry-run", "--namespace", NS])  # detected -> local (fixture)
    assert [p for p, _ in seen] == ["generic", "local"]


def test_config_unknown_provider_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    _fakes(monkeypatch)
    assert cli.run(["config", "--provider", "eks", "--namespace", NS]) == 1
    err = capsys.readouterr().err
    assert "eks" in err and "tabled" in err and "Traceback" not in err
    assert not env.exists()


# --- config --provider gke (cloud plan) --------------------------------------------------------

PROJ, REGION, BUCKET = "my-proj", "us-central1", "my-bucket"
STORE = f"gs://{BUCKET}/sk8s/"
EMAIL = f"sk8s-worker@{PROJ}.iam.gserviceaccount.com"
WI_ANN = "iam.gke.io/gcp-service-account"
GKE = ["config", "--provider", "gke", "--namespace", NS, "--store", STORE,
       "--project", PROJ, "--region", REGION]

DESCRIBE = ("gcloud", "iam", "service-accounts", "describe", EMAIL, "--project", PROJ)
CREATE = ("gcloud", "iam", "service-accounts", "create", "sk8s-worker",
          "--project", PROJ, "--display-name", "sk8s worker")
BUCKET_BIND = ("gcloud", "storage", "buckets", "add-iam-policy-binding", f"gs://{BUCKET}",
               "--member", f"serviceAccount:{EMAIL}", "--role", "roles/storage.objectAdmin",
               "--project", PROJ)
WI_BIND = ("gcloud", "iam", "service-accounts", "add-iam-policy-binding", EMAIL,
           "--role", "roles/iam.workloadIdentityUser",
           "--member", f"serviceAccount:{PROJ}.svc.id.goog[{NS}/sk8s]", "--project", PROJ)


class Runner:
    """Stand-in for cli._run_argv: records (argv, quiet), exit codes by argv prefix."""

    def __init__(self, codes: dict[tuple[str, ...], int] | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], bool]] = []
        self.codes = codes or {}

    def __call__(self, argv: Any, *, quiet: bool = False) -> int:
        self.calls.append((tuple(argv), quiet))
        return self.codes.get(tuple(argv), 0)

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [a for a, _ in self.calls]


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> Runner:
    r = Runner()
    monkeypatch.setattr(cli, "_run_argv", r)
    return r


def test_gke_dry_run_prints_the_k8s_plan_and_the_exact_cloud_plan_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path, runner: Runner
) -> None:
    core, _, rbac = _fakes(monkeypatch)
    monkeypatch.setattr(profile_mod, "apply", lambda *a, **k: pytest.fail("apply called"))
    assert cli.run([*GKE, "--gsa", "sk8s-worker", "--dry-run"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert f"annotate ServiceAccount sk8s in {NS}: {WI_ANN}={EMAIL}" in lines
    import shlex

    for argv in (CREATE, BUCKET_BIND, WI_BIND):  # printed as the exact argv, shell-quoted
        assert shlex.join(argv) in lines
    assert "# note: Artifact Registry repo us-central1-docker.pkg.dev/my-proj/sk8s/" in out
    assert "roles/artifactregistry.reader" in out
    assert "eventually consistent" in out and "wait ~30s" in out
    # ordering: the GSA exists before it is bound
    assert lines.index(shlex.join(CREATE)) < lines.index(shlex.join(BUCKET_BIND)) < lines.index(
        shlex.join(WI_BIND)
    )
    assert runner.calls == [] and core.actions == [] and rbac.actions == [] and not env.exists()


def test_gke_without_apply_cloud_prints_the_plan_applies_k8s_and_runs_no_gcloud(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path, runner: Runner
) -> None:
    core, _, rbac = _fakes(monkeypatch)
    assert cli.run([*GKE, "--gsa", EMAIL]) == 0
    out = capsys.readouterr().out
    assert "not run" in out and "--apply-cloud" in out and "service-accounts create" in out
    assert runner.calls == []
    sa = core.service_accounts[(NS, "sk8s")]
    assert sa.metadata.annotations == {WI_ANN: EMAIL}
    assert tomllib.loads(env.read_text()) == {
        "namespace": NS, "store": STORE, "image_prefix": "us-central1-docker.pkg.dev/my-proj/sk8s/",
        "rwx_storage_class": "standard-rwx",
    }


def test_gke_apply_cloud_runs_each_group_as_argv_in_order_then_applies_k8s(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path, runner: Runner
) -> None:
    core, _, rbac = _fakes(monkeypatch)
    runner.codes[DESCRIBE] = 1  # GSA does not exist yet
    assert cli.run([*GKE, "--gsa", "sk8s-worker", "--apply-cloud"]) == 0
    assert runner.calls == [(DESCRIBE, True), (CREATE, False), (BUCKET_BIND, False), (WI_BIND, False)]
    assert all(isinstance(a, str) for argv in runner.argvs for a in argv)
    assert core.service_accounts[(NS, "sk8s")].metadata.annotations == {WI_ANN: EMAIL}
    assert env.exists()
    assert "note:" in capsys.readouterr().out  # the AR note is still shown, never executed


def test_gke_apply_cloud_skips_creating_an_existing_gsa_but_still_binds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner: Runner
) -> None:
    _fakes(monkeypatch)
    runner.codes[DESCRIBE] = 0
    assert cli.run([*GKE, "--gsa", EMAIL, "--apply-cloud"]) == 0
    assert runner.argvs == [DESCRIBE, BUCKET_BIND, WI_BIND]
    assert "already done" in capsys.readouterr().out


def test_gke_apply_cloud_failure_is_exit_1_and_applies_nothing_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path, runner: Runner
) -> None:
    core, _, rbac = _fakes(monkeypatch)
    runner.codes[DESCRIBE] = 1
    runner.codes[BUCKET_BIND] = 1
    assert cli.run([*GKE, "--gsa", EMAIL, "--apply-cloud"]) == 1
    err = capsys.readouterr().err
    assert "gcloud step failed" in err and "buckets add-iam-policy-binding" in err
    assert "Traceback" not in err
    assert runner.argvs == [DESCRIBE, CREATE, BUCKET_BIND]  # stopped at the failure
    assert core.actions == [] and rbac.actions == [] and not env.exists()


def test_gke_bucket_role_is_configurable(monkeypatch: pytest.MonkeyPatch, runner: Runner) -> None:
    _fakes(monkeypatch)
    cli.run([*GKE, "--gsa", EMAIL, "--apply-cloud", "--bucket-role", "roles/storage.objectViewer"])
    bind = next(a for a in runner.argvs if "buckets" in a)
    assert bind[bind.index("--role") + 1] == "roles/storage.objectViewer"


def test_gke_non_gs_store_plans_no_bucket_binding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner: Runner
) -> None:
    _fakes(monkeypatch)
    i = GKE.index("--store")
    cli.run([*GKE[:i], "--store", "k8s", *GKE[i + 2:], "--gsa", EMAIL, "--dry-run"])
    out = capsys.readouterr().out
    assert "buckets" not in out and "is not gs://" in out


def test_gke_without_gsa_has_no_gcloud_commands_and_no_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner: Runner
) -> None:
    core, _, _ = _fakes(monkeypatch)
    assert cli.run(GKE) == 0
    assert runner.calls == [] and "gcloud iam" not in capsys.readouterr().out
    assert not core.service_accounts[(NS, "sk8s")].metadata.annotations


def test_gke_repo_flag_flows_to_the_image_prefix(
    monkeypatch: pytest.MonkeyPatch, env: Path, runner: Runner
) -> None:
    _fakes(monkeypatch)
    assert cli.run([*GKE, "--repo", "images"]) == 0
    assert tomllib.loads(env.read_text())["image_prefix"].endswith("/my-proj/images/")


def test_gke_second_apply_converges(monkeypatch: pytest.MonkeyPatch, runner: Runner) -> None:
    core, _, rbac = _fakes(monkeypatch)
    cli.run([*GKE, "--gsa", EMAIL])
    n = (len(core.actions), len(rbac.actions))
    cli.run([*GKE, "--gsa", EMAIL])
    assert (len(core.actions), len(rbac.actions)) == n


@pytest.mark.parametrize("drop", ["--project", "--region"])
def test_gke_requires_project_and_region_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], drop: str, runner: Runner
) -> None:
    _fakes(monkeypatch)
    i = GKE.index(drop)
    assert cli.run(GKE[:i] + GKE[i + 2:]) == 1
    err = capsys.readouterr().err
    assert "--project and --region" in err and "Traceback" not in err


def test_gke_infers_project_and_region_from_the_context_name(
    monkeypatch: pytest.MonkeyPatch, env: Path, runner: Runner
) -> None:
    core, _, _ = _fakes(monkeypatch)
    monkeypatch.setattr(profile_mod, "active_context", lambda: "gke_ctx-proj_europe-west4-b_c1")
    assert cli.run(["config", "--provider", "gke", "--namespace", NS, "--store", STORE,
                    "--gsa", "sk8s-worker", "--apply-cloud"]) == 0
    assert tomllib.loads(env.read_text())["image_prefix"] == (
        "europe-west4-docker.pkg.dev/ctx-proj/sk8s/"  # zone -> region
    )
    gsa = "sk8s-worker@ctx-proj.iam.gserviceaccount.com"
    assert core.service_accounts[(NS, "sk8s")].metadata.annotations == {WI_ANN: gsa}
    assert ("gcloud", "iam", "service-accounts", "describe", gsa, "--project", "ctx-proj") in runner.argvs


def test_gke_flags_override_the_context_name(
    monkeypatch: pytest.MonkeyPatch, env: Path, runner: Runner
) -> None:
    _fakes(monkeypatch)
    monkeypatch.setattr(profile_mod, "active_context", lambda: "gke_ctx-proj_europe-west4_c1")
    assert cli.run(GKE) == 0  # --project my-proj --region us-central1
    assert tomllib.loads(env.read_text())["image_prefix"] == (
        "us-central1-docker.pkg.dev/my-proj/sk8s/"
    )


def test_gke_one_flag_plus_context_fills_the_other(
    monkeypatch: pytest.MonkeyPatch, env: Path, runner: Runner
) -> None:
    _fakes(monkeypatch)
    monkeypatch.setattr(profile_mod, "active_context", lambda: "gke_ctx-proj_europe-west4_c1")
    assert cli.run(["config", "--provider", "gke", "--namespace", NS, "--project", "mine"]) == 0
    assert tomllib.loads(env.read_text())["image_prefix"] == (
        "europe-west4-docker.pkg.dev/mine/sk8s/"
    )


def test_gke_on_a_non_gke_context_without_flags_errors_citing_the_context_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner: Runner
) -> None:
    _fakes(monkeypatch)
    monkeypatch.setattr(profile_mod, "active_context", lambda: "minikube")
    assert cli.run(["config", "--provider", "gke", "--namespace", NS]) == 1
    err = capsys.readouterr().err
    assert "gke_<project>_<location>_<name>" in err and "--project and --region" in err


def test_gke_apply_cloud_requires_gsa(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner: Runner
) -> None:
    _fakes(monkeypatch)
    assert cli.run([*GKE, "--apply-cloud"]) == 1
    assert "--gsa" in capsys.readouterr().err and runner.calls == []


@pytest.mark.parametrize("flag", [["--project", "p"], ["--gsa", "g"], ["--apply-cloud"], ["--repo", "r"]])
def test_gke_options_are_rejected_for_other_providers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: list[str],
    runner: Runner,
) -> None:
    _fakes(monkeypatch)
    assert cli.run(["config", "--provider", "generic", "--namespace", NS, *flag]) == 1
    assert "GKE options" in capsys.readouterr().err and runner.calls == []


def test_gke_is_used_when_detected_from_the_context(
    monkeypatch: pytest.MonkeyPatch, runner: Runner
) -> None:
    core, _, _ = _fakes(monkeypatch)
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "gke")
    assert cli.run(["config", "--namespace", NS, "--project", PROJ, "--region", REGION,
                    "--gsa", EMAIL]) == 0
    assert core.service_accounts[(NS, "sk8s")].metadata.annotations == {WI_ANN: EMAIL}


def test_config_help_documents_the_gke_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.run(["config", "--help"])
    out = capsys.readouterr().out
    for flag in ("--project", "--region", "--repo", "--gsa", "--bucket-role", "--apply-cloud"):
        assert flag in out


def test_gcloud_helpers() -> None:
    assert cli.gsa_email("w", "p") == "w@p.iam.gserviceaccount.com"
    assert cli.gsa_email("w@x.iam.gserviceaccount.com", "p") == "w@x.iam.gserviceaccount.com"
    assert cli.bucket_of("gs://b/p/") == "b" and cli.bucket_of("gs://b") == "b"
    assert cli.bucket_of("s3://b/p") is None and cli.bucket_of("gs://") is None


def test_run_argv_is_argv_only_never_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: Any, **kw: Any) -> Any:
        seen["argv"], seen["kw"] = argv, kw
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._run_argv(["gcloud", "a; rm -rf /", "b"]) == 3
    assert seen["argv"] == ["gcloud", "a; rm -rf /", "b"]
    assert not seen["kw"].get("shell") and seen["kw"]["stdout"] is None
    cli._run_argv(["gcloud", "x"], quiet=True)
    assert seen["kw"]["stdout"] is subprocess.DEVNULL and seen["kw"]["stderr"] is subprocess.DEVNULL


def test_run_argv_missing_gcloud_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("gcloud")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(Sk8sError, match="gcloud not found"):
        cli._run_argv(["gcloud", "x"])


def test_config_apply_failure_writes_no_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    def boom(*a: Any, **k: Any) -> list[str]:
        raise ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(profile_mod, "apply", boom)
    assert cli.run(["config", "--namespace", NS]) == 1
    assert not env.exists()
    assert "403" in capsys.readouterr().err


def test_config_preserves_other_keys(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    _fakes(monkeypatch)
    env.parent.mkdir(parents=True)
    env.write_text(
        'image = "me/img:1"\nretention_days = 3\nkeep_results = true\n'
        'namespace = "old"\n\n[store_env]\nAWS_KEY = "abc"\n'
    )
    cli.run(["config", "--namespace", NS])
    data = tomllib.loads(env.read_text())
    assert data["namespace"] == NS
    assert data["image"] == "me/img:1" and data["retention_days"] == 3
    assert data["keep_results"] is True and data["store_env"] == {"AWS_KEY": "abc"}


def test_config_refuses_to_clobber_unparseable_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    _fakes(monkeypatch)
    env.parent.mkdir(parents=True)
    env.write_text("this is = = not toml")
    # config loading itself fails first: a clear error, not a traceback
    assert cli.run(["config", "--namespace", NS]) == 1
    assert env.read_text() == "this is = = not toml"
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("value", [
    "plain", "", 'quo"te', "back\\slash", "new\nline", "tab\there", "bell\x07", "del\x7f",
    "uni\u00e9\u4e2d", "gs://bucket/pre fix", "C:\\path\\n",
])
def test_toml_string_roundtrips(value: str) -> None:
    assert tomllib.loads(f"k = {cli.toml_string(value)}")["k"] == value


def test_write_config_escapes_values(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    nasty = 'a"b\\c\nd'
    cli.write_config(path, {"namespace": nasty, "store": "k8s", "image_prefix": "reg/"})
    assert tomllib.loads(path.read_text()) == {
        "namespace": nasty, "store": "k8s", "image_prefix": "reg/",
    }


def test_write_config_none_removes_a_key_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    cli.write_config(path, {"namespace": "n", "rwx_storage_class": "nfs"})
    cli.write_config(path, {"store": "k8s", "rwx_storage_class": None})
    assert tomllib.loads(path.read_text()) == {"namespace": "n", "store": "k8s"}
    cli.write_config(path, {"rwx_storage_class": None})  # absent key: still fine
    assert "rwx_storage_class" not in tomllib.loads(path.read_text())


# --- config: the rwx storage class ----------------------------------------------------------------

RWX = ["config", "--provider", "generic", "--namespace", NS, "--rwx-storage-class", "nfs"]


def test_config_persists_the_generic_profiles_rwx_class_and_creates_it_when_asked(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    _fakes(monkeypatch)
    storage = _storage(monkeypatch)
    assert cli.run([*RWX, "--rwx-provisioner", "example.com/nfs"]) == 0
    assert tomllib.loads(env.read_text())["rwx_storage_class"] == "nfs"
    assert storage.created["nfs"].provisioner == "example.com/nfs"
    assert cli._load_cfg().rwx_storage_class == "nfs"


def test_config_second_run_without_flags_keeps_the_class_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    _fakes(monkeypatch)
    storage = _storage(monkeypatch)
    cli.run([*RWX, "--rwx-provisioner", "example.com/nfs"])
    before = list(storage.actions)
    assert cli.run(["config", "--provider", "generic", "--namespace", NS]) == 0
    assert tomllib.loads(env.read_text())["rwx_storage_class"] == "nfs"  # carried over
    assert storage.actions == before


def test_config_dry_run_plans_the_storage_class_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    _fakes(monkeypatch)
    storage = _storage(monkeypatch)
    assert cli.run([*RWX, "--rwx-provisioner", "example.com/nfs", "--dry-run"]) == 0
    assert "ensure StorageClass nfs (provisioner example.com/nfs)" in capsys.readouterr().out
    assert storage.actions == [] and not env.exists()


def test_config_without_a_provisioner_names_the_class_but_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    _fakes(monkeypatch)
    storage = _storage(monkeypatch)
    monkeypatch.setattr(k8s, "storage_api", lambda: pytest.fail("storage client built"))
    assert cli.run(RWX) == 0
    assert tomllib.loads(env.read_text())["rwx_storage_class"] == "nfs"
    assert storage.actions == []


def test_gke_config_persists_the_derived_standard_rwx(
    monkeypatch: pytest.MonkeyPatch, env: Path, runner: Runner
) -> None:
    _fakes(monkeypatch)
    monkeypatch.setattr(k8s, "storage_api", lambda: pytest.fail("GKE ships standard-rwx"))
    assert cli.run(GKE) == 0
    assert tomllib.loads(env.read_text())["rwx_storage_class"] == "standard-rwx"


def test_local_config_has_no_rwx_key_and_drops_a_stale_one(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    _fakes(monkeypatch)
    env.parent.mkdir(parents=True)
    env.write_text('rwx_storage_class = "stale"\n')
    assert cli.run(["config", "--namespace", NS]) == 0  # detected -> local (fixture)
    assert "rwx_storage_class" not in tomllib.loads(env.read_text())


@pytest.mark.parametrize("provider", ["local", "gke"])
def test_rwx_flags_are_rejected_where_the_provider_owns_the_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path,
    provider: str, runner: Runner,
) -> None:
    _fakes(monkeypatch)
    argv = ["config", "--provider", provider, "--namespace", NS, "--rwx-storage-class", "nfs"]
    if provider == "gke":
        argv += ["--project", PROJ, "--region", REGION]
    assert cli.run(argv) == 1
    expected = "has no RWX storage class" if provider == "local" else "do not pass them"
    assert expected in capsys.readouterr().err
    assert not env.exists()


def test_config_help_documents_the_rwx_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.run(["config", "--help"])
    out = capsys.readouterr().out
    assert "--rwx-storage-class" in out and "--rwx-provisioner" in out


def test_doctor_checks_the_persisted_rwx_class_on_a_generic_cluster(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "generic")
    env.parent.mkdir(parents=True)
    env.write_text('rwx_storage_class = "nfs"\n')
    seen = _doctor_with(monkeypatch, [Check("x", True)])
    assert cli.run(["doctor"]) == 0
    assert seen[0].rwx_storage_class == "nfs"


def test_doctor_on_local_ignores_a_stale_rwx_class(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    env.parent.mkdir(parents=True)
    env.write_text('rwx_storage_class = "nfs"\n')
    seen = _doctor_with(monkeypatch, [Check("x", True)])
    assert cli.run(["doctor"]) == 0
    assert seen[0].rwx_storage_class is None


# --- doctor --------------------------------------------------------------------------------------


def _doctor_with(monkeypatch: pytest.MonkeyPatch, checks: list[Check]) -> list[Any]:
    seen: list[Any] = []

    def fake(profile: Any, **kw: Any) -> list[Check]:
        seen.append(profile)
        return checks

    monkeypatch.setattr(doctor_mod, "doctor", fake)
    return seen


def test_doctor_all_pass_exit_0(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen = _doctor_with(monkeypatch, [Check("cluster reachable", True), Check("store writable", True)])
    assert cli.run(["doctor"]) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") == 2 and "FAIL" not in out
    assert seen[0].namespace == "ctx-ns" and seen[0].store == "k8s"


def test_doctor_failure_exit_1_with_aligned_hints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _doctor_with(monkeypatch, [
        Check("cluster reachable", True),
        Check("role permissions", False, "run: sk8s config"),
    ])
    assert cli.run(["doctor"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "PASS  cluster reachable"
    assert lines[1] == "FAIL  role permissions"
    assert "run: sk8s config" in lines[2] and lines[2].startswith(" ")


def test_doctor_uses_configured_namespace_and_store(monkeypatch: pytest.MonkeyPatch, env: Path) -> None:
    env.parent.mkdir(parents=True)
    env.write_text('namespace = "cfg-ns"\nstore = "local:/s"\nimage_prefix = "reg/"\n')
    seen = _doctor_with(monkeypatch, [Check("x", True)])
    cli.run(["doctor"])
    assert (seen[0].namespace, seen[0].store, seen[0].image_prefix) == ("cfg-ns", "local:/s", "reg/")


def test_doctor_on_a_gke_context_builds_the_gke_profile_from_the_context_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "gke")
    monkeypatch.setattr(profile_mod, "active_context", lambda: "gke_my-proj_us-central1-a_c1")
    seen = _doctor_with(monkeypatch, [Check("x", True)])
    assert cli.run(["doctor"]) == 0
    assert seen[0].rwx_storage_class == "standard-rwx"
    assert seen[0].image_prefix == "us-central1-docker.pkg.dev/my-proj/sk8s/"


def test_doctor_on_gke_keeps_the_persisted_image_prefix(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    env.parent.mkdir(parents=True)
    env.write_text('image_prefix = "us-central1-docker.pkg.dev/my-proj/foo/"\n')
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "gke")
    monkeypatch.setattr(profile_mod, "active_context", lambda: "gke_my-proj_us-central1_c1")
    seen = _doctor_with(monkeypatch, [Check("x", True)])
    assert cli.run(["doctor"]) == 0
    assert seen[0].image_prefix == "us-central1-docker.pkg.dev/my-proj/foo/"
    assert seen[0].rwx_storage_class == "standard-rwx"


def test_doctor_does_not_crash_when_gke_context_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "gke")
    monkeypatch.setattr(profile_mod, "active_context", lambda: None)
    seen = _doctor_with(monkeypatch, [Check("x", True)])
    assert cli.run(["doctor"]) == 0
    assert seen[0].rwx_storage_class is None


def test_doctor_builds_store_with_configured_endpoint_and_injects_it(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    _fakes(monkeypatch)
    env.parent.mkdir(parents=True)
    env.write_text(
        'namespace = "cfg-ns"\nstore = "s3://bkt/pfx"\nstore_endpoint = "http://minio:9000"\n'
    )
    sentinel = object()
    built: list[tuple[str, dict[str, Any]]] = []

    def fake_get_store(store: str, **kw: Any) -> Any:
        built.append((store, kw))
        return sentinel

    monkeypatch.setattr(cli, "get_store", fake_get_store)
    injected: list[dict[str, Any]] = []

    def fake_doctor(profile: Any, **kw: Any) -> list[Check]:
        injected.append(kw)
        return [Check("x", True)]

    monkeypatch.setattr(doctor_mod, "doctor", fake_doctor)
    assert cli.run(["doctor"]) == 0
    ((store, kw),) = built
    assert store == "s3://bkt/pfx"
    assert kw["endpoint"] == "http://minio:9000" and kw["namespace"] == "cfg-ns"
    assert injected[0]["store"] is sentinel


def test_doctor_real_checks_against_converged_fakes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, env: Path
) -> None:
    _fakes(monkeypatch)
    env.parent.mkdir(parents=True)
    env.write_text(f'namespace = "{NS}"\nstore = "local:{tmp_path / "blobs"}"\n')
    assert cli.run(["doctor"]) == 1  # nothing applied yet
    assert "FAIL  namespace exists" in capsys.readouterr().out
    assert cli.run(["config"]) == 0
    capsys.readouterr()
    assert cli.run(["doctor"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_namespace_failure_hint_does_not_mention_a_namespace_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kubernetes.config import ConfigException

    from sk8s.config import Config

    def boom() -> str:
        raise ConfigException("no kubeconfig")

    monkeypatch.setattr(k8s, "current_namespace", boom)
    with pytest.raises(Sk8sError) as e:
        cli._namespace(None, Config())
    assert "--namespace" not in str(e.value)
    assert "sk8s config" in str(e.value) and "SK8S_NAMESPACE" in str(e.value)


# --- workflows / logs ----------------------------------------------------------------------------


def _anchor(core: FakeCoreV1, wf: str, *, age: datetime.timedelta, driver: str = "", detached: bool = True) -> None:
    cm = anchors.create_anchor(
        core, namespace="ctx-ns", workflow_id=wf, store="k8s", store_prefix=wf,
        retention_days=7, detached=detached,
    )
    cm.metadata.annotations[anchors.CREATED_AT] = (
        datetime.datetime.now(datetime.timezone.utc) - age
    ).isoformat()
    cm.metadata.annotations[anchors.DRIVER_JOB] = driver


def _job(name: str, **status: Any) -> client.V1Job:
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=name, namespace="ctx-ns"),
        spec=client.V1JobSpec(
            backoff_limit=0, template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[]))
        ),
        status=client.V1JobStatus(**status),
    )


def test_workflows_table(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    core, batch, _ = _fakes(monkeypatch)
    _anchor(core, "wf-run", age=datetime.timedelta(hours=3), driver="drv-1")
    _anchor(core, "wf-done", age=datetime.timedelta(days=2), driver="drv-2")
    _anchor(core, "wf-nodrv", age=datetime.timedelta(minutes=5), driver="", detached=False)
    _anchor(core, "wf-gone", age=datetime.timedelta(seconds=30), driver="drv-reaped")
    batch.create_namespaced_job("ctx-ns", _job("drv-1", active=1))
    batch.create_namespaced_job("ctx-ns", _job("drv-2", succeeded=1))
    # a non-anchor configmap must not be listed
    core.create_namespaced_config_map("ctx-ns", client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name="other", labels={"sk8s/kind": "service"})))
    assert cli.run(["workflows"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["ID", "DETACHED", "AGE", "DRIVER"]
    rows = {r.split()[0]: r.split() for r in lines[1:]}
    assert set(rows) == {"wf-run", "wf-done", "wf-nodrv", "wf-gone"}
    assert rows["wf-run"] == ["wf-run", "yes", "3h", "active"]
    assert rows["wf-done"] == ["wf-done", "yes", "2d", "succeeded"]
    assert rows["wf-nodrv"] == ["wf-nodrv", "no", "5m", "unknown"]
    assert rows["wf-gone"] == ["wf-gone", "yes", "30s", "unknown"]  # driver job reaped -> unknown
    assert [r.split()[0] for r in lines[1:]] == ["wf-done", "wf-run", "wf-nodrv", "wf-gone"]  # oldest first


def test_workflows_empty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fakes(monkeypatch)
    assert cli.run(["workflows"]) == 0
    assert "no workflows" in capsys.readouterr().out


def test_logs_reads_driver_pod_logs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    core, _, _ = _fakes(monkeypatch)
    _anchor(core, "wf-1", age=datetime.timedelta(minutes=10), driver="drv-9")
    selectors: list[str] = []

    def list_pods(namespace: str, label_selector: str = "") -> Any:
        selectors.append(label_selector)
        return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="drv-9-pod"))])

    core.list_namespaced_pod = list_pods  # type: ignore[method-assign]
    core.read_namespaced_pod_log = lambda name, namespace: f"log of {name} in {namespace}"  # type: ignore[method-assign]
    assert cli.run(["logs", "wf-1"]) == 0
    assert selectors == ["job-name=drv-9"]
    assert capsys.readouterr().out == "log of drv-9-pod in ctx-ns\n"


@pytest.mark.parametrize("driver,wf,fragment", [
    ("drv", "wf-nope", "unknown workflow"),
    ("", "wf-1", "no driver job"),
    ("drv", "wf-1", "no pods"),
])
def test_logs_failures_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    driver: str, wf: str, fragment: str,
) -> None:
    core, _, _ = _fakes(monkeypatch)
    _anchor(core, "wf-1", age=datetime.timedelta(minutes=10), driver=driver)
    assert cli.run(["logs", wf]) == 1
    err = capsys.readouterr().err
    assert fragment in err and "Traceback" not in err


# --- shell ---------------------------------------------------------------------------------------


def _pod(phase: str, waiting: str | None = None, name: str = "shell-pod") -> Any:
    states = []
    if waiting:
        states = [SimpleNamespace(state=SimpleNamespace(waiting=SimpleNamespace(reason=waiting, message="")))]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(phase=phase, container_statuses=states),
    )


def _shell_env(
    monkeypatch: pytest.MonkeyPatch, pods: list[Any]
) -> tuple[FakeBatchV1, list[list[str]]]:
    core, batch, _ = _fakes(monkeypatch)
    monkeypatch.setenv("SK8S_IMAGE", "me/worker:1")  # the worker image is build-your-own
    seq = iter(pods)
    last: list[Any] = [None]

    def list_pods(namespace: str, label_selector: str = "") -> Any:
        try:
            last[0] = next(seq)
        except StopIteration:
            pass
        return SimpleNamespace(items=[] if last[0] is None else [last[0]])

    core.list_namespaced_pod = list_pods  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "_which", lambda name: "/usr/bin/kubectl")
    monkeypatch.setattr(cli, "_sleep", lambda s: None)
    execs: list[list[str]] = []
    monkeypatch.setattr(cli, "_execvp", lambda f, argv: execs.append([f, *argv]))
    return batch, execs


def test_shell_creates_job_waits_then_execs_kubectl(monkeypatch: pytest.MonkeyPatch) -> None:
    batch, execs = _shell_env(monkeypatch, [_pod("Pending"), _pod("Running")])
    assert cli.run(["shell", "--image", "me/img:2"]) == 0
    ((ns, name), job), = batch.jobs.items()
    assert ns == "ctx-ns" and name.startswith("sk8s-shell-")
    container = job.spec.template.spec.containers[0]
    assert container.image == "me/img:2"
    assert container.command[:2] == ["sh", "-c"]  # no python needed in the image
    assert "sleep" in container.command[2] and "trap" in container.command[2]  # SIGTERM is honoured
    assert job.spec.active_deadline_seconds and job.spec.ttl_seconds_after_finished is not None
    assert execs == [["kubectl", "kubectl", "exec", "-it", "-n", "ctx-ns", "shell-pod", "--", "sh"]]
    assert batch.deleted == []  # kept for the session


def test_shell_prints_progress_and_early_cleanup_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    batch, _ = _shell_env(monkeypatch, [_pod("Pending"), _pod("Running")])
    assert cli.run(["shell"]) == 0
    ((_, name),) = batch.jobs
    lines = capsys.readouterr().out.splitlines()
    starting = f"starting shell pod (job {name}) in namespace ctx-ns..."
    hint = f"job {name} is removed after 4h; delete earlier with: kubectl delete job {name} -n ctx-ns"
    assert lines == [starting, hint]


def test_shell_default_image_is_worker_image(monkeypatch: pytest.MonkeyPatch) -> None:
    batch, _ = _shell_env(monkeypatch, [_pod("Running")])
    cli.run(["shell"])
    (job,) = batch.jobs.values()
    assert job.spec.template.spec.containers[0].image == "me/worker:1"


def test_shell_with_no_image_anywhere_fails_fast_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    batch, execs = _shell_env(monkeypatch, [_pod("Running")])
    monkeypatch.delenv("SK8S_IMAGE")
    monkeypatch.setenv("SK8S_CONFIG", str(tmp_path / "absent.toml"))
    assert cli.run(["shell"]) == 1
    assert "Dockerfile.worker" in capsys.readouterr().err
    assert batch.jobs == {} and execs == []
    assert cli.run(["shell", "--image", "alpine"]) == 0  # an explicit image still works


def test_shell_without_kubectl_exits_1_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    batch, execs = _shell_env(monkeypatch, [_pod("Running")])
    monkeypatch.setattr(cli, "_which", lambda name: None)
    assert cli.run(["shell"]) == 1
    assert "kubectl" in capsys.readouterr().err
    assert batch.jobs == {} and execs == []


def test_shell_pod_failure_deletes_job_and_does_not_exec(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    batch, execs = _shell_env(monkeypatch, [_pod("Pending", waiting="ImagePullBackOff")])
    assert cli.run(["shell", "--image", "nope"]) == 1
    assert "ImagePullBackOff" in capsys.readouterr().err
    assert len(batch.deleted) == 1 and batch.jobs == {} and execs == []


def test_shell_err_image_never_pull_fails_fast(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same bug class as cluster.py's ErrImageNeverPull handling: must not wait out the timeout.
    batch, execs = _shell_env(monkeypatch, [_pod("Pending", waiting="ErrImageNeverPull")])
    assert cli.run(["shell"]) == 1
    assert "ErrImageNeverPull" in capsys.readouterr().err
    assert len(batch.deleted) == 1 and batch.jobs == {} and execs == []


def test_shell_timeout_deletes_job(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    batch, execs = _shell_env(monkeypatch, [_pod("Pending")])
    ticks = iter(range(0, 10_000, 50))
    monkeypatch.setattr(cli, "_clock", lambda: float(next(ticks)))
    assert cli.run(["shell"]) == 1
    assert "did not become Running" in capsys.readouterr().err
    assert len(batch.deleted) == 1 and execs == []


def test_shell_exec_failure_deletes_job(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    batch, _ = _shell_env(monkeypatch, [_pod("Running")])

    def bad_exec(f: str, argv: list[str]) -> None:
        raise FileNotFoundError("kubectl vanished")

    monkeypatch.setattr(cli, "_execvp", bad_exec)
    assert cli.run(["shell"]) == 1
    assert len(batch.deleted) == 1


# --- subprocess smokes (the real entrypoint) -----------------------------------------------------


def _sk8s() -> list[str]:
    exe = Path(sys.executable).parent / "sk8s"
    if exe.exists():
        return [str(exe)]
    return [sys.executable, "-c", "import sys; from sk8s.cli import main; sys.exit(main())"]


def _clean_env(tmp_path: Path) -> dict[str, str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("SK8S_")}
    e["HOME"] = str(tmp_path)
    return e


def test_subprocess_version(tmp_path: Path) -> None:
    p = subprocess.run([*_sk8s(), "--version"], capture_output=True, text=True, env=_clean_env(tmp_path))
    assert p.returncode == 0 and p.stdout.startswith("sk8s ") and p.stderr == ""


@pytest.mark.parametrize("argv", [["bogus"], ["clean", "--older-than", "garbage"], []])
def test_subprocess_usage_error_exit_2_no_traceback(tmp_path: Path, argv: list[str]) -> None:
    p = subprocess.run([*_sk8s(), *argv], capture_output=True, text=True, env=_clean_env(tmp_path))
    assert p.returncode == 2
    assert "usage: sk8s" in p.stderr and "Traceback" not in p.stderr


_DOCTOR_SCRIPT = """
import sys
import sk8s.doctor, sk8s.k8s
from sk8s.doctor import Check
sk8s.k8s.current_namespace = lambda: "ns"
sk8s.doctor.doctor = lambda profile, **kw: [Check("a", True), Check("b", {ok}, "do the thing")]
from sk8s.cli import main
sys.argv = ["sk8s", "doctor"]
sys.exit(main())
"""


@pytest.mark.parametrize("ok,code", [(True, 0), (False, 1)])
def test_subprocess_doctor_exit_code(tmp_path: Path, ok: bool, code: int) -> None:
    p = subprocess.run(
        [sys.executable, "-c", _DOCTOR_SCRIPT.format(ok=ok)],
        capture_output=True, text=True, env=_clean_env(tmp_path),
    )
    assert p.returncode == code, p.stderr
    assert ("FAIL  b" in p.stdout) == (not ok) and "Traceback" not in p.stderr


def test_main_reads_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["sk8s", "image", "push", "--tag", "t"])
    pushed: list[str] = []
    monkeypatch.setattr(images, "push", lambda tag, **kw: pushed.append(tag))
    assert cli.main() == 0 and pushed == ["t"]
