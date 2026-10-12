from typing import Any

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from sk8s.doctor import (
    WORKER_PROBE_TIMEOUT,
    Check,
    StoreProbeError,
    doctor,
    probe_store_in_pod,
    submit_probe_job,
)
from sk8s.errors import (
    BlobNotFound, ImageNotConfigured, ImagePullError, JobTimeout, PodFailed, RemoteTraceback,
)
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


# --- worker identity (in-cluster store probe) --------------------------------------------------

WORKER = "worker identity (in-cluster store probe)"


class Submitter:
    """Stub for the job-submission seam: records calls, returns or raises as told."""

    def __init__(self, result: Any = "ok", raises: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ClusterProfile]] = []
        self.result, self.raises = result, raises

    def __call__(self, fn: Any, profile: ClusterProfile) -> Any:
        self.calls.append((fn, profile))
        if self.raises:
            raise self.raises
        return self.result


def _remote(exc: Exception) -> Exception:
    """What a pod-side failure looks like after decode_result: raised from a RemoteTraceback."""
    exc.__cause__ = RemoteTraceback("Traceback (in the pod) ...")
    return exc


def _gs_profile(**kw: Any) -> ClusterProfile:
    return profile_for("generic", namespace="team-a", store="gs://bkt/p/", **kw)


def _doctor_gs(
    submit: Any, p: ClusterProfile | None = None, core: Any = None, rbac: Any = None
) -> dict[str, Check]:
    p = p or _gs_profile()
    if core is None:
        core, rbac = _converged(p)
    checks = doctor(
        p, core_api=core, rbac_api=rbac, batch_api=FakeBatchV1(),  # type: ignore[arg-type]
        store=MemStore(), submit_probe=submit,
    )
    return {c.name: c for c in checks}


def test_worker_probe_appears_and_passes_for_a_reachable_gs_store() -> None:
    sub = Submitter()
    checks = _doctor_gs(sub)
    assert checks[WORKER].ok and checks[WORKER].fix_hint == ""
    assert all(c.ok for c in checks.values())
    assert len(sub.calls) == 1 and sub.calls[0][1].store == "gs://bkt/p/"
    assert sub.calls[0][0] is probe_store_in_pod  # the job runs the probe function itself


@pytest.mark.parametrize("store", ["k8s", "s3://b/p", "local:/tmp/x"])
def test_worker_probe_is_absent_not_failed_for_other_stores(store: str) -> None:
    sub = Submitter()
    p = profile_for("generic", namespace="team-a", store=store)
    core, rbac = _converged(p)
    checks = doctor(
        p, core_api=core, rbac_api=rbac, batch_api=FakeBatchV1(),  # type: ignore[arg-type]
        store=MemStore(), submit_probe=sub,
    )
    assert WORKER not in {c.name for c in checks}
    assert all(c.ok for c in checks)
    assert sub.calls == []


def test_worker_probe_is_absent_for_the_local_provider() -> None:
    sub = Submitter()
    p = profile_for("local", namespace="team-a", store="local:/tmp/x")
    core, rbac = _converged(p)
    checks = doctor(
        p, core_api=core, rbac_api=rbac, batch_api=FakeBatchV1(),  # type: ignore[arg-type]
        store=MemStore(), submit_probe=sub,
    )
    assert WORKER not in {c.name for c in checks} and sub.calls == []


def test_worker_probe_is_absent_when_the_cluster_is_unreachable() -> None:
    sub = Submitter()
    checks = _doctor_gs(sub, core=_Down(), rbac=_Down())
    assert WORKER not in checks and sub.calls == []
    assert not checks["cluster reachable"].ok


def test_worker_probe_failure_names_workload_identity_first() -> None:
    sub = Submitter(raises=_remote(RuntimeError("403 caller does not have storage.objects.create")))
    c = _doctor_gs(sub)[WORKER]
    assert not c.ok
    assert "Workload Identity" in c.fix_hint
    assert c.fix_hint.index("Workload Identity") < c.fix_hint.index("403")  # lead with the cause
    for needle in (
        "iam.gke.io/gcp-service-account",
        "roles/iam.workloadIdentityUser",
        "serviceAccount:<project>.svc.id.goog[team-a/sk8s]",
        "roles/storage.objectAdmin",
        "403 caller does not have",
    ):
        assert needle in c.fix_hint


def test_worker_probe_client_side_failure_is_not_blamed_on_workload_identity() -> None:
    # No RemoteTraceback cause: the error arose before/outside the pod (payload write, API call).
    sub = Submitter(raises=PermissionError("403 client cannot write the payload"))
    c = _doctor_gs(sub)[WORKER]
    assert not c.ok
    assert "before reaching the pod" in c.fix_hint and "'store writable'" in c.fix_hint
    assert "403 client cannot write the payload" in c.fix_hint
    assert "Workload Identity" not in c.fix_hint


def test_worker_probe_pod_failure_without_a_result_still_points_at_workload_identity() -> None:
    c = _doctor_gs(Submitter(raises=PodFailed("doctor-abc", "container crashed")))[WORKER]
    assert not c.ok and "Workload Identity" in c.fix_hint


def test_worker_probe_non_ok_result_fails() -> None:
    c = _doctor_gs(Submitter(result="nope"))[WORKER]
    assert not c.ok and "Workload Identity" in c.fix_hint


def test_worker_probe_image_pull_failure_gets_an_image_hint() -> None:
    c = _doctor_gs(Submitter(raises=ImagePullError("doctor-abc", "reg/x:1")))[WORKER]
    assert not c.ok and "image" in c.fix_hint and "reg/x:1" in c.fix_hint
    assert "Workload Identity" not in c.fix_hint


def test_worker_probe_with_no_image_configured_says_so() -> None:
    c = _doctor_gs(Submitter(raises=ImageNotConfigured()))[WORKER]
    assert not c.ok and "no worker image" in c.fix_hint and "SK8S_IMAGE" in c.fix_hint
    # the same remedy `sk8s config` and the commented hint in the toml point at
    assert "sk8s config --image" in c.fix_hint and "image_prefix" in c.fix_hint
    assert "Workload Identity" not in c.fix_hint and "client-side" not in c.fix_hint


def test_worker_probe_timeout_gets_a_timeout_hint() -> None:
    c = _doctor_gs(Submitter(raises=JobTimeout("doctor-abc", 600.0)))[WORKER]
    assert not c.ok and "600s" in c.fix_hint and "autoscal" in c.fix_hint


def test_worker_probe_not_attempted_when_namespace_or_service_account_missing() -> None:
    sub = Submitter()
    checks = _doctor_gs(sub, core=FakeCoreV1(), rbac=FakeRbacV1())
    assert not checks[WORKER].ok and "not attempted" in checks[WORKER].fix_hint
    assert "sk8s config" in checks[WORKER].fix_hint
    assert sub.calls == []


def test_client_side_store_check_alone_does_not_make_a_gs_store_healthy() -> None:
    """The client's own store probe passes (MemStore), yet a failing pod still fails doctor."""
    checks = _doctor_gs(Submitter(raises=_remote(RuntimeError("denied"))))
    assert checks["store writable"].ok and not checks[WORKER].ok


def test_worker_probe_function_round_trips_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import sk8s.store as store_mod

    mem = MemStore()
    seen: list[str] = []

    def fake_get_store(url: str, **kw: Any) -> BlobStore:
        seen.append(url)
        return mem

    monkeypatch.setattr(store_mod, "get_store", fake_get_store)
    assert probe_store_in_pod("gs://bkt/p/") == "ok"
    assert seen == ["gs://bkt/p/"]
    assert mem.blobs == {} and len(mem.deletes) == 1  # put, get, delete, confirmed gone


def test_worker_probe_function_raises_when_the_store_denies_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sk8s.store as store_mod

    mem = MemStore()
    mem.fail_put = PermissionError("403 forbidden")
    monkeypatch.setattr(store_mod, "get_store", lambda url, **kw: mem)
    with pytest.raises(StoreProbeError, match="write failed.*403"):
        probe_store_in_pod("gs://bkt/p/")


def test_worker_probe_end_to_end_with_a_seam_that_runs_the_function_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sk8s.store as store_mod

    mem = MemStore()
    monkeypatch.setattr(store_mod, "get_store", lambda url, **kw: mem)
    def in_pod(fn: Any, p: ClusterProfile) -> str:  # like a real run: failures arrive remote
        try:
            return str(fn(p.store))
        except Exception as e:
            raise _remote(e) from e.__cause__ or RemoteTraceback("tb")

    ok = _doctor_gs(in_pod)[WORKER]
    assert ok.ok
    mem.fail_put = PermissionError("403 forbidden")
    bad = _doctor_gs(in_pod)[WORKER]
    assert not bad.ok and "Workload Identity" in bad.fix_hint and "403" in bad.fix_hint


def test_default_submission_runs_the_probe_through_a_cluster_with_the_profiles_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sk8s.cluster as cluster_mod

    log: dict[str, Any] = {}

    class FakeCluster:
        def __init__(self, config: Any) -> None:
            log["config"] = config

        def run(self, fn: Any, *args: Any, **kw: Any) -> Any:
            log["run"] = (fn, args, kw)
            return "ok"

        def close(self) -> None:
            log["closed"] = True

    monkeypatch.setattr(cluster_mod, "Cluster", FakeCluster)
    p = profile_for("gke", namespace="team-a", store="gs://bkt/p/", project="pr", region="us-central1")
    assert submit_probe_job(probe_store_in_pod, p) == "ok"
    cfg = log["config"]
    assert (cfg.namespace, cfg.store, cfg.service_account) == ("team-a", "gs://bkt/p/", "sk8s")
    assert cfg.image_prefix == "us-central1-docker.pkg.dev/pr/sk8s/"
    fn, args, kw = log["run"]
    assert fn is probe_store_in_pod and args == ("gs://bkt/p/",)
    assert kw["timeout"] == WORKER_PROBE_TIMEOUT
    assert log["closed"] is True


def test_default_submission_closes_the_cluster_when_the_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sk8s.cluster as cluster_mod

    closed: list[bool] = []

    class FakeCluster:
        def __init__(self, config: Any) -> None: ...

        def run(self, fn: Any, *args: Any, **kw: Any) -> Any:
            raise RuntimeError("boom")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(cluster_mod, "Cluster", FakeCluster)
    with pytest.raises(RuntimeError, match="boom"):
        submit_probe_job(probe_store_in_pod, _gs_profile())
    assert closed == [True]
