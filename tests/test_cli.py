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
from tests.fakes import FakeBatchV1, FakeCoreV1, FakeRbacV1

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
    return path


def _fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCoreV1, FakeBatchV1, FakeRbacV1]:
    core, batch, rbac = FakeCoreV1(), FakeBatchV1(), FakeRbacV1()
    monkeypatch.setattr(k8s, "core_api", lambda: core)
    monkeypatch.setattr(k8s, "batch_api", lambda: batch)
    monkeypatch.setattr(k8s, "rbac_api", lambda: rbac)
    return core, batch, rbac


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
    ["image", "build", "--tag", "t", "--pip", "x", "--push", "--minikube"],
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

    seen: dict[str, Any] = {}

    def fake_minikube(tag: str, *, dockerfile: str | None = None, **kw: Any) -> str:
        seen["dockerfile"] = dockerfile
        return tag

    monkeypatch.setattr(images, "build_in_minikube", fake_minikube)
    assert cli.run(
        ["image", "build", "--tag", "t:1", "--base", "sk8s:dev", "--pip", "pysam", "--minikube"]
    ) == 0
    assert seen["dockerfile"].startswith("FROM sk8s:dev\n")

    built: list[dict[str, Any]] = []
    monkeypatch.setattr(images, "build", lambda tag, **kw: built.append(kw) or tag)
    assert cli.run(["image", "build", "--tag", "t:1", "--base", "b:1", "--pip", "x"]) == 0
    assert built[0]["base"] == "b:1"


def test_image_build_base_with_conda_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("docker ran"))
    for extra in ([], ["--minikube"]):
        rc = cli.run(["image", "build", "--tag", "t:1", "--base", "b:1", "--conda", "x", *extra])
        assert rc == 1
        assert "base cannot be combined with conda" in capsys.readouterr().err


def test_image_build_minikube_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_minikube(tag: str, *, dockerfile: str | None = None, **kw: Any) -> str:
        calls.append((tag, dockerfile))
        return tag

    monkeypatch.setattr(images, "build_in_minikube", fake_minikube)
    monkeypatch.setattr(images, "build", lambda *a, **k: pytest.fail("docker build used"))
    assert cli.run(["image", "build", "--tag", "t:1", "--pip", "numpy", "--minikube"]) == 0
    (tag, dockerfile), = calls
    assert tag == "t:1" and dockerfile is not None and "pip install" in dockerfile and "numpy" in dockerfile


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
    assert calls == [("ctx-ns", {"older_than_days": expected})]
    out = capsys.readouterr().out
    assert "wf-1" in out and "wf-2" in out


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
    assert cli.run(["config", "--provider", "gke", "--namespace", NS]) == 1
    err = capsys.readouterr().err
    assert "gke" in err and "tabled" in err and "Traceback" not in err
    assert not env.exists()


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
    assert job.spec.template.spec.containers[0].image.startswith("ghcr.io/jared-maguire/sk8s:")


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
