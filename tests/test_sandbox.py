"""sk8s.sandbox: create / list / delete / reap, against in-memory fakes."""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client

import sk8s
from sk8s import cleaning, profile as profile_mod, sandbox
from sk8s.budget import Budget
from sk8s.config import Config
from sk8s.errors import Sk8sError
from sk8s.store.local import LocalStore
from tests.fakes import FakeAppsV1, FakeBatchV1, FakeCoreV1, FakeRbacV1

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

NOW = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.timezone.utc)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.core, self.batch, self.rbac, self.apps = FakeCoreV1(), FakeBatchV1(), FakeRbacV1(), FakeAppsV1()
        self.base = tmp_path / "blobs"
        self.cwd = tmp_path / "cwd"
        self.cwd.mkdir()

    def create(self, name: str | None = "alpha", **kw: Any) -> sandbox.Created:
        cfg = kw.pop("cfg", Config(store=f"local:{self.base}"))
        return sandbox.create(
            name, cfg=cfg, provider=kw.pop("provider", "local"), config_dir=self.cwd,
            core_api=self.core, rbac_api=self.rbac, now=kw.pop("now", NOW), **kw,
        )

    def reap(self, now: datetime.datetime) -> sandbox.ReapReport:
        return sandbox.reap(core_api=self.core, batch_api=self.batch, apps_api=self.apps, now=now)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _age(core: FakeCoreV1, name: str, *, created: datetime.datetime, expires: datetime.datetime) -> None:
    ann = core.namespaces[sandbox.namespace_for(name)].metadata.annotations
    ann[sandbox.ANN_CREATED] = created.isoformat()
    ann[sandbox.ANN_EXPIRES] = expires.isoformat()


def _job(ns: str, name: str, *, active: bool) -> Any:
    status = client.V1JobStatus(active=1) if active else client.V1JobStatus(succeeded=1)
    return client.V1Job(metadata=client.V1ObjectMeta(name=name, namespace=ns), status=status)


# --- names, ttl, store prefix ---------------------------------------------------------------------------


def test_namespace_naming_and_validation() -> None:
    assert sandbox.namespace_for("alpha") == "sk8s-sbx-alpha"
    assert sandbox.name_of("sk8s-sbx-alpha") == "alpha"
    assert sandbox.name_of("alpha") == "alpha"
    for bad in ("", "Up", "a_b", "-a", "a-", "x" * 55, "a b"):
        with pytest.raises(Sk8sError, match="sandbox name"):
            sandbox.validate_name(bad)
    sandbox.validate_name("a")
    sandbox.validate_name("x" * 54)
    random_name = sandbox.new_name(None)
    assert len(random_name) == 6 and random_name != sandbox.new_name(None)
    sandbox.validate_name(random_name)


@pytest.mark.parametrize("text,seconds", [
    ("90s", 90), ("30m", 1800), ("24h", 86400), ("7d", 604800), ("1.5h", 5400),
])
def test_parse_ttl(text: str, seconds: float) -> None:
    assert sandbox.parse_ttl(text) == seconds


@pytest.mark.parametrize("text", ["", "24", "h", "1w", "-1h", "0h", "abc", "1 h", "nanh"])
def test_parse_ttl_rejects(text: str) -> None:
    with pytest.raises(Sk8sError, match="ttl"):
        sandbox.parse_ttl(text)


def test_sandbox_store_prefixes_the_configured_store() -> None:
    assert sandbox.sandbox_store("k8s", "a") == ("k8s", "sbx-a")  # the namespace IS the isolation
    assert sandbox.sandbox_store("local:/tmp/x", "a") == ("local:/tmp/x/sbx-a", "sbx-a")
    assert sandbox.sandbox_store("s3://bkt/", "a") == ("s3://bkt/sbx-a", "sbx-a")
    assert sandbox.sandbox_store("s3://bkt/team", "a") == ("s3://bkt/team/sbx-a", "sbx-a")
    assert sandbox.sandbox_store("gs://bkt/team/", "a") == ("gs://bkt/team/sbx-a", "sbx-a")
    with pytest.raises(Sk8sError, match="store"):
        sandbox.sandbox_store("ftp://x", "a")


# --- create ---------------------------------------------------------------------------------------------


def test_create_makes_a_labelled_namespace_with_rbac_and_a_ttl(env: Env) -> None:
    made = env.create("alpha", ttl_seconds=3600)
    ns = env.core.namespaces["sk8s-sbx-alpha"]
    assert ns.metadata.labels[sandbox.LABEL_KEY] == sandbox.LABEL_VALUE
    assert ns.metadata.labels["app.kubernetes.io/managed-by"] == "sk8s"
    ann = ns.metadata.annotations
    assert ann[sandbox.ANN_CREATED] == NOW.isoformat()
    assert ann[sandbox.ANN_EXPIRES] == (NOW + datetime.timedelta(hours=1)).isoformat()
    # the profile apply did the RBAC: service account, role, binding exist in the new namespace
    assert ("sk8s-sbx-alpha", "sk8s") in env.core.service_accounts
    assert ("sk8s-sbx-alpha", "sk8s") in env.rbac.roles
    assert made.sandbox.namespace == "sk8s-sbx-alpha" and made.sandbox.name == "alpha"
    assert any("Role" in line for line in made.lines)


def test_create_writes_an_isolated_config_file_in_cwd(env: Env) -> None:
    made = env.create("alpha", budget_usd=2.5, cfg=Config(
        store=f"local:{env.base}", store_endpoint=None, image_prefix="reg.io/me", image="img:1",
        store_env={"AWS_X": "y"}, namespace="not-this-one"))
    assert made.config_path == env.cwd / ".sk8s-sandbox-alpha.toml"
    data = tomllib.loads(made.config_path.read_text())
    assert data["namespace"] == "sk8s-sbx-alpha"
    assert data["store"] == f"local:{env.base}/sbx-alpha"
    assert data["budget"] == {"max_estimated_usd": 2.5, "assumed_job_hours": 1.0}
    assert data["image_prefix"] == "reg.io/me" and data["image"] == "img:1"
    assert data["store_env"] == {"AWS_X": "y"}
    assert (made.config_path.stat().st_mode & 0o777) == 0o600  # may carry store credentials
    assert made.export_line == f"export SK8S_CONFIG={made.config_path}"
    # and it loads as a Config
    from sk8s.config import load_config
    cfg = load_config(path=made.config_path, env={})
    assert cfg.namespace == "sk8s-sbx-alpha" and cfg.budget == Budget(max_estimated_usd=2.5)


def test_create_without_a_budget_writes_none(env: Env) -> None:
    made = env.create("alpha")
    assert "budget" not in tomllib.loads(made.config_path.read_text())


def test_create_with_the_k8s_store_keeps_it(env: Env) -> None:
    made = env.create("alpha", cfg=Config(store="k8s"))
    assert tomllib.loads(made.config_path.read_text())["store"] == "k8s"


def test_create_generates_a_name_when_none_is_given(env: Env) -> None:
    made = env.create(None)
    assert made.sandbox.namespace.startswith("sk8s-sbx-") and len(made.sandbox.name) == 6
    assert made.config_path.name == f".sk8s-sandbox-{made.sandbox.name}.toml"


def test_create_refuses_to_touch_an_existing_namespace(env: Env) -> None:
    env.core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name="sk8s-sbx-alpha")))
    with pytest.raises(Sk8sError, match="already exists"):
        env.create("alpha")
    assert ("sk8s-sbx-alpha", "sk8s") not in env.core.service_accounts  # nothing was applied to it
    env2 = env.create("beta")
    with pytest.raises(Sk8sError, match="already exists"):
        env.create("beta")
    assert env2.config_path.exists()


def test_failed_apply_rolls_the_namespace_back(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("rbac exploded")

    monkeypatch.setattr(profile_mod, "apply", boom)
    with pytest.raises(RuntimeError, match="exploded"):
        env.create("alpha")
    assert "sk8s-sbx-alpha" not in env.core.namespaces
    assert not list(env.cwd.iterdir())


def test_create_rejects_a_bad_budget_before_creating_anything(env: Env) -> None:
    with pytest.raises(ValueError):
        env.create("alpha", budget_usd=-1)
    assert not env.core.namespaces


# --- list -----------------------------------------------------------------------------------------------


def test_list_returns_only_sandboxes(env: Env) -> None:
    env.create("alpha", budget_usd=1.0)
    env.create("beta")
    env.core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name="prod")))
    got = sandbox.list_sandboxes(env.core)
    assert [s.name for s in got] == ["alpha", "beta"]
    alpha = got[0]
    assert alpha.namespace == "sk8s-sbx-alpha" and alpha.budget == Budget(max_estimated_usd=1.0)
    assert alpha.expires_at == NOW + datetime.timedelta(hours=24)
    assert alpha.expired(NOW) is False
    assert alpha.expired(NOW + datetime.timedelta(hours=25)) is True
    assert alpha.to_dict()["namespace"] == "sk8s-sbx-alpha"


# --- delete ---------------------------------------------------------------------------------------------


def test_delete_cascades_namespace_and_store_prefix(env: Env) -> None:
    made = env.create("alpha")
    other = env.create("beta")
    store = LocalStore(env.base)
    store.put("sbx-alpha/wf-1/payload", b"a")
    store.put("sbx-alpha/wf-2/result", b"b")
    store.put("sbx-beta/wf-9/payload", b"keep")
    store.put("sbx-alphabet/x", b"keep too")  # whole-segment matching: not alpha's
    env.core.create_namespaced_config_map("sk8s-sbx-alpha", client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name="cm1")))
    lines = sandbox.delete("alpha", core_api=env.core)
    assert "sk8s-sbx-alpha" not in env.core.namespaces and ("sk8s-sbx-alpha", "cm1") not in env.core.configmaps
    assert "sk8s-sbx-beta" in env.core.namespaces
    assert store.count_prefix("sbx-alpha") == 0
    assert store.count_prefix("sbx-beta") == 1 and store.count_prefix("sbx-alphabet") == 1
    assert any("alpha" in line for line in lines)
    assert other.config_path.exists()


def test_delete_accepts_the_full_namespace_name(env: Env) -> None:
    env.create("alpha")
    sandbox.delete("sk8s-sbx-alpha", core_api=env.core)
    assert not env.core.namespaces


def test_delete_never_deletes_a_namespace_that_is_not_a_sandbox(env: Env) -> None:
    env.core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name="sk8s-sbx-imposter")))
    with pytest.raises(Sk8sError, match="not an sk8s sandbox"):
        sandbox.delete("imposter", core_api=env.core)
    assert "sk8s-sbx-imposter" in env.core.namespaces
    with pytest.raises(Sk8sError, match="no such sandbox"):
        sandbox.delete("ghost", core_api=env.core)


def test_delete_keeps_the_namespace_when_the_store_cannot_be_cleaned(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.create("alpha")

    def boom(self: LocalStore, prefix: str) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(LocalStore, "delete_prefix", boom)
    with pytest.raises(Sk8sError, match="disk on fire"):
        sandbox.delete("alpha", core_api=env.core)
    assert "sk8s-sbx-alpha" in env.core.namespaces  # retryable: the record of what to clean survives


def test_delete_with_the_k8s_store_only_removes_the_namespace(env: Env) -> None:
    env.create("alpha", cfg=Config(store="k8s"))
    sandbox.delete("alpha", core_api=env.core)
    assert not env.core.namespaces


# --- reap -----------------------------------------------------------------------------------------------


def test_reap_deletes_expired_idle_sandboxes_only(env: Env) -> None:
    env.create("old")
    env.create("fresh")
    LocalStore(env.base).put("sbx-old/wf/x", b"1")
    _age(env.core, "old", created=NOW - datetime.timedelta(days=3), expires=NOW - datetime.timedelta(days=2))
    report = env.reap(NOW)
    assert report.deleted == ["old"]
    assert "sk8s-sbx-old" not in env.core.namespaces and "sk8s-sbx-fresh" in env.core.namespaces
    assert LocalStore(env.base).count_prefix("sbx-old") == 0
    assert [k["sandbox"] for k in report.kept] == ["fresh"] and "not expired" in report.kept[0]["reason"]


def test_reap_never_deletes_a_sandbox_with_active_jobs(env: Env) -> None:
    env.create("busy")
    _age(env.core, "busy", created=NOW - datetime.timedelta(days=3), expires=NOW - datetime.timedelta(days=2))
    env.batch.create_namespaced_job("sk8s-sbx-busy", _job("sk8s-sbx-busy", "j1", active=True))
    report = env.reap(NOW)
    assert report.deleted == [] and "active jobs" in report.kept[0]["reason"]
    assert "sk8s-sbx-busy" in env.core.namespaces
    # finished jobs do not hold it
    env.batch.jobs.clear()
    env.batch.create_namespaced_job("sk8s-sbx-busy", _job("sk8s-sbx-busy", "j1", active=False))
    assert env.reap(NOW).deleted == ["busy"]


def test_reap_never_deletes_a_sandbox_with_a_running_service(env: Env) -> None:
    env.create("svc")
    _age(env.core, "svc", created=NOW - datetime.timedelta(days=3), expires=NOW - datetime.timedelta(days=2))
    env.apps.create_namespaced_deployment("sk8s-sbx-svc", SimpleNamespace(
        metadata=SimpleNamespace(name="d1", namespace="sk8s-sbx-svc")))
    report = env.reap(NOW)
    assert report.deleted == [] and "service" in report.kept[0]["reason"]


def test_reap_honours_the_age_floor(env: Env) -> None:
    env.create("tiny", now=NOW)
    # expired a minute after creation: still younger than the 300s floor every sweep respects
    _age(env.core, "tiny", created=NOW, expires=NOW + datetime.timedelta(seconds=1))
    report = env.reap(NOW + datetime.timedelta(seconds=60))
    assert report.deleted == [] and "minimum age" in report.kept[0]["reason"]
    assert env.reap(NOW + datetime.timedelta(seconds=400)).deleted == ["tiny"]


def test_reap_keeps_what_it_cannot_prove_idle_or_parse(env: Env) -> None:
    env.create("odd")
    env.core.namespaces["sk8s-sbx-odd"].metadata.annotations[sandbox.ANN_EXPIRES] = "garbage"
    report = env.reap(NOW + datetime.timedelta(days=30))
    assert report.deleted == [] and "malformed" in report.kept[0]["reason"]

    env.create("unlistable")
    _age(env.core, "unlistable", created=NOW - datetime.timedelta(days=3), expires=NOW - datetime.timedelta(days=2))

    class Broken(FakeBatchV1):
        def list_namespaced_job(self, *a: Any, **k: Any) -> Any:
            raise client.ApiException(status=500, reason="boom")

    r = sandbox.reap(core_api=env.core, batch_api=Broken(), apps_api=env.apps, now=NOW)
    assert "unlistable" not in r.deleted
    assert any("could not be listed" in k["reason"] for k in r.kept if k["sandbox"] == "unlistable")


def test_reap_only_touches_labelled_sandboxes(env: Env) -> None:
    env.core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(
        name="sk8s-sbx-fake", annotations={sandbox.ANN_EXPIRES: "2000-01-01T00:00:00+00:00"})))
    assert env.reap(NOW).deleted == []
    assert "sk8s-sbx-fake" in env.core.namespaces


# --- clean() integration --------------------------------------------------------------------------------


def test_clean_reaps_expired_sandboxes_and_reports_them(env: Env) -> None:
    env.create("old")
    real = datetime.datetime.now(datetime.timezone.utc)
    _age(env.core, "old", created=real - datetime.timedelta(days=3), expires=real - datetime.timedelta(days=2))
    report = cleaning.clean_report("ns1", core_api=env.core, batch_api=env.batch, apps_api=env.apps)
    assert report.sandboxes_deleted == ["old"]
    assert report.as_dict()["sandboxes"]["deleted"] == ["old"]
    assert "sk8s-sbx-old" not in env.core.namespaces


def test_clean_can_leave_sandboxes_alone_and_never_when_a_workflow_is_named(env: Env) -> None:
    env.create("old")
    real = datetime.datetime.now(datetime.timezone.utc)
    _age(env.core, "old", created=real - datetime.timedelta(days=3), expires=real - datetime.timedelta(days=2))
    cleaning.clean("ns1", core_api=env.core, batch_api=env.batch, sandboxes=False)
    assert "sk8s-sbx-old" in env.core.namespaces
    cleaning.clean("ns1", core_api=env.core, batch_api=env.batch, workflow="wf-x")
    assert "sk8s-sbx-old" in env.core.namespaces
    assert "sandboxes" not in cleaning.CleanReport().as_dict()  # the JSON shape only grows when used


def test_clean_survives_a_core_api_that_cannot_list_namespaces(env: Env) -> None:
    class NoNamespaces(FakeCoreV1):
        def list_namespace(self, label_selector: str = "") -> Any:
            raise client.ApiException(status=403, reason="Forbidden")

    assert cleaning.clean("ns1", core_api=NoNamespaces(), batch_api=env.batch) == []


def test_the_worker_startup_sweep_does_not_reap_sandboxes(monkeypatch: pytest.MonkeyPatch) -> None:
    import sk8s.worker as worker

    seen: dict[str, Any] = {}
    monkeypatch.setattr(cleaning, "clean", lambda *a, **k: seen.update(k) or [])
    worker._sweep({"SK8S_RESULT_KEY": "k", "SK8S_NAMESPACE": "ns1"})
    assert seen["sandboxes"] is False  # a pod's namespaced Role could not (and must not) do this


def test_sandbox_symbols_are_reachable_from_the_package() -> None:
    assert sk8s.sandbox.create is sandbox.create
