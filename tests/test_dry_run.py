"""dry_run=True: build everything a submission would send, create nothing."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from kubernetes import client

import sk8s
from sk8s import api, k8s, workflows
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import BlobTooLarge, ImageNotConfigured
from sk8s.store.configmap import MAX_CONFIGMAP_BLOB
from sk8s.store.local import LocalStore
from tests.test_cluster import Harness, _close_clusters  # noqa: F401  (autouse fixture)


class Strict:
    """An API client that fails the test on ANY use: dry_run must make no API calls."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dry_run touched the {self._label} API: {name}")


def strict_cluster(tmp_path: Path, *, inject_store: bool = True, **cfg: Any) -> Cluster:
    cfg.setdefault("image", "sk8s:dev")
    return Cluster(
        Config(namespace="ns1", **cfg),
        batch_api=Strict("batch"), core_api=Strict("core"),
        store=LocalStore(tmp_path / "store") if inject_store else None, watch=False,
    )


def nothing_written(tmp_path: Path) -> bool:
    return not (tmp_path / "store").exists() or not any((tmp_path / "store").rglob("*"))


def double(x: int) -> int:
    return x * 2


@pytest.fixture(autouse=True)
def reset_api() -> Any:
    sk8s.configure()
    yield
    sk8s.configure()


def container(body: dict[str, Any]) -> dict[str, Any]:
    c: dict[str, Any] = body["spec"]["template"]["spec"]["containers"][0]
    return c


def env(body: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container(body)["env"]}


# --- Cluster.submit ----------------------------------------------------------------------------------


def test_submit_dry_run_builds_the_job_and_touches_nothing(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = c.submit(double, 21, dry_run=True, requests={"cpu": "500m", "memory": "1Gi"},
                  name_prefix="align", backoff_limit=2)
    assert isinstance(dr, sk8s.DryRun)
    assert dr.n_jobs == 1 and len(dr.jobs) == 1
    assert dr.image == "sk8s:dev" and dr.store == "k8s" and dr.namespace == "ns1"
    assert dr.total_cpu_request == pytest.approx(0.5)
    assert dr.total_memory_request == 1024 ** 3
    body = dr.jobs[0]
    assert body["kind"] == "Job" and body["apiVersion"] == "batch/v1"
    assert body["metadata"]["name"].startswith("align-")
    assert body["metadata"]["namespace"] == "ns1"
    assert body["spec"]["backoffLimit"] == 2
    assert container(body)["image"] == "sk8s:dev"
    assert container(body)["resources"]["requests"] == {"cpu": "500m", "memory": "1Gi"}
    assert env(body)["SK8S_WORKFLOW_ID"] == dr.workflow_id
    assert env(body)["SK8S_PAYLOAD_URI"].endswith(f"{dr.workflow_id}/{body['metadata']['name']}/payload")
    assert dr.payload_bytes > 0
    assert nothing_written(tmp_path)  # no payload upload; Strict proves no API call or anchor


def test_dry_run_resolves_everything_a_real_submit_would(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path, image="cfg:img", pull_policy="Always", service_account="robot")
    dr = c.submit(double, 1, dry_run=True, image="job:img", privileged=True)
    body = dr.jobs[0]
    assert dr.image == "job:img" and container(body)["image"] == "job:img"
    assert container(body)["imagePullPolicy"] == "Always"
    assert body["spec"]["template"]["spec"]["serviceAccountName"] == "robot"
    assert container(body)["securityContext"] == {"privileged": True}
    composed = strict_cluster(tmp_path, image=None, image_prefix="reg.example/").submit(
        double, 1, dry_run=True)
    assert composed.image.startswith("reg.example/ghcr.io/jared-maguire/sk8s:")


def test_dry_run_fails_like_a_submit_when_no_image_is_configured(tmp_path: Path) -> None:
    with pytest.raises(ImageNotConfigured):  # the preview fails exactly as a real submit would
        strict_cluster(tmp_path, image=None).submit(double, 1, dry_run=True)
    assert nothing_written(tmp_path)


def test_dry_run_is_not_a_function_kwarg(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def f(**kw: Any) -> None:
        seen.append(kw)

    dr = strict_cluster(tmp_path).submit(f, scale=3, dry_run=True)
    assert dr.n_jobs == 1 and seen == []  # nothing ran, and dry_run is a job option


def test_dry_run_parses_mounts_without_reading_volumes(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = c.submit(double, 1, dry_run=True, volumes={
        "ref": {}, "out": {"access_mode": "rw", "mountpoint": "/scratch"},
    })
    spec = dr.jobs[0]["spec"]["template"]["spec"]
    claims = {v["name"]: v["persistentVolumeClaim"] for v in spec["volumes"]}
    assert claims["ref"] == {"claimName": "ref", "readOnly": True}
    assert claims["out"] == {"claimName": "out"}
    mounts = {m["name"]: m for m in container(dr.jobs[0])["volumeMounts"]}
    assert mounts["ref"]["mountPath"] == "/mnt/ref" and mounts["ref"]["readOnly"] is True
    assert mounts["out"]["mountPath"] == "/scratch"
    assert any("clone" in n for n in dr.notes)  # the read-only claim may become a hidden clone


def test_dry_run_keeps_every_validation(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    with pytest.raises(ValueError, match="access_mode"):
        c.submit(double, 1, dry_run=True, volumes={"v": {"access_mode": "wx"}})
    with pytest.raises(ValueError, match="namespace"):
        c.submit(double, 1, dry_run=True, namespace="other")
    with pytest.raises(TypeError):  # an unpicklable argument fails now, not at submit
        c.submit(double, threading.Lock(), dry_run=True)
    with pytest.raises(BlobTooLarge):  # the default ConfigMap store caps blobs, as a real put would
        strict_cluster(tmp_path, inject_store=False).submit(
            double, b"x" * (MAX_CONFIGMAP_BLOB + 10), dry_run=True)


def test_oversize_payloads_are_fine_for_object_stores(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path, store="gs://bucket/pre")
    dr = c.submit(double, b"x" * (MAX_CONFIGMAP_BLOB + 10), dry_run=True)
    assert dr.store == "gs://bucket/pre"
    assert env(dr.jobs[0])["SK8S_STORE"] == "gs://bucket/pre"


def test_dry_run_is_json_serializable(tmp_path: Path) -> None:
    dr = strict_cluster(tmp_path).submit(double, 1, dry_run=True, requests={"cpu": "2"})
    data = json.loads(json.dumps(dr.to_dict()))
    assert data["n_jobs"] == 1 and data["total_cpu_request"] == 2.0
    assert data["jobs"][0]["kind"] == "Job"


def test_dry_run_body_matches_what_a_real_submit_creates(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    opts: dict[str, Any] = dict(
        requests={"cpu": "250m"}, limits={"memory": "2Gi"}, name_prefix="same",
        volumes={"v": {"access_mode": "rw"}},
    )
    preview = h.cluster.submit(double, 1, dry_run=True, **opts).jobs[0]
    h.core.pvcs[("ns1", "v")] = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name="v", namespace="ns1"))
    real_fut = h.cluster.submit(double, 1, **opts)
    real = h.cluster.batch.jobs[("ns1", real_fut.job_name)]
    real_body = client.ApiClient().sanitize_for_serialization(real)
    assert env(preview).keys() == env(real_body).keys()
    for key in ("metadata", "spec"):
        a, b = preview[key], real_body[key]
        a, b = json.loads(json.dumps(a)), json.loads(json.dumps(b))
        for body in (a, b):  # only names / ids / uids / uris legitimately differ
            body.pop("name", None)
            body.get("labels", {}).pop("sk8s/workflow-id", None)
            body.pop("ownerReferences", None)
            for e in body.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("env", []):
                e["value"] = "*"
            body.get("template", {}).get("metadata", {}).get("labels", {}).pop("sk8s/workflow-id", None)
        assert a == b, key


# --- Cluster.run / LocalCluster ------------------------------------------------------------------------


def test_run_dry_run_returns_the_preview(tmp_path: Path) -> None:
    dr = strict_cluster(tmp_path).run(double, 1, dry_run=True)
    assert isinstance(dr, sk8s.DryRun)


def test_local_cluster_has_nothing_to_preview(tmp_path: Path) -> None:
    lc = sk8s.LocalCluster(tmp_path)
    with pytest.raises(ValueError, match="LocalCluster"):
        lc.submit(double, 1, dry_run=True)


# --- module-level API ----------------------------------------------------------------------------------


def test_module_submit_uses_the_default_cluster(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    dr = sk8s.submit(double, 1, dry_run=True)
    assert isinstance(dr, sk8s.DryRun) and dr.n_jobs == 1


@pytest.mark.parametrize("star,items", [(False, [1, 2, 3, 4, 5]), (True, [(1,), (2,), (3,), (4,), (5,)])])
def test_map_family_previews_one_body_and_counts_the_rest(
    tmp_path: Path, star: bool, items: list[Any]
) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    kw: dict[str, Any] = dict(dry_run=True, requests={"cpu": "500m", "memory": "1Gi"})
    calls = (
        [sk8s.map, sk8s.submit_map] if not star else [sk8s.starmap, sk8s.submit_starmap]
    )
    for fn in calls:
        dr = fn(double, items, **kw)
        assert isinstance(dr, sk8s.DryRun)
        assert dr.n_jobs == 5 and len(dr.jobs) == 1 and dr.n_items == 5
        assert dr.total_cpu_request == pytest.approx(2.5)
        assert dr.total_memory_request == 5 * 1024 ** 3
        assert any("only the first job" in n for n in dr.notes)
    assert nothing_written(tmp_path)


def test_map_dry_run_with_batching(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    dr = sk8s.map(double, range(10), dry_run=True, batch_size=3, requests={"cpu": "1"})
    assert dr.n_items == 10 and dr.n_jobs == 4  # ceil(10 / 3)
    assert dr.total_cpu_request == pytest.approx(4.0)
    assert any("batch" in n.lower() for n in dr.notes)
    assert len(dr.jobs) == 1


def test_map_dry_run_does_not_serialize_ten_thousand_bodies(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    dr = sk8s.map(double, range(10_000), dry_run=True)
    assert dr.n_jobs == 10_000 and len(dr.jobs) == 1


def test_map_dry_run_validates_fan_out_options(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    with pytest.raises(ValueError, match="batch_size"):
        sk8s.map(double, [1], dry_run=True, batch_size=0)
    with pytest.raises(ValueError, match="max_concurrent"):
        sk8s.map(double, [1], dry_run=True, max_concurrent=0)


def test_map_dry_run_of_nothing(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    dr = sk8s.map(double, [], dry_run=True)
    assert dr.n_jobs == 0 and dr.jobs == [] and dr.total_cpu_request == 0
    assert dr.image == "sk8s:dev"  # still resolves the context


def test_map_dry_run_accepts_a_generator_and_function_kwargs(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    dr = sk8s.map(lambda x, scale=1: x * scale, (i for i in range(4)), dry_run=True, scale=3, timeout=5)
    assert dr.n_jobs == 4


def test_without_a_default_cluster_a_throwaway_one_is_used_and_nothing_is_contacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> Any:
        raise AssertionError("dry_run contacted the cluster")

    monkeypatch.setattr(k8s, "core_api", forbidden)
    monkeypatch.setattr(k8s, "batch_api", forbidden)
    monkeypatch.setenv("SK8S_STORE", f"local:{tmp_path / 'store'}")
    sk8s.configure(namespace="ns1", image="sk8s:dev")
    dr = sk8s.map(double, [1, 2], dry_run=True)
    assert dr.n_jobs == 2 and dr.store.startswith("local:")
    assert api._default_cluster is None  # no shared Cluster (or watcher thread) was left behind
    assert nothing_written(tmp_path)


# --- submit_workflow -----------------------------------------------------------------------------------


def test_submit_workflow_dry_run(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = workflows.submit_workflow(double, 1, cluster=c, dry_run=True, requests={"cpu": "1"})
    assert isinstance(dr, sk8s.DryRun) and dr.n_jobs == 1
    assert dr.jobs[0]["metadata"]["name"].startswith("wfdriver-")
    assert any("detached" in n for n in dr.notes)
    assert nothing_written(tmp_path)
    # the cluster was not bound or consumed: it can still become a real workflow's
    c.bind_workflow("wf-later", adopt=False, detached=True)


def test_submit_workflow_dry_run_builds_its_own_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> Any:
        raise AssertionError("dry_run contacted the cluster")

    monkeypatch.setattr(k8s, "core_api", forbidden)
    monkeypatch.setattr(k8s, "batch_api", forbidden)
    monkeypatch.setenv("SK8S_STORE", f"local:{tmp_path / 'store'}")
    sk8s.configure(namespace="ns1", image="sk8s:dev")
    dr = sk8s.submit_workflow(double, 1, dry_run=True)
    assert dr.n_jobs == 1 and dr.namespace == "ns1"
    assert nothing_written(tmp_path)


# --- composed with pilot= and expect= -------------------------------------------------------------


@pytest.mark.parametrize("call,items", [
    (lambda f, items, **kw: sk8s.map(f, items, **kw), [1, 2, 3, 4]),
    (lambda f, items, **kw: sk8s.starmap(f, items, **kw), [(1,), (2,), (3,), (4,)]),
    (lambda f, items, **kw: sk8s.submit_map(f, items, **kw), [1, 2, 3, 4]),
    (lambda f, items, **kw: sk8s.submit_starmap(f, items, **kw), [(1,), (2,), (3,), (4,)]),
])
def test_dry_run_with_pilot_never_submits_or_touches_the_api(
    tmp_path: Path, call: Any, items: list[Any]
) -> None:
    # dry_run must win over pilot: a pilot would otherwise submit and WAIT for real.
    sk8s.configure(cluster=strict_cluster(tmp_path))  # Strict: any API use fails the test
    dr = call(double, items, dry_run=True, pilot=2, timeout=1)
    assert isinstance(dr, sk8s.DryRun) and dr.n_jobs == 4 and len(dr.jobs) == 1
    assert any("pilot=2" in n and "nothing was submitted" in n for n in dr.notes)
    assert nothing_written(tmp_path)


def test_dry_run_still_validates_pilot(tmp_path: Path) -> None:
    sk8s.configure(cluster=strict_cluster(tmp_path))
    with pytest.raises(ValueError, match="pilot"):
        sk8s.map(double, [1], dry_run=True, pilot=0)
    with pytest.raises(TypeError, match="pilot"):
        sk8s.map(double, [1], dry_run=True, pilot="3")


def ok(result: Any) -> bool:
    return True


def test_expect_rides_in_the_previewed_payload_and_is_validated(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    plain = c.submit(double, 1, dry_run=True)
    gated = c.submit(double, 1, dry_run=True, expect=ok)
    assert gated.payload_bytes > plain.payload_bytes
    assert "expect" not in env(gated.jobs[0])  # a payload property, not job env
    with pytest.raises(TypeError, match="expect"):
        c.submit(double, 1, dry_run=True, expect="not callable")


@pytest.mark.parametrize("batch_size", [1, 2])
def test_preview_payload_is_the_size_of_the_real_one(tmp_path: Path, batch_size: int) -> None:
    h = Harness(tmp_path)
    sk8s.configure(cluster=h.cluster)
    kw: dict[str, Any] = dict(batch_size=batch_size, expect=ok)
    preview = sk8s.map(double, [1, 2, 3, 4], dry_run=True, **kw)
    sk8s.submit_map(double, [1, 2, 3, 4], **kw)
    wf = h.cluster.workflow_id
    sizes = {len(h.store.get(h.store.uri_for(f"{wf}/{n}/payload"))) for (_ns, n) in h.batch.jobs}
    assert sizes == {preview.payload_bytes}  # honest: same call shape, expect included


# --- unset requests: the preview says the cluster, not sk8s, decides --------------------------------


def test_unset_requests_are_flagged_not_silently_zero(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = c.submit(double, 1, dry_run=True)
    assert dr.total_cpu_request == 0 and dr.total_memory_request == 0
    assert dr.unset_requests == ["cpu", "memory"]
    note = next(n for n in dr.notes if "no cpu or memory request" in n)
    # names what happens instead of leaving the zeros to be read as "free"
    assert "cluster" in note and "Autopilot" in note and "0.5 vCPU" in note and "2 GiB" in note
    assert dr.to_dict()["unset_requests"] == ["cpu", "memory"]


def test_partially_set_requests_flag_only_the_missing_one(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = c.submit(double, 1, dry_run=True, requests={"cpu": "1"})
    assert dr.unset_requests == ["memory"]
    assert any("no memory request" in n for n in dr.notes)


def test_limits_count_as_requests_for_the_unset_flag(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = c.submit(double, 1, dry_run=True, limits={"cpu": "2", "memory": "1Gi"})
    assert dr.unset_requests == [] and not any("request" in n and "no " in n for n in dr.notes)


def test_submit_workflow_preview_flags_the_driver_defaults(tmp_path: Path) -> None:
    c = strict_cluster(tmp_path)
    dr = workflows.submit_workflow(double, 1, cluster=c, dry_run=True)
    assert dr.unset_requests == ["cpu", "memory"]
    assert any("driver" in n and "Autopilot" in n for n in dr.notes)


def test_map_preview_carries_the_unset_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SK8S_STORE", f"local:{tmp_path / 'store'}")
    sk8s.configure(namespace="ns1", image="sk8s:dev")
    dr = sk8s.map(double, [1, 2, 3], dry_run=True)
    assert dr.unset_requests == ["cpu", "memory"]
