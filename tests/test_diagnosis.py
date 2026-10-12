"""sk8s.diagnosis: the one copy of the stuck-volume rules, run by the watcher and by explain."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kubernetes import client

from sk8s.diagnosis import (
    ATTACH, PROVISION, SnapshotSightings, Wedge, diagnose_pod_volumes,
)
from sk8s.errors import VolumeAttachBlocked, VolumeProvisioningFailed


class Clock:
    """Sightings with a settable 'time since first seen'; records forgets."""

    def __init__(self, elapsed: float = 0.0) -> None:
        self.value = elapsed
        self.forgot: list[tuple[str, str]] = []

    def elapsed(self, kind: str, key: str) -> float:
        return self.value

    def forget(self, kind: str, key: str) -> None:
        self.forgot.append((kind, key))


def pod(*claims: str) -> Any:
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name="p-1", labels={"job-name": "job-1"}),
        spec=client.V1PodSpec(containers=[], volumes=[
            client.V1Volume(name=f"vol-{c}", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=c))
            for c in claims
        ]),
    )


def ev(kind: str, name: str, message: str) -> Any:
    return SimpleNamespace(involved_object=SimpleNamespace(kind=kind, name=name), message=message)


def pvc(*, clone: bool, phase: str, volume_name: str | None = None) -> Any:
    return client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(labels={"sk8s/kind": "volume-clone"} if clone else {}),
        spec=client.V1PersistentVolumeClaimSpec(volume_name=volume_name),
        status=client.V1PersistentVolumeClaimStatus(phase=phase),
    )


def run(p: Any, attach: list[Any], prov: list[Any], *, pvcs: dict[str, Any] | None = None,
        live: set[str] = frozenset(), sightings: Any = None, ag: float = 300.0, pg: float = 300.0) -> Wedge | None:  # type: ignore[assignment]
    table = pvcs or {}
    return diagnose_pod_volumes(
        p, attach, prov, read_pvc=table.get, holds=lambda n: n in live,
        sightings=sightings if sightings is not None else SnapshotSightings(),
        attach_grace=ag, provision_grace=pg,
    )


ATTACH_MSG = 'Multi-Attach error for volume "pv-b": Volume is already used by pod(s) h1, h2'


def test_attach_needs_a_live_holder_and_the_grace_to_pass() -> None:
    p = pod("a")
    events = [ev("Pod", "p-1", ATTACH_MSG)]
    clock = Clock(10.0)
    assert run(p, events, [], live={"h2"}, sightings=clock) is None  # inside the grace
    clock.value = 400.0
    wedge = run(p, events, [], live={"h2"}, sightings=clock)
    assert wedge is not None and wedge.kind == "attach" and wedge.holders == ("h2",)  # only live ones
    gone = Clock(400.0)
    assert run(p, events, [], live=set(), sightings=gone) is None  # a detach in flight
    assert gone.forgot == [(ATTACH, "p-1")]
    assert isinstance(wedge.to_error(), VolumeAttachBlocked)


def test_transient_attach_noise_and_other_pods_events_are_ignored() -> None:
    p = pod("a")
    assert run(p, [ev("Pod", "p-1", "CSINode does not contain driver")], [], live={"h"}) is None
    assert run(p, [ev("Pod", "someone-else", ATTACH_MSG)], [], live={"h1"}) is None


def test_snapshot_mode_reports_a_live_holder_at_once() -> None:
    wedge = run(pod("a"), [ev("Pod", "p-1", ATTACH_MSG)], [], live={"h1"})
    assert wedge is not None and wedge.holders == ("h1",)


def test_attach_event_is_attributed_to_the_claim_bound_to_the_named_pv() -> None:
    p = pod("a", "b")
    pvcs = {"a": pvc(clone=False, phase="Bound", volume_name="pv-a"),
            "b": pvc(clone=False, phase="Bound", volume_name="pv-b")}
    wedge = run(p, [ev("Pod", "p-1", ATTACH_MSG)], [], pvcs=pvcs, live={"h1"})
    assert wedge is not None and (wedge.claim, wedge.volume) == ("b", "vol-b")
    fallback = run(p, [ev("Pod", "p-1", ATTACH_MSG)], [], pvcs={}, live={"h1"})
    assert fallback is not None and fallback.claim == "a"  # unreadable PVCs: first mount


def test_content_less_claim_is_fatal_without_grace() -> None:
    p = pod("rox")
    wedge = run(p, [], [ev("PersistentVolumeClaim", "rox", "rpc: VolumeContentSource must be provided")],
                sightings=Clock(0.0))
    assert wedge is not None and wedge.kind == "no-content-source"
    err = wedge.to_error(clone_grace=300.0)
    assert isinstance(err, VolumeProvisioningFailed) and "can never be provisioned" in str(err)


def test_clone_must_be_pending_and_outlast_the_grace() -> None:
    p = pod("c")
    prov = [ev("PersistentVolumeClaim", "c", "quota exceeded")]
    clock = Clock(10.0)
    assert run(p, [], prov, pvcs={"c": pvc(clone=True, phase="Pending")}, sightings=clock) is None
    clock.value = 400.0
    wedge = run(p, [], prov, pvcs={"c": pvc(clone=True, phase="Pending")}, sightings=clock)
    assert wedge is not None and wedge.kind == "clone-provisioning"
    assert "after 300s" in str(wedge.to_error(clone_grace=300.0))
    bound = Clock(400.0)
    assert run(p, [], prov, pvcs={"c": pvc(clone=True, phase="Bound")}, sightings=bound) is None
    assert bound.forgot == [(PROVISION, "c")]  # provisioned since: the event is history
    not_ours = run(p, [], prov, pvcs={"c": pvc(clone=False, phase="Pending")}, sightings=Clock(400.0))
    assert not_ours is None  # a claim sk8s did not clone is not ours to call wedged
    assert run(p, [], prov, pvcs={}, sightings=Clock(400.0)) is None  # unreadable claim


def test_provisioning_events_for_unmounted_claims_are_ignored() -> None:
    assert run(pod("a"), [], [ev("PersistentVolumeClaim", "other", "VolumeContentSource must be provided")]) is None


def test_provisioning_is_judged_before_attach() -> None:
    p = pod("rox")
    wedge = run(p, [ev("Pod", "p-1", ATTACH_MSG)],
                [ev("PersistentVolumeClaim", "rox", "VolumeContentSource must be provided")], live={"h1"})
    assert wedge is not None and wedge.kind == "no-content-source"
