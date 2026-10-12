"""Stuck-volume diagnosis shared by the live watcher (``Cluster``) and ``sk8s.explain``.

One pure function, :func:`diagnose_pod_volumes`, decides from a pending pod's events whether a
volume wedge is persistent: event order, the "no live holder means a detach in flight" rule, the
"clone still Pending" rule and which of several claims an attach event is about all live here
and nowhere else. The two callers differ only in the callbacks they pass in:

- the watcher has a clock: its :class:`Sightings` remember when each wedge was first seen, so a
  wedge is reported only once it outlasts the configured grace period;
- ``explain`` takes a snapshot with no memory: its sightings report an infinite elapsed time, i.e.
  grace 0, so whatever is wedged *right now* is reported at once.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Callable, Protocol

from kubernetes.client import ApiException

from sk8s import clones
from sk8s.errors import PodFailed, VolumeAttachBlocked, VolumeProvisioningFailed

# Both are events, not container waiting reasons: the pod only ever says ContainerCreating /
# Pending. Match on the persistent signatures only; the transient attach noise ("CSINode ... does
# not contain driver", "ControllerPublish not permitted ... backoff condition") shares the
# FailedAttachVolume reason and must never be fatal.
ATTACH_BLOCKED = re.compile(r"Volume is already used by pod\(s\) (?P<pods>.+?)\s*$")
NO_CONTENT_SOURCE = "VolumeContentSource must be provided"
_PV_IN_EVENT = re.compile(r'volume "(?P<pv>[^"]+)"')

ATTACH = "attach"
PROVISION = "provision"


def match_pv(message: str) -> str | None:
    m = _PV_IN_EVENT.search(message or "")
    return m.group("pv") if m else None


class Sightings(Protocol):
    """How long a wedge has been seen for, keyed by (kind, key); ``forget`` when it clears."""

    def elapsed(self, kind: str, key: str) -> float: ...

    def forget(self, kind: str, key: str) -> None: ...


class SnapshotSightings:
    """No memory: every wedge counts as having lasted forever (grace 0)."""

    def elapsed(self, kind: str, key: str) -> float:
        return float("inf")

    def forget(self, kind: str, key: str) -> None:
        pass


@dataclasses.dataclass(frozen=True)
class Wedge:
    """A persistent volume problem of one pending pod."""

    kind: str  # "attach" | "no-content-source" | "clone-provisioning"
    job: str
    volume: str
    claim: str
    message: str
    holders: tuple[str, ...] = ()

    def to_error(self, *, clone_grace: float = 0.0) -> PodFailed:
        """The typed error for this wedge. ``clone_grace`` is the grace the watcher waited, for
        the clone message ("still not provisioned after Ns"); 0 reads as the infeasible-claim text."""
        if self.kind == "attach":
            return VolumeAttachBlocked(
                self.job, self.volume, self.claim, list(self.holders), self.message
            )
        grace = clone_grace if self.kind == "clone-provisioning" else 0.0
        return VolumeProvisioningFailed(
            self.job, self.volume, self.claim, self.message, clone_grace=grace
        )


def diagnose_pod_volumes(
    pod: Any,
    attach: list[Any],
    provisioning: list[Any],
    *,
    read_pvc: Callable[[str], Any | None],
    holds: Callable[[str], bool],
    sightings: Sightings,
    attach_grace: float,
    provision_grace: float,
) -> Wedge | None:
    """The persistent volume wedge ``pod`` is stuck in, or None.

    ``attach`` / ``provisioning`` are the cluster's FailedAttachVolume / ProvisioningFailed
    events; ``read_pvc(claim)`` returns the PVC or None when it cannot be read; ``holds(pod_name)``
    says whether that pod still holds its volumes (False once finished or gone).
    """
    job = pod.metadata.labels["job-name"]
    mounts = {
        v.persistent_volume_claim.claim_name: v.name
        for v in pod.spec.volumes or []
        if v.persistent_volume_claim is not None
    }
    for ev in provisioning:
        involved = ev.involved_object
        if involved.name not in mounts:
            continue
        if NO_CONTENT_SOURCE in (ev.message or ""):  # infeasible: no grace period
            return Wedge("no-content-source", job, mounts[involved.name], involved.name, ev.message)
        wedge = _diagnose_clone(
            job, mounts[involved.name], involved.name, ev.message,
            read_pvc=read_pvc, sightings=sightings, grace=provision_grace,
        )
        if wedge is not None:
            return wedge
    for ev in attach:
        if ev.involved_object.name != pod.metadata.name:
            continue
        match = ATTACH_BLOCKED.search(ev.message or "")
        if match is None:
            continue  # transient attach noise, not the persistent signature
        holders = [h for h in re.split(r"[,\s]+", match.group("pods")) if h]
        live = [h for h in holders if holds(h)]
        if not live:
            sightings.forget(ATTACH, pod.metadata.name)  # a detach in flight
            return None
        if sightings.elapsed(ATTACH, pod.metadata.name) < attach_grace:
            return None
        claim, volume = _blocked_mount(mounts, match_pv(ev.message), read_pvc)
        return Wedge("attach", job, volume, claim, ev.message, tuple(live))
    return None


def _diagnose_clone(
    job: str, volume: str, claim: str, message: str, *,
    read_pvc: Callable[[str], Any | None], sightings: Sightings, grace: float,
) -> Wedge | None:
    """A ProvisioningFailed event on a clone sk8s created: a wedge once it has stayed Pending
    for the grace period (the provisioner retries transient causes by itself)."""
    pvc = read_pvc(claim)
    if pvc is None:
        return None
    is_clone = (pvc.metadata.labels or {}).get(clones.KIND_LABEL) == clones.KIND_CLONE
    if not is_clone or pvc.status is None or pvc.status.phase != "Pending":
        sightings.forget(PROVISION, claim)  # provisioned since: the event is history
        return None
    if sightings.elapsed(PROVISION, claim) < grace:
        return None
    return Wedge("clone-provisioning", job, volume, claim, message)


def _blocked_mount(
    mounts: dict[str, str], pv: str | None, read_pvc: Callable[[str], Any | None]
) -> tuple[str, str]:
    """(claim, volume) of the mount the attach event is about: the claim bound to PV ``pv``."""
    if pv is not None and len(mounts) > 1:
        for claim, volume in mounts.items():
            pvc = read_pvc(claim)
            if pvc is not None and pvc.spec.volume_name == pv:
                return claim, volume
    return next(iter(mounts.items()))


def read_pvc(core: Any, namespace: str, claim: str) -> Any | None:
    """The PVC, or None if it cannot be read."""
    try:
        return core.read_namespaced_persistent_volume_claim(name=claim, namespace=namespace)
    except ApiException:
        return None


def pod_holds_volumes(core: Any, namespace: str, name: str) -> bool:
    """True unless the pod is over or gone (a finished pod releases its disk, spike section 7)."""
    try:
        pod = core.read_namespaced_pod(name=name, namespace=namespace)
    except ApiException as exc:
        return bool(exc.status != 404)
    phase = None if pod.status is None else pod.status.phase
    return phase not in ("Succeeded", "Failed")
