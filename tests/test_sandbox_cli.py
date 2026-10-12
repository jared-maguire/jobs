"""`sk8s sandbox ...`: create / list / delete through the CLI, against fakes."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from kubernetes import client

import sk8s.config as config_mod
import sk8s.k8s as k8s
import sk8s.profile as profile_mod
from sk8s import cli, sandbox
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1, FakeRbacV1

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key in list(os.environ):
        if key.startswith("SK8S_"):
            monkeypatch.delenv(key)
    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(f'store = "local:{tmp_path / "blobs"}"\nnamespace = "team-a"\n')
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg)
    monkeypatch.setattr(k8s, "context_namespace", lambda: "ctx-ns")
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "local")
    monkeypatch.setattr(profile_mod, "active_context", lambda: None)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> FakeCoreV1:
    core, batch, rbac = FakeCoreV1(), FakeBatchV1(), FakeRbacV1()
    monkeypatch.setattr(k8s, "core_api", lambda: core)
    monkeypatch.setattr(k8s, "batch_api", lambda: batch)
    monkeypatch.setattr(k8s, "rbac_api", lambda: rbac)
    return core


def test_parser_wires_sandbox_subcommands() -> None:
    p = cli.build_parser()
    a = p.parse_args(["sandbox", "create", "--name", "x", "--budget-usd", "2.5", "--ttl", "6h"])
    assert (a.name, a.budget_usd, a.ttl) == ("x", 2.5, "6h")
    assert p.parse_args(["sandbox", "create"]).ttl == "24h"
    assert p.parse_args(["sandbox", "list", "--json"]).json is True
    assert p.parse_args(["sandbox", "delete", "x"]).name == "x"
    with pytest.raises(SystemExit):
        p.parse_args(["sandbox"])
    with pytest.raises(SystemExit):
        p.parse_args(["sandbox", "create", "--budget-usd", "-1"])


def test_create_prints_the_export_line_and_writes_the_isolated_config(
    cluster: FakeCoreV1, env: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert cli.run(["sandbox", "create", "--name", "alpha", "--budget-usd", "1.5", "--ttl", "2h"]) == 0
    out = capsys.readouterr().out
    path = env / ".sk8s-sandbox-alpha.toml"
    assert f"export SK8S_CONFIG={path}" in out.splitlines()
    assert "Namespace sk8s-sbx-alpha: exists" in out  # the labelled namespace was created first
    data = tomllib.loads(path.read_text())
    assert data["namespace"] == "sk8s-sbx-alpha"
    assert data["store"] == f"local:{tmp_path / 'blobs'}/sbx-alpha"
    assert data["budget"]["max_estimated_usd"] == 1.5
    assert cluster.namespaces["sk8s-sbx-alpha"].metadata.labels["sk8s/kind"] == "sandbox"
    # the user's own config is untouched
    assert "sbx" not in config_mod.DEFAULT_CONFIG_PATH.read_text()


def test_create_json(cluster: FakeCoreV1, env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(["sandbox", "create", "--name", "alpha", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["name"] == "alpha" and got["namespace"] == "sk8s-sbx-alpha"
    assert got["config_path"] == str(env / ".sk8s-sandbox-alpha.toml")
    assert got["export"] == f"export SK8S_CONFIG={env / '.sk8s-sandbox-alpha.toml'}"
    assert got["expires_at"] and got["budget"] is None


def test_create_random_name_and_bad_inputs(
    cluster: FakeCoreV1, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(["sandbox", "create"]) == 0
    assert len([ns for ns in cluster.namespaces if ns.startswith("sk8s-sbx-")]) == 1
    capsys.readouterr()
    assert cli.run(["sandbox", "create", "--name", "Bad_Name"]) == 1
    assert "sandbox name" in capsys.readouterr().err
    assert cli.run(["sandbox", "create", "--name", "ok", "--ttl", "24"]) == 1
    assert "ttl" in capsys.readouterr().err
    assert "sk8s-sbx-ok" not in cluster.namespaces


def test_create_twice_is_an_error_not_a_clobber(
    cluster: FakeCoreV1, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(["sandbox", "create", "--name", "alpha"]) == 0
    assert cli.run(["sandbox", "create", "--name", "alpha"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_create_on_a_gke_context_uses_the_gke_profile(
    cluster: FakeCoreV1, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: Path
) -> None:
    monkeypatch.setattr(profile_mod, "detect_provider", lambda context=None: "gke")
    monkeypatch.setattr(profile_mod, "active_context", lambda: "gke_my-proj_us-central1-a_c1")
    seen: dict[str, Any] = {}
    real = profile_mod.profile_for

    def spy(provider: str, **kw: Any) -> Any:
        seen.update(provider=provider, **kw)
        return real(provider, **kw)

    monkeypatch.setattr(profile_mod, "profile_for", spy)
    assert cli.run(["sandbox", "create", "--name", "g", "--gsa", "worker"]) == 0
    assert seen["provider"] == "gke" and seen["project"] == "my-proj" and seen["region"] == "us-central1"
    assert seen["gsa"] == "worker@my-proj.iam.gserviceaccount.com"
    assert tomllib.loads((env / ".sk8s-sandbox-g.toml").read_text())["image_prefix"].startswith(
        "us-central1-docker.pkg.dev/my-proj/")


def test_gke_flags_are_refused_elsewhere(cluster: FakeCoreV1, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(["sandbox", "create", "--name", "x", "--gsa", "w"]) == 1
    assert "gke" in capsys.readouterr().err.lower()
    assert not cluster.namespaces


def test_list_text_and_json(cluster: FakeCoreV1, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(["sandbox", "list"]) == 0
    assert "no sandboxes" in capsys.readouterr().out
    cli.run(["sandbox", "create", "--name", "alpha", "--budget-usd", "3"])
    cli.run(["sandbox", "create", "--name", "beta", "--ttl", "1h"])
    cluster.namespaces["sk8s-sbx-beta"].metadata.annotations[sandbox.ANN_EXPIRES] = "2000-01-01T00:00:00+00:00"
    cluster.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name="prod")))
    capsys.readouterr()
    assert cli.run(["sandbox", "list"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and "prod" not in out
    assert "EXPIRED" in out and "$3" in out
    assert cli.run(["sandbox", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["alpha", "beta"]
    assert rows[0]["expired"] is False and rows[1]["expired"] is True
    assert rows[0]["budget"] == {"max_estimated_usd": 3.0, "assumed_job_hours": 1.0}


def test_delete_cascades_and_removes_the_local_config_file(
    cluster: FakeCoreV1, env: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cli.run(["sandbox", "create", "--name", "alpha"])
    store = LocalStore(tmp_path / "blobs")
    store.put("sbx-alpha/wf/x", b"1")
    store.put("keep/me", b"2")
    capsys.readouterr()
    assert cli.run(["sandbox", "delete", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "sk8s-sbx-alpha" in out
    assert "sk8s-sbx-alpha" not in cluster.namespaces
    assert store.count_prefix("sbx-alpha") == 0 and store.count_prefix("keep") == 1
    assert not (env / ".sk8s-sandbox-alpha.toml").exists()


def test_delete_unknown_and_foreign_namespaces_fail(
    cluster: FakeCoreV1, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(["sandbox", "delete", "ghost"]) == 1
    assert "no such sandbox" in capsys.readouterr().err
    cluster.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name="sk8s-sbx-x")))
    assert cli.run(["sandbox", "delete", "x"]) == 1
    assert "not an sk8s sandbox" in capsys.readouterr().err
    assert "sk8s-sbx-x" in cluster.namespaces


def test_clean_help_mentions_sandboxes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.run(["clean", "--help"])
    assert "sandbox" in capsys.readouterr().out
