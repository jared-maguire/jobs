"""sk8s.explain: the whole picture of a workflow from cluster + store state alone."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client import ApiException

import sk8s
from sk8s import anchors, explaining, k8s
from sk8s.errors import Sk8sError
from sk8s.store.configmap import ConfigMapStore
from tests.test_cluster import Harness, _close_clusters  # noqa: F401  (autouse fixture)

NS = "ns1"
WF = "wf-abc123"
NOW = datetime.datetime(2026, 10, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)


def ago(**kw: float) -> datetime.datetime:
    return NOW - datetime.timedelta(**kw)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explaining, "_now", lambda: NOW)


# --- builders ------------------------------------------------------------------------------------


def make_anchor(h: Harness, *, detached: bool = True, age: datetime.timedelta | None = None,
                driver: str = "", wf: str = WF) -> None:
    cm = anchors.create_anchor(
        h.core, namespace=NS, workflow_id=wf, store="k8s", store_prefix=wf,
        retention_days=7, detached=detached,
    )
    cm.metadata.annotations[anchors.CREATED_AT] = (NOW - (age or datetime.timedelta(hours=3))).isoformat()
    cm.metadata.annotations[anchors.DRIVER_JOB] = driver


def make_job(h: Harness, name: str, *, wf: str = WF, active: int | None = None,
             succeeded: int | None = None, failed: int | None = None,
             start: datetime.datetime | None = None, end: datetime.datetime | None = None,
             failed_condition: str | None = None, cpu: str | None = None,
             memory: str | None = None, image: str = "sk8s:dev") -> Any:
    conditions = None
    if failed_condition:
        conditions = [client.V1JobCondition(
            type="Failed", status="True", message=failed_condition, last_transition_time=end,
        )]
    resources = client.V1ResourceRequirements(
        requests={k: v for k, v in (("cpu", cpu), ("memory", memory)) if v} or None
    )
    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name, namespace=NS, labels={anchors.WORKFLOW_LABEL: wf},
            creation_timestamp=start,
        ),
        spec=client.V1JobSpec(
            backoff_limit=0,
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(
                containers=[client.V1Container(name="worker", image=image, resources=resources)]
            )),
        ),
        status=client.V1JobStatus(
            active=active, succeeded=succeeded, failed=failed, start_time=start,
            completion_time=end if succeeded else None, conditions=conditions,
        ),
    )
    h.batch.jobs[(NS, name)] = job
    return job


def make_pod(h: Harness, job: str, *, phase: str = "Pending", wf: str = WF,
             waiting: tuple[str, str] | None = None, terminated: tuple[str, int] | None = None,
             unschedulable: str | None = None, claims: tuple[str, ...] = (),
             cpu: str | None = None, memory: str | None = None, limit_memory: str | None = None,
             start: datetime.datetime | None = None, finished: datetime.datetime | None = None,
             name: str | None = None, image: str = "sk8s:dev") -> Any:
    state = None
    if waiting:
        state = client.V1ContainerState(
            waiting=client.V1ContainerStateWaiting(reason=waiting[0], message=waiting[1])
        )
    elif terminated:
        state = client.V1ContainerState(terminated=client.V1ContainerStateTerminated(
            reason=terminated[0], exit_code=terminated[1], finished_at=finished,
        ))
    statuses = [client.V1ContainerStatus(
        name="worker", image=image, image_id="", ready=False, restart_count=0, state=state,
    )] if state else None
    conditions = None
    if unschedulable:
        conditions = [client.V1PodCondition(
            type="PodScheduled", status="False", reason="Unschedulable", message=unschedulable,
        )]
    requests = {k: v for k, v in (("cpu", cpu), ("memory", memory)) if v} or None
    limits = {"memory": limit_memory} if limit_memory else None
    pod_name = name or f"{job}-pod"
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name, namespace=NS, labels={"job-name": job, anchors.WORKFLOW_LABEL: wf},
        ),
        spec=client.V1PodSpec(
            containers=[client.V1Container(
                name="worker", image=image,
                resources=client.V1ResourceRequirements(requests=requests, limits=limits),
            )],
            volumes=[
                client.V1Volume(name=c, persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=c))
                for c in claims
            ] or None,
        ),
        status=client.V1PodStatus(
            phase=phase, container_statuses=statuses, conditions=conditions, start_time=start,
        ),
    )
    h.core.pods[(NS, pod_name)] = pod
    return pod


def make_pvc(h: Harness, name: str, *, kind: str = "volume", wf: str = WF,
             clone_of: str | None = None, phase: str = "Bound", size: str = "10Gi",
             annotations: dict[str, str] | None = None) -> None:
    labels = {"app.kubernetes.io/managed-by": "sk8s", "sk8s/kind": kind, anchors.WORKFLOW_LABEL: wf}
    if clone_of:
        labels["sk8s/clone-of"] = clone_of
    h.core.pvcs[(NS, name)] = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=name, namespace=NS, labels=labels, annotations=annotations),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"], storage_class_name="standard",
            resources=client.V1VolumeResourceRequirements(requests={"storage": size}),
        ),
        status=client.V1PersistentVolumeClaimStatus(phase=phase),
    )


def event(reason: str, obj_kind: str, obj_name: str, message: str) -> Any:
    return SimpleNamespace(
        reason=reason, message=message,
        involved_object=SimpleNamespace(kind=obj_kind, name=obj_name), involvedObject=None,
    )


def put_blobs(h: Harness, *keys: str, wf: str = WF) -> None:
    for key in keys:
        h.store.put(f"{wf}/{key}", b"x")


# --- identity, errors, shape ---------------------------------------------------------------------


def test_unknown_workflow_is_an_sk8s_error(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(Sk8sError, match="unknown workflow wf-nope"):
        sk8s.explain("wf-nope", cluster=h.cluster)


def test_non_cluster_argument_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        sk8s.explain(WF, cluster=object())


def test_healthy_detached_workflow(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="wfdriver-1", age=datetime.timedelta(hours=3, minutes=5))
    make_job(h, "wfdriver-1", active=1, start=ago(hours=3))
    make_pod(h, "wfdriver-1", phase="Running", start=ago(hours=3), cpu="500m")
    h.core.pod_logs["wfdriver-1-pod"] = "\n".join(f"line {i}" for i in range(30))
    make_job(h, "job-a", succeeded=1, start=ago(hours=2), end=ago(hours=1, minutes=30))
    make_job(h, "job-b", active=1, start=ago(minutes=10))
    make_pod(h, "job-b", phase="Running", start=ago(minutes=10))
    put_blobs(h, "wfdriver-1/payload", "job-b/payload", "job-a/result")

    report = sk8s.explain(WF, cluster=h.cluster)

    json.dumps(report)  # everything JSON-serializable
    wf = report["workflow"]
    assert wf["id"] == WF and wf["namespace"] == NS and wf["detached"] is True
    assert wf["age"] == "3h" and wf["age_seconds"] == 3 * 3600 + 5 * 60
    assert wf["created_at"] == ago(hours=3, minutes=5).isoformat()
    assert wf["anchor"]["name"] == anchors.anchor_name(WF)
    assert wf["retention_days"] == 7
    assert report["driver"]["job"] == "wfdriver-1"
    assert report["driver"]["phase"] == "running"
    assert report["driver"]["logs_tail"] == [f"line {i}" for i in range(20, 30)]
    assert report["counts"] == {
        "running": 2, "pending": 0, "succeeded": 1, "failed": 0, "total": 3,
    }
    jobs = {j["name"]: j for j in report["jobs"]}
    assert jobs["wfdriver-1"]["role"] == "driver" and jobs["job-a"]["role"] == "job"
    assert jobs["job-a"]["phase"] == "succeeded"
    assert jobs["job-a"]["duration_seconds"] == 1800
    assert jobs["job-a"]["started_at"] == ago(hours=2).isoformat()
    assert jobs["job-a"]["completed_at"] == ago(hours=1, minutes=30).isoformat()
    assert jobs["job-b"]["phase"] == "running" and jobs["job-b"]["duration_seconds"] == 600
    assert jobs["job-b"]["pods"] == [{"name": "job-b-pod", "phase": "Running"}]
    assert report["store"] == {"url": "k8s", "prefix": WF, "blob_count": 3}
    assert report["blocking"] == []
    assert report["warnings"] == []
    assert report["volumes"] == []


def test_non_detached_workflow_has_no_driver(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["workflow"]["detached"] is False
    assert report["driver"] is None
    assert report["counts"]["total"] == 0 and report["blocking"] == []


def test_explain_only_reads(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="wfdriver-1")
    make_job(h, "wfdriver-1", active=1, start=ago(hours=1))
    make_pod(h, "wfdriver-1", phase="Running")
    make_pvc(h, "vol-1")
    creates, jobs_before = list(h.core.create_calls), dict(h.batch.jobs)
    sk8s.explain(WF, cluster=h.cluster)
    assert h.core.create_calls == creates
    assert h.batch.deleted == [] and h.batch.jobs == jobs_before
    assert h.core.pvc_patches == [] and h.core.pvc_deleted == []
    assert h.core.delete_policies == {}


def test_ignores_other_workflows(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h)
    make_anchor(h, wf="wf-other")
    make_job(h, "mine", succeeded=1, start=ago(hours=1), end=ago(minutes=30))
    make_job(h, "theirs", wf="wf-other", failed=1, failed_condition="x")
    make_pvc(h, "theirs-vol", wf="wf-other")
    report = sk8s.explain(WF, cluster=h.cluster)
    assert [j["name"] for j in report["jobs"]] == ["mine"]
    assert report["volumes"] == []


# --- job phases and counts -----------------------------------------------------------------------


def test_counts_cover_every_phase(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "ok", succeeded=1, start=ago(hours=1), end=ago(minutes=30))
    make_job(h, "bad", failed=1, start=ago(hours=1), end=ago(minutes=30), failed_condition="Job has reached the specified backoff limit")
    make_job(h, "run", active=1, start=ago(minutes=5))
    make_pod(h, "run", phase="Running")
    make_job(h, "wait", active=1)
    make_pod(h, "wait", phase="Pending")
    make_job(h, "nopod", active=1)
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["counts"] == {"running": 1, "pending": 2, "succeeded": 1, "failed": 1, "total": 5}
    phases = {j["name"]: j["phase"] for j in report["jobs"]}
    assert phases == {"ok": "succeeded", "bad": "failed", "run": "running", "wait": "pending", "nopod": "pending"}


def test_failed_job_is_reported_as_blocking_with_its_reason(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "bad", failed=1, start=ago(hours=1), end=ago(minutes=30),
             failed_condition="Job has reached the specified backoff limit")
    make_pod(h, "bad", phase="Failed", terminated=("Error", 3), finished=ago(minutes=30))
    report = sk8s.explain(WF, cluster=h.cluster)
    job = report["jobs"][0]
    assert "exit code 3" in job["failure_reason"]
    assert any("bad" in b and "failed" in b and "exit code 3" in b for b in report["blocking"])


def test_oom_killed_names_the_memory_limit_and_the_fix(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "hog", failed=1, start=ago(hours=1), end=ago(minutes=30), failed_condition="backoff")
    make_pod(h, "hog", phase="Failed", terminated=("OOMKilled", 137), finished=ago(minutes=30),
             limit_memory="256Mi")
    report = sk8s.explain(WF, cluster=h.cluster)
    reason = report["jobs"][0]["failure_reason"]
    assert "out of memory" in reason and "256Mi" in reason and "limits" in reason
    assert any("hog" in b and "out of memory" in b for b in report["blocking"])


def test_failures_do_not_block_a_workflow_whose_driver_finished(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="drv")
    make_job(h, "drv", succeeded=1, start=ago(hours=2), end=ago(hours=1))
    make_job(h, "bad", failed=1, start=ago(hours=2), end=ago(hours=1), failed_condition="backoff")
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["blocking"] == []
    assert report["counts"]["failed"] == 1


# --- stuck pods, interpreted ---------------------------------------------------------------------


def test_image_pull_failure_is_interpreted(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "pull", active=1, start=ago(minutes=20), image="registry.invalid/nope:1")
    make_pod(h, "pull", phase="Pending", waiting=("ImagePullBackOff", 'Back-off pulling image "registry.invalid/nope:1"'),
             image="registry.invalid/nope:1")
    report = sk8s.explain(WF, cluster=h.cluster)
    job = report["jobs"][0]
    assert job["phase"] == "pending"
    assert job["waiting_code"] == "ImagePullBackOff"
    assert "registry.invalid/nope:1" in job["waiting_reason"]
    assert "cannot pull image" in job["waiting_reason"]
    assert any("pull" in b and "registry.invalid/nope:1" in b for b in report["blocking"])


@pytest.mark.parametrize("code,needle", [
    ("ErrImagePull", "cannot pull image"),
    ("InvalidImageName", "cannot pull image"),
    ("CreateContainerConfigError", "configuration"),
    ("CrashLoopBackOff", "crash"),
])
def test_other_waiting_reasons_are_blocking(tmp_path: Path, code: str, needle: str) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "j", active=1)
    make_pod(h, "j", phase="Pending", waiting=(code, "details here"))
    report = sk8s.explain(WF, cluster=h.cluster)
    assert needle in report["jobs"][0]["waiting_reason"].lower()
    assert "details here" in report["jobs"][0]["waiting_reason"]
    assert report["blocking"]


def test_container_creating_is_informational_not_blocking(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "j", active=1)
    make_pod(h, "j", phase="Pending", waiting=("ContainerCreating", ""))
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["jobs"][0]["waiting_code"] == "ContainerCreating"
    assert report["blocking"] == []


def test_unschedulable_pod_is_blocking(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "big", active=1)
    make_pod(h, "big", phase="Pending", unschedulable="0/1 nodes are available: 1 Insufficient cpu.")
    report = sk8s.explain(WF, cluster=h.cluster)
    job = report["jobs"][0]
    assert job["waiting_code"] == "Unschedulable"
    assert "Insufficient cpu" in job["waiting_reason"]
    assert any("big" in b and "Insufficient cpu" in b for b in report["blocking"])


def test_volume_attach_blocked_by_a_live_pod(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "reader", active=1)
    make_pod(h, "reader", phase="Pending", waiting=("ContainerCreating", ""), claims=("data",))
    make_pod(h, "holder-pod", phase="Running", name="holder-pod", wf="wf-other")
    h.core.events.append(event(
        "FailedAttachVolume", "Pod", "reader-pod",
        'Multi-Attach error for volume "pvc-1": Volume is already used by pod(s) holder-pod',
    ))
    report = sk8s.explain(WF, cluster=h.cluster)
    job = report["jobs"][0]
    assert job["waiting_code"] == "VolumeAttachBlocked"
    assert "data" in job["waiting_reason"] and "holder-pod" in job["waiting_reason"]
    assert any("holder-pod" in b for b in report["blocking"])


def test_volume_attach_event_with_a_gone_holder_is_a_detach_in_flight(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "reader", active=1)
    make_pod(h, "reader", phase="Pending", waiting=("ContainerCreating", ""), claims=("data",))
    h.core.events.append(event(
        "FailedAttachVolume", "Pod", "reader-pod",
        'Multi-Attach error: Volume is already used by pod(s) holder-pod',
    ))
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["jobs"][0]["waiting_code"] == "ContainerCreating"
    assert report["blocking"] == []


def test_transient_attach_noise_is_not_a_diagnosis(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "reader", active=1)
    make_pod(h, "reader", phase="Pending", waiting=("ContainerCreating", ""), claims=("data",))
    h.core.events.append(event(
        "FailedAttachVolume", "Pod", "reader-pod", "AttachVolume.Attach failed: CSINode does not contain driver",
    ))
    assert sk8s.explain(WF, cluster=h.cluster)["blocking"] == []


def test_content_less_claim_can_never_be_provisioned(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "reader", active=1)
    make_pod(h, "reader", phase="Pending", claims=("rox",))
    h.core.events.append(event(
        "ProvisioningFailed", "PersistentVolumeClaim", "rox",
        "rpc error: VolumeContentSource must be provided",
    ))
    report = sk8s.explain(WF, cluster=h.cluster)
    job = report["jobs"][0]
    assert job["waiting_code"] == "VolumeProvisioningFailed"
    assert "rox" in job["waiting_reason"] and "can never be provisioned" in job["waiting_reason"]
    assert report["blocking"]


def test_failing_clone_provisioning_is_blocking(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "reader", active=1)
    make_pvc(h, "ref-ro-1234", kind="volume-clone", clone_of="ref", phase="Pending")
    make_pod(h, "reader", phase="Pending", claims=("ref-ro-1234",))
    h.core.events.append(event(
        "ProvisioningFailed", "PersistentVolumeClaim", "ref-ro-1234", "quota exceeded",
    ))
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["jobs"][0]["waiting_code"] == "VolumeProvisioningFailed"
    assert "quota exceeded" in report["jobs"][0]["waiting_reason"]


def test_unreadable_events_degrade_to_a_warning(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "reader", active=1)
    make_pod(h, "reader", phase="Pending", waiting=("ContainerCreating", ""), claims=("data",))
    h.core.events_forbidden = True
    report = sk8s.explain(WF, cluster=h.cluster)
    assert any("events" in w and "sk8s config" in w for w in report["warnings"])
    assert report["jobs"][0]["waiting_code"] == "ContainerCreating"


# --- driver ----------------------------------------------------------------------------------------


def test_failed_driver_blocks_and_carries_its_log_tail(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="drv")
    make_job(h, "drv", failed=1, start=ago(hours=1), end=ago(minutes=50), failed_condition="backoff")
    make_pod(h, "drv", phase="Failed", terminated=("Error", 1), finished=ago(minutes=50))
    h.core.pod_logs["drv-pod"] = "Traceback...\nValueError: boom"
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["driver"]["phase"] == "failed"
    assert report["driver"]["logs_tail"] == ["Traceback...", "ValueError: boom"]
    assert any("driver" in b and "drv" in b and "failed" in b for b in report["blocking"])


def test_reaped_driver_is_unknown(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="drv-gone")
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["driver"]["job"] == "drv-gone"
    assert report["driver"]["phase"] == "unknown"
    assert report["driver"]["logs_tail"] == []
    assert report["blocking"] == []


def test_detached_workflow_without_a_recorded_driver_yet(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="")
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["driver"] is None


def test_unreadable_driver_logs_degrade_to_a_warning(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="drv")
    make_job(h, "drv", active=1, start=ago(hours=1))
    make_pod(h, "drv", phase="Running")

    def boom(name: str, namespace: str, **kw: Any) -> str:
        raise ApiException(status=403, reason="Forbidden")

    h.core.read_namespaced_pod_log = boom  # type: ignore[method-assign]
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["driver"]["logs_tail"] is None
    assert any("log" in w for w in report["warnings"])


# --- volumes, store, cost ---------------------------------------------------------------------------


def test_owned_volumes_and_clones(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_pvc(h, "ref", annotations={
        "sk8s/access-modes": "ReadWriteOnce,ReadOnlyMany", "sk8s/writer-generation": "job-w1",
        "writers.sk8s/job-w2": "x",
    })
    make_pvc(h, "ref-ro-ab12cd34", kind="volume-clone", clone_of="ref", size="10Gi")
    make_pvc(h, "scratch", size="1Gi", phase="Pending")
    report = sk8s.explain(WF, cluster=h.cluster)
    vols = {v["name"]: v for v in report["volumes"]}
    assert set(vols) == {"ref", "ref-ro-ab12cd34", "scratch"}
    assert vols["ref"]["kind"] == "volume" and vols["ref"]["size"] == "10Gi"
    assert vols["ref"]["writer_generation"] == "job-w1" and vols["ref"]["active_writers"] == ["job-w2"]
    assert vols["ref"]["clone_backed"] is True
    assert vols["ref-ro-ab12cd34"]["kind"] == "volume-clone" and vols["ref-ro-ab12cd34"]["clone_of"] == "ref"
    assert vols["scratch"]["phase"] == "Pending" and vols["scratch"]["clone_backed"] is False
    json.dumps(report)


def test_store_trouble_is_a_warning_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)

    def boom(prefix: str) -> int:
        raise OSError("store down")

    monkeypatch.setattr(h.store, "count_prefix", boom)
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["store"]["blob_count"] is None
    assert any("store" in w and "store down" in w for w in report["warnings"])


def test_store_without_counting_support_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)

    def unsupported(prefix: str) -> int:
        raise NotImplementedError("nope")

    monkeypatch.setattr(h.store, "count_prefix", unsupported)
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["store"]["blob_count"] is None
    assert report["warnings"] == []


def test_cost_is_rough_cpu_request_times_runtime(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    # 2 cpu for 1h (finished) + 500m for 2h (running since 2h ago) = 2 + 1 = 3 cpu-hours
    make_job(h, "done", succeeded=1, start=ago(hours=5), end=ago(hours=4))
    make_pod(h, "done", phase="Succeeded", cpu="2", start=ago(hours=5),
             terminated=("Completed", 0), finished=ago(hours=4))
    make_job(h, "live", active=1, start=ago(hours=2))
    make_pod(h, "live", phase="Running", cpu="500m", start=ago(hours=2))
    report = sk8s.explain(WF, cluster=h.cluster)
    assert report["estimated_cpu_hours"] == pytest.approx(3.0)
    assert report["estimated_cost_usd"] == pytest.approx(3.0 * explaining.CPU_HOUR_USD)
    assert report["estimated_cost_label"] == "rough"
    assert "$" in report["estimated_cost_basis"] or "USD" in report["estimated_cost_basis"]


def test_no_cost_when_nothing_requests_cpu(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "j", active=1, start=ago(hours=1))
    make_pod(h, "j", phase="Running", start=ago(hours=1))
    report = sk8s.explain(WF, cluster=h.cluster)
    for key in ("estimated_cost_usd", "estimated_cpu_hours", "estimated_cost_label"):
        assert key not in report


def test_limits_only_pods_cost_by_their_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "j", active=1, start=ago(hours=1))
    pod = make_pod(h, "j", phase="Running", start=ago(hours=1))
    pod.spec.containers[0].resources.limits = {"cpu": "1"}
    assert sk8s.explain(WF, cluster=h.cluster)["estimated_cpu_hours"] == pytest.approx(1.0)


# --- cluster=None ------------------------------------------------------------------------------------


def test_cluster_none_builds_read_only_handles_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "j", succeeded=1, start=ago(hours=1), end=ago(minutes=30))
    ConfigMapStore(NS, h.core).put(f"{WF}/j/result", b"x")  # the anchor's store is "k8s"
    monkeypatch.setattr(k8s, "core_api", lambda: h.core)
    monkeypatch.setattr(k8s, "batch_api", lambda: h.batch)
    sk8s.configure(namespace=NS)
    try:
        report = sk8s.explain(WF)
    finally:
        sk8s.configure()
    assert report["workflow"]["id"] == WF
    assert report["counts"]["succeeded"] == 1
    assert report["store"]["blob_count"] == 1  # opened from the anchor's recorded store ("k8s")


# --- human rendering ---------------------------------------------------------------------------------


def test_render_healthy(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, driver="wfdriver-1")
    make_job(h, "wfdriver-1", active=1, start=ago(hours=3))
    make_pod(h, "wfdriver-1", phase="Running")
    h.core.pod_logs["wfdriver-1-pod"] = "hello from the driver"
    text = explaining.render(sk8s.explain(WF, cluster=h.cluster))
    assert WF in text and "detached" in text and "3h" in text
    assert "wfdriver-1" in text and "running" in text
    assert "hello from the driver" in text
    assert "nothing is blocking" in text


def test_render_unhealthy_lists_blocking_and_reasons(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    make_job(h, "pull", active=1, image="nope:1")
    make_pod(h, "pull", phase="Pending", waiting=("ImagePullBackOff", "Back-off"), image="nope:1")
    make_pvc(h, "vol-1")
    text = explaining.render(sk8s.explain(WF, cluster=h.cluster))
    assert "blocking" in text and "nothing is blocking" not in text
    assert "nope:1" in text and "ImagePullBackOff" in text
    assert "vol-1" in text


def test_render_truncates_huge_job_lists(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    make_anchor(h, detached=False)
    for i in range(60):
        make_job(h, f"ok-{i:03d}", succeeded=1, start=ago(hours=2), end=ago(hours=1))
    make_job(h, "bad", failed=1, start=ago(hours=2), end=ago(hours=1), failed_condition="backoff")
    text = explaining.render(sk8s.explain(WF, cluster=h.cluster))
    assert "bad" in text  # the interesting ones are always shown
    assert "more" in text and text.count("ok-") < 60
