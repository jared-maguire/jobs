"""RF3: the two misleading volume failures are typed, hinted, and surface in bounded time.

Signatures are the spike's verbatim captures (spike-report.md section 11):
1. ``FailedAttachVolume`` / ``Waiting for detach ... Volume is already used by pod(s) X`` on the
   pod: ContainerCreating forever, no timeout.
2. ``ProvisioningFailed`` / ``... VolumeContentSource must be provided when AccessMode is set to
   read only`` on the PVC: the pod only says "didn't find available persistent volumes to bind".
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from sk8s import clones
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import (
    ImagePullError, PodFailed, VolumeAttachBlocked, VolumeProvisioningFailed,
)
from sk8s.store.local import LocalStore
from sk8s.watch import TERMINAL
from tests.fakes import FakeBatchV1, FakeCoreV1

NS = "ns1"

# Verbatim from the spike (pod names and uids are the captured ones).
ATTACH_MSG = (
    'Waiting for detach for volume "pvc-43dee828-dc4f-445e-a9c8-7b7dcf58dfb1" '
    "Volume is already used by pod(s) sk8s-rox-r1-ro1"
)
NO_SOURCE_MSG = (
    "Volume provisioning failed with infeasible error. Retries will be delayed. rpc error: "
    "code = InvalidArgument desc = VolumeContentSource must be provided when AccessMode is set "
    "to read only"
)
NOISE = [
    'AttachVolume.Attach failed for volume "pvc-1" : CSINode gk3-autopilot-cluster-1-pool-1-x '
    "does not contain driver pd.csi.storage.gke.io",
    'AttachVolume.Attach failed for volume "pvc-1" : rpc error: code = InvalidArgument desc = '
    'ControllerPublish not permitted on node "projects/p/zones/z/instances/gk3-x" due to '
    "backoff condition",
]
GRACE = 300.0  # Config.volume_attach_grace_seconds default


def event(reason: str, message: str, kind: str, name: str) -> Any:
    return SimpleNamespace(
        reason=reason, message=message, type="Warning",
        involved_object=SimpleNamespace(kind=kind, name=name),
    )


class H:
    def __init__(self, tmp_path: Any) -> None:
        self.core = FakeCoreV1()
        self.batch = FakeBatchV1()
        self.now = 1000.0
        self.cluster = Cluster(
            Config(namespace=NS, image="sk8s:dev", store=f"local:{tmp_path}"),
            batch_api=self.batch, core_api=self.core, store=LocalStore(tmp_path),
            watcher_events=lambda: iter(()),
        )
        self.cluster._clock = lambda: self.now

    def job_with_pending_pod(
        self, *, claim: str = "idx", volume: str = "idx", phase: str = "Pending"
    ) -> tuple[Any, str]:
        fut = self.cluster.submit(lambda: 1)
        pod_name = f"{fut.job_name}-xyz"
        self.core.pods[(NS, pod_name)] = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name, namespace=NS,
                labels={"job-name": fut.job_name, "app.kubernetes.io/managed-by": "sk8s"},
            ),
            spec=client.V1PodSpec(
                containers=[client.V1Container(name="worker")],
                volumes=[client.V1Volume(
                    name=volume,
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=claim),
                )],
            ),
            status=client.V1PodStatus(phase=phase),
        )
        return fut, pod_name

    def holder(self, name: str, phase: str) -> None:
        self.core.pods[(NS, name)] = client.V1Pod(
            metadata=client.V1ObjectMeta(name=name, namespace=NS, labels={}),
            spec=client.V1PodSpec(containers=[client.V1Container(name="c")]),
            status=client.V1PodStatus(phase=phase),
        )

    def probe(self) -> list[tuple[str, str]]:
        return list(self.cluster._volume_failures())


@pytest.fixture
def h(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0)
    harness = H(tmp_path)
    yield harness
    harness.cluster.close()


# --- the watcher knows the new phase ------------------------------------------------------------------


def test_volume_stuck_is_a_terminal_watcher_phase() -> None:
    assert "volume-stuck" in TERMINAL


# --- signature 2: a reader fleet / rw fan-out blocked on a ReadWriteOnce disk ------------------------------------


def test_attach_block_is_not_reported_before_the_grace_period(h: H) -> None:
    fut, pod = h.job_with_pending_pod()
    h.holder("sk8s-rox-r1-ro1", "Running")
    h.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
    assert h.probe() == []  # first sighting: starts the clock
    h.now += GRACE - 1
    assert h.probe() == []  # still within the window a normal detach can take (spike: ~15s)


def test_attach_block_surfaces_as_a_typed_error_after_the_grace_period(h: H) -> None:
    fut, pod = h.job_with_pending_pod(claim="idx", volume="idx")
    h.holder("sk8s-rox-r1-ro1", "Running")
    h.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
    h.probe()
    h.now += GRACE
    assert h.probe() == [(fut.job_name, "volume-stuck")]
    h.cluster._on_terminal(fut.job_name, "volume-stuck")  # what the watcher does with the event
    err = fut.exception(timeout=5)
    assert isinstance(err, VolumeAttachBlocked) and isinstance(err, PodFailed)
    assert err.job_name == fut.job_name
    assert err.volume == "idx" and err.holders == ["sk8s-rox-r1-ro1"]
    text = str(err)
    assert "Waiting for detach" in text and "already used by pod(s) sk8s-rox-r1-ro1" in text  # verbatim
    assert "sk8s-rox-r1-ro1" in text and "'idx'" in text
    assert 'access_modes=["ReadWriteOnce", "ReadOnlyMany"]' in text  # the fix for a reader fleet
    assert "one node" in text and "no timeout of its own" in text
    assert fut.job_name in h.batch.deleted  # the wedged Job is removed: it must not run later


def test_the_error_names_every_holder(h: H) -> None:
    fut, pod = h.job_with_pending_pod()
    h.holder("a-1", "Running")
    h.holder("b-2", "Pending")
    msg = 'Waiting for detach for volume "pvc-1" Volume is already used by pod(s) a-1, b-2'
    h.core.events.append(event("FailedAttachVolume", msg, "Pod", pod))
    h.probe()
    h.now += GRACE
    h.probe()
    h.cluster._on_terminal(fut.job_name, "volume-stuck")
    err = fut.exception(timeout=5)
    assert isinstance(err, VolumeAttachBlocked) and err.holders == ["a-1", "b-2"]


@pytest.mark.parametrize("holder_phase", ["Succeeded", "Failed", None])
def test_a_finished_or_vanished_holder_is_a_normal_detach_not_a_wedge(h: H, holder_phase: str | None) -> None:
    fut, pod = h.job_with_pending_pod()
    if holder_phase is not None:
        h.holder("sk8s-rox-r1-ro1", holder_phase)  # None: the pod is gone (404)
    h.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
    h.probe()
    h.now += 10 * GRACE
    assert h.probe() == []


def test_the_clock_restarts_when_the_holder_goes_away_and_comes_back(h: H) -> None:
    fut, pod = h.job_with_pending_pod()
    h.holder("sk8s-rox-r1-ro1", "Running")
    h.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
    h.probe()
    h.now += GRACE - 1
    h.core.pods[(NS, "sk8s-rox-r1-ro1")].status.phase = "Succeeded"
    assert h.probe() == []  # released: forget the sighting
    h.core.pods[(NS, "sk8s-rox-r1-ro1")].status.phase = "Running"
    h.now += 2
    assert h.probe() == []  # a fresh window, not the old one


def test_a_stale_event_on_a_pod_that_is_now_running_is_ignored(h: H) -> None:
    fut, pod = h.job_with_pending_pod(phase="Running")
    h.holder("sk8s-rox-r1-ro1", "Running")
    h.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
    h.now += 10 * GRACE
    assert h.probe() == [] and h.probe() == []


@pytest.mark.parametrize("message", NOISE)
def test_transient_attach_noise_is_never_fatal(h: H, message: str) -> None:
    fut, pod = h.job_with_pending_pod()
    # Even a live pod that shares a word with the message must not turn noise into a diagnosis.
    for word in message.replace('"', " ").split():
        h.holder(word, "Running")
    h.core.events.append(event("FailedAttachVolume", message, "Pod", pod))
    for _ in range(5):
        h.now += GRACE
        assert h.probe() == []


def test_a_pod_without_volumes_costs_no_event_calls(h: H) -> None:
    fut = h.cluster.submit(lambda: 1)
    h.core.pods[(NS, "p")] = client.V1Pod(
        metadata=client.V1ObjectMeta(name="p", labels={"job-name": fut.job_name}),
        spec=client.V1PodSpec(containers=[client.V1Container(name="w")]),
        status=client.V1PodStatus(phase="Pending"),
    )
    calls: list[str] = []
    h.core.list_namespaced_event = lambda **kw: calls.append("x")  # type: ignore[method-assign,assignment]
    assert h.probe() == [] and calls == []


def test_other_workflows_pods_are_not_diagnosed(h: H) -> None:
    fut, pod = h.job_with_pending_pod()
    h.core.pods[(NS, pod)].metadata.labels["job-name"] = "someone-elses-job"
    h.holder("sk8s-rox-r1-ro1", "Running")
    h.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
    h.probe()
    h.now += GRACE
    assert h.probe() == []


# --- signature 1: a ROX claim with no content source -----------------------------------------------------------


def test_unprovisionable_pvc_is_reported_at_once_with_the_pvcs_own_words(h: H) -> None:
    fut, pod = h.job_with_pending_pod(claim="idx-ro", volume="idx")
    h.core.events.append(event("ProvisioningFailed", NO_SOURCE_MSG, "PersistentVolumeClaim", "idx-ro"))
    assert h.probe() == [(fut.job_name, "volume-stuck")]  # infeasible: no grace period
    h.cluster._on_terminal(fut.job_name, "volume-stuck")
    err = fut.exception(timeout=5)
    assert isinstance(err, VolumeProvisioningFailed) and isinstance(err, PodFailed)
    assert err.volume == "idx" and err.claim == "idx-ro"
    text = str(err)
    assert "VolumeContentSource must be provided when AccessMode is set to read only" in text
    assert "idx-ro" in text and "clone" in text
    assert 'access_modes=["ReadWriteOnce", "ReadOnlyMany"]' in text
    assert fut.job_name in h.batch.deleted


def test_other_provisioning_failures_are_left_alone(h: H) -> None:
    fut, pod = h.job_with_pending_pod(claim="idx", volume="idx")
    h.core.events.append(event(
        "ProvisioningFailed", "rpc error: code = ResourceExhausted desc = quota exceeded",
        "PersistentVolumeClaim", "idx",
    ))
    assert h.probe() == []


def test_a_pvc_event_for_somebody_elses_claim_is_ignored(h: H) -> None:
    fut, pod = h.job_with_pending_pod(claim="idx", volume="idx")
    h.core.events.append(event("ProvisioningFailed", NO_SOURCE_MSG, "PersistentVolumeClaim", "other"))
    assert h.probe() == []


# --- robustness ---------------------------------------------------------------------------------------------------


def test_forbidden_events_degrade_to_no_diagnosis_without_noise(h: H, caplog: Any) -> None:
    fut, pod = h.job_with_pending_pod()

    def forbidden(**_: Any) -> Any:
        raise ApiException(status=403, reason="Forbidden")

    h.core.list_namespaced_event = forbidden  # type: ignore[method-assign,assignment]
    for _ in range(3):
        assert h.probe() == []
    assert len([r for r in caplog.records if "events" in r.getMessage()]) == 1  # warned once


def test_a_broken_volume_probe_never_wedges_the_event_stream(h: H) -> None:
    fut, pod = h.job_with_pending_pod()

    def boom(**_: Any) -> Any:
        raise RuntimeError("api down")

    h.core.list_namespaced_event = boom  # type: ignore[method-assign,assignment]
    assert h.probe() == []


def test_probe_runs_alongside_the_image_pull_probe(h: H) -> None:
    fut, pod = h.job_with_pending_pod(claim="idx-ro", volume="idx")
    h.core.events.append(event("ProvisioningFailed", NO_SOURCE_MSG, "PersistentVolumeClaim", "idx-ro"))
    assert list(h.cluster._probe_failures()) == [(fut.job_name, "volume-stuck")]


def test_a_stuck_phase_without_a_recorded_diagnosis_is_still_a_pod_failure(h: H) -> None:
    fut = h.cluster.submit(lambda: 1)
    h.cluster._on_terminal(fut.job_name, "volume-stuck")
    err = fut.exception(timeout=5)
    assert isinstance(err, PodFailed) and not isinstance(err, ImagePullError)


def test_a_stuck_writer_never_becomes_a_generation(h: H) -> None:
    h.core.pvcs[(NS, "idx")] = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name="idx", namespace=NS,
            annotations={clones.ACCESS_MODES_ANNOTATION: "ReadWriteOnce,ReadOnlyMany"},
        ),
        spec=client.V1PersistentVolumeClaimSpec(access_modes=["ReadWriteOnce"]),
    )
    fut = h.cluster.submit(lambda: 1, volumes={"idx": {"access_mode": "rw"}})
    assert clones.WRITER_MARKER_PREFIX + fut.job_name in h.core.pvcs[(NS, "idx")].metadata.annotations
    h.cluster._on_terminal(fut.job_name, "volume-stuck")  # it never ran
    ann = h.core.pvcs[(NS, "idx")].metadata.annotations
    assert clones.WRITER_GENERATION_ANNOTATION not in ann
    assert not [k for k in ann if k.startswith(clones.WRITER_MARKER_PREFIX)]


def test_the_event_stream_runs_the_volume_probe_on_connect(h: H) -> None:
    """Wired into _k8s_events itself, so a wedge surfaces without any other trigger."""
    fut, pod = h.job_with_pending_pod(claim="idx-ro", volume="idx")
    h.core.events.append(event("ProvisioningFailed", NO_SOURCE_MSG, "PersistentVolumeClaim", "idx-ro"))
    gen = h.cluster._k8s_events()
    seen = [next(gen) for _ in range(2)]  # the LIST replay of the job, then the probe
    gen.close()
    assert (fut.job_name, "volume-stuck") in seen
