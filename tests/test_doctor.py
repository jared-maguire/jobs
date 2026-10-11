from typing import Any

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from sk8s.doctor import Check, doctor
from sk8s.errors import BlobNotFound
from sk8s.profile import ClusterProfile, apply, profile_for
from sk8s.store.base import BlobStore
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1, FakeRbacV1, FakeStorageV1


def _profile(**kw: Any) -> ClusterProfile:
    return profile_for("generic", namespace="team-a", store="k8s", **kw)


def _converged(p: ClusterProfile | None = None) -> tuple[FakeCoreV1, FakeRbacV1]:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    apply(p or _profile(), core_api=core, rbac_api=rbac)  # type: ignore[arg-type]
    return core, rbac


def _run(
    p: ClusterProfile,
    core: Any,
    rbac: Any,
    store: BlobStore,
    storage: Any = None,
) -> dict[str, Check]:
    checks = doctor(
        p,
        core_api=core,
        rbac_api=rbac,
        batch_api=FakeBatchV1(),  # type: ignore[arg-type]
        storage_api=storage,
        store=store,
    )
    by_name = {c.name: c for c in checks}
    assert len(by_name) == len(checks)  # names are unique
    return by_name


class MemStore(BlobStore):
    """Dict-backed store with knobs for the failure modes doctor must catch."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.deletes: list[str] = []
        self.drop_writes = False  # read-only store that swallows writes
        self.fail_get: Exception | None = None
        self.fail_put: Exception | None = None
        self.ignore_delete = False

    def uri_for(self, key: str) -> str:
        return f"mem:{key}"

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        if self.fail_put:
            raise self.fail_put
        if not self.drop_writes:
            self.blobs[key] = data
        return self.uri_for(key)

    def get(self, uri: str) -> bytes:
        if self.fail_get:
            raise self.fail_get
        key = uri[len("mem:") :]
        if key not in self.blobs:
            raise BlobNotFound(uri)
        return self.blobs[key]

    def delete(self, uri: str) -> None:
        self.deletes.append(uri)
        if not self.ignore_delete:
            self.blobs.pop(uri[len("mem:") :], None)

    def delete_prefix(self, prefix: str) -> None:
        raise NotImplementedError


def _names(checks: dict[str, Check]) -> list[str]:
    return list(checks)


# --- happy path --------------------------------------------------------------------------------


def test_all_green_on_a_converged_cluster() -> None:
    core, rbac = _converged()
    checks = _run(_profile(), core, rbac, MemStore())
    assert all(c.ok for c in checks.values()), checks
    assert all(c.fix_hint == "" for c in checks.values())
    assert set(checks) == {
        "cluster reachable",
        "namespace exists",
        "service account exists",
        "role permissions",
        "store writable",
    }  # no storage-class check: the profile names none


def test_doctor_is_read_only_against_the_cluster() -> None:
    core, rbac = _converged()
    core_before, rbac_before = list(core.actions), list(rbac.actions)
    _run(_profile(), core, rbac, MemStore())
    assert core.actions == core_before and rbac.actions == rbac_before


# --- individual failures -----------------------------------------------------------------------


def test_missing_namespace_fails_with_config_hint() -> None:
    _, rbac = _converged()
    checks = _run(_profile(), FakeCoreV1(), rbac, MemStore())
    assert not checks["namespace exists"].ok
    assert "run: sk8s config" in checks["namespace exists"].fix_hint
    assert "team-a" in checks["namespace exists"].fix_hint
    assert checks["cluster reachable"].ok  # others unaffected


def test_missing_service_account_fails_but_others_still_run() -> None:
    core, rbac = _converged()
    del core.service_accounts[("team-a", "sk8s")]
    checks = _run(_profile(), core, rbac, MemStore())
    assert not checks["service account exists"].ok
    assert "run: sk8s config" in checks["service account exists"].fix_hint
    assert checks["namespace exists"].ok
    assert checks["role permissions"].ok
    assert checks["store writable"].ok


def test_missing_role_fails_with_config_hint() -> None:
    core, rbac = _converged()
    del rbac.roles[("team-a", "sk8s")]
    c = _run(_profile(), core, rbac, MemStore())["role permissions"]
    assert not c.ok and "sk8s config" in c.fix_hint


def test_role_missing_patch_is_caught_and_named() -> None:
    core, rbac = _converged()
    for rule in rbac.roles[("team-a", "sk8s")].rules:
        rule.verbs = [v for v in rule.verbs if v != "patch"]
    c = _run(_profile(), core, rbac, MemStore())["role permissions"]
    assert not c.ok
    assert "patch" in c.fix_hint and "sk8s config" in c.fix_hint


def test_role_missing_one_resource_is_caught() -> None:
    core, rbac = _converged()
    role = rbac.roles[("team-a", "sk8s")]
    role.rules = [r for r in role.rules if "deployments" not in r.resources]
    c = _run(_profile(), core, rbac, MemStore())["role permissions"]
    assert not c.ok and "deployments" in c.fix_hint


def test_extra_grants_are_not_failures() -> None:
    core, rbac = _converged()
    role = rbac.roles[("team-a", "sk8s")]
    role.rules[0].verbs.append("escalate")
    role.rules.append(client.V1PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get"]))
    assert _run(_profile(), core, rbac, MemStore())["role permissions"].ok


def test_reshuffled_or_wildcard_role_that_covers_requirements_passes() -> None:
    core, rbac = _converged()
    rbac.roles[("team-a", "sk8s")] = client.V1Role(
        rules=[client.V1PolicyRule(api_groups=["*"], resources=["*"], verbs=["*"])]
    )
    assert _run(_profile(), core, rbac, MemStore())["role permissions"].ok


def test_name_narrowed_rules_do_not_satisfy_requirements() -> None:
    core, rbac = _converged()
    for rule in rbac.roles[("team-a", "sk8s")].rules:
        rule.resource_names = ["just-one"]
    assert not _run(_profile(), core, rbac, MemStore())["role permissions"].ok


def test_forbidden_reads_fail_with_a_message_not_a_crash() -> None:
    class Forbidden(FakeRbacV1):
        def read_namespaced_role(self, name: str, namespace: str) -> object:
            raise ApiException(status=403, reason="Forbidden")

    core, _ = _converged()
    c = _run(_profile(), core, Forbidden(), MemStore())["role permissions"]
    assert not c.ok and c.fix_hint


# --- unreachable cluster -----------------------------------------------------------------------


class _Down:
    """Every API call fails like a refused connection."""

    def __getattr__(self, name: str) -> Any:
        def boom(*a: Any, **k: Any) -> Any:
            raise OSError("connection refused")

        return boom


def test_unreachable_cluster_fails_reachability_and_marks_dependents() -> None:
    checks = _run(_profile(), _Down(), _Down(), LocalStore("/nonexistent-never-used"))
    assert not checks["cluster reachable"].ok
    assert "kubeconfig" in checks["cluster reachable"].fix_hint
    for name in ("namespace exists", "service account exists", "role permissions"):
        assert not checks[name].ok
        assert "cluster unreachable" in checks[name].fix_hint


def test_unreachable_cluster_does_not_stop_a_cluster_independent_store_check(
    tmp_path: Any,
) -> None:
    p = profile_for("generic", namespace="team-a", store=f"local:{tmp_path}")
    checks = _run(p, _Down(), _Down(), LocalStore(tmp_path))
    assert not checks["cluster reachable"].ok
    assert checks["store writable"].ok


def test_unreachable_cluster_with_k8s_store_reports_unreachable() -> None:
    class DownStore(MemStore):
        def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
            raise OSError("connection refused")

    checks = _run(_profile(), _Down(), _Down(), DownStore())
    assert not checks["store writable"].ok
    assert "cluster unreachable" in checks["store writable"].fix_hint


def test_unreachable_cluster_with_k8s_store_skips_the_probe() -> None:
    calls: list[str] = []

    class SpyStore(MemStore):
        def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
            calls.append("put")
            return super().put(key, data, owner)

        def get(self, uri: str) -> bytes:
            calls.append("get")
            return super().get(uri)

        def delete(self, uri: str) -> None:
            calls.append("delete")
            super().delete(uri)

    c = _run(_profile(), _Down(), _Down(), SpyStore())["store writable"]
    assert not c.ok and "cluster unreachable" in c.fix_hint
    assert calls == []


def test_default_clients_failing_to_build_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sk8s import k8s

    def no_config() -> None:
        raise RuntimeError("no kubeconfig")

    monkeypatch.setattr(k8s, "core_api", no_config)
    monkeypatch.setattr(k8s, "rbac_api", no_config)
    monkeypatch.setattr(k8s, "batch_api", no_config)
    monkeypatch.setattr(k8s, "storage_api", no_config)
    checks = doctor(_profile(), store=MemStore())
    by = {c.name: c for c in checks}
    assert not by["cluster reachable"].ok
    assert "no kubeconfig" in by["cluster reachable"].fix_hint
    assert not by["role permissions"].ok


# --- store probe -------------------------------------------------------------------------------


def test_store_probe_round_trips_under_doctor_prefix_and_cleans_up() -> None:
    core, rbac = _converged()
    store = MemStore()
    seen: list[str] = []
    orig_put = store.put

    def spy(key: str, data: bytes, owner: Any | None = None) -> str:
        seen.append(key)
        return orig_put(key, data, owner)

    store.put = spy  # type: ignore[method-assign]
    assert _run(_profile(), core, rbac, store)["store writable"].ok
    assert len(seen) == 1 and seen[0].startswith("doctor/")
    assert store.blobs == {} and store.deletes  # probe removed


def test_read_only_store_that_drops_writes_fails() -> None:
    core, rbac = _converged()
    store = MemStore()
    store.drop_writes = True
    c = _run(_profile(), core, rbac, store)["store writable"]
    assert not c.ok and "read" in c.fix_hint.lower()


def test_store_put_failure_is_reported() -> None:
    core, rbac = _converged()
    store = MemStore()
    store.fail_put = PermissionError("read-only filesystem")
    c = _run(_profile(), core, rbac, store)["store writable"]
    assert not c.ok and "write" in c.fix_hint.lower() and "read-only" in c.fix_hint


def test_get_failure_still_cleans_up_the_probe() -> None:
    core, rbac = _converged()
    store = MemStore()
    store.fail_get = RuntimeError("boom")
    c = _run(_profile(), core, rbac, store)["store writable"]
    assert not c.ok
    assert store.deletes and store.blobs == {}  # probe was deleted despite the failed get


def test_corrupt_read_back_fails_and_cleans_up() -> None:
    core, rbac = _converged()

    class Corrupt(MemStore):
        def get(self, uri: str) -> bytes:
            super().get(uri)
            return b"something else"

    store = Corrupt()
    c = _run(_profile(), core, rbac, store)["store writable"]
    assert not c.ok and store.blobs == {}


def test_delete_that_leaves_the_probe_behind_fails() -> None:
    core, rbac = _converged()
    store = MemStore()
    store.ignore_delete = True
    c = _run(_profile(), core, rbac, store)["store writable"]
    assert not c.ok and "delete" in c.fix_hint.lower()


def test_cleanup_error_does_not_mask_the_original_failure() -> None:
    core, rbac = _converged()

    class Bad(MemStore):
        def delete(self, uri: str) -> None:
            raise RuntimeError("delete also broken")

    store = Bad()
    store.fail_get = RuntimeError("get broken")
    c = _run(_profile(), core, rbac, store)["store writable"]
    assert not c.ok and "get broken" in c.fix_hint


def test_store_is_built_from_the_profile_when_not_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    core, rbac = _converged()
    p = profile_for("generic", namespace="team-a", store=f"local:{tmp_path}")
    checks = doctor(p, core_api=core, rbac_api=rbac, batch_api=FakeBatchV1())  # type: ignore[arg-type]
    assert {c.name: c for c in checks}["store writable"].ok


def test_store_endpoint_is_passed_to_the_fallback_store_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sk8s.store as store_mod

    core, rbac = _converged()
    seen: list[dict[str, Any]] = []

    def fake_get_store(store: str, **kw: Any) -> BlobStore:
        seen.append(kw)
        return MemStore()

    monkeypatch.setattr(store_mod, "get_store", fake_get_store)
    p = profile_for("generic", namespace="team-a", store="s3://bkt/p")
    doctor(p, core_api=core, rbac_api=rbac, batch_api=FakeBatchV1(),  # type: ignore[arg-type]
           store_endpoint="http://minio:9000")
    assert seen[0]["endpoint"] == "http://minio:9000"


def test_injected_store_is_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    import sk8s.store as store_mod

    def forbidden(*a: Any, **kw: Any) -> BlobStore:
        raise AssertionError("must not construct a store when one is injected")

    monkeypatch.setattr(store_mod, "get_store", forbidden)
    core, rbac = _converged()
    p = profile_for("generic", namespace="team-a", store="s3://bkt/p")
    checks = doctor(p, core_api=core, rbac_api=rbac, batch_api=FakeBatchV1(),  # type: ignore[arg-type]
                    store=MemStore(), store_endpoint="http://minio:9000")
    assert {c.name: c for c in checks}["store writable"].ok


def test_unbuildable_store_is_a_failed_check() -> None:
    core, rbac = _converged()
    p = profile_for("generic", namespace="team-a", store="ftp://nope")
    checks = doctor(p, core_api=core, rbac_api=rbac, batch_api=FakeBatchV1())  # type: ignore[arg-type]
    c = {c.name: c for c in checks}["store writable"]
    assert not c.ok and "ftp://nope" in c.fix_hint


# --- storage class -----------------------------------------------------------------------------


def test_storage_class_checked_only_when_profile_names_one() -> None:
    p = _profile(rwx_storage_class="nfs")
    core, rbac = _converged(p)
    ok = _run(p, core, rbac, MemStore(), FakeStorageV1(("nfs",)))["storage class exists"]
    assert ok.ok
    bad = _run(p, core, rbac, MemStore(), FakeStorageV1(("other",)))["storage class exists"]
    assert not bad.ok and "nfs" in bad.fix_hint
    assert "storage class exists" not in _run(_profile(), core, rbac, MemStore())


def test_storage_class_check_on_unreachable_cluster() -> None:
    p = _profile(rwx_storage_class="nfs")
    c = _run(p, _Down(), _Down(), MemStore(), _Down())["storage class exists"]
    assert not c.ok and "cluster unreachable" in c.fix_hint


# --- independence ------------------------------------------------------------------------------


def test_every_check_failing_still_returns_every_check() -> None:
    store = MemStore()
    store.fail_put = RuntimeError("nope")
    p = _profile(rwx_storage_class="nfs")
    checks = _run(p, FakeCoreV1(), FakeRbacV1(), store, FakeStorageV1())
    assert len(checks) == 6
    assert [c.ok for n, c in checks.items() if n != "cluster reachable"] == [False] * 5
