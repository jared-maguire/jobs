"""``dry_run=True``: the result of building a submission without sending any of it."""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from kubernetes import client

from sk8s.budget import BudgetVerdict
from sk8s.quantity import cpu_cores, effective_request, memory_bytes


@dataclasses.dataclass
class DryRun:
    """What a submission WOULD do. Nothing was created: no anchor, no payload upload, no API call.

    ``jobs`` holds fully serialized ``V1Job`` bodies (plain dicts, as the API would receive
    them). For ``map``-style fan-outs it holds ONE representative body (the first job's);
    ``n_jobs`` and the totals cover the whole fan-out (with ``batch_size`` a job carries several
    items, so ``n_jobs`` is the number of batches). Names, payload/result keys and ids differ
    per job and per real submission; the owner reference and ``SK8S_ANCHOR_*`` env of the
    workflow anchor show a placeholder uid, because the anchor does not exist yet.

    ``total_cpu_request`` is in cores, ``total_memory_request`` in bytes; an unset request
    counts as 0 (a job with only limits is costed by its limits, as Kubernetes defaults
    requests to limits), and ``unset_requests`` names which of "cpu"/"memory" the job asks
    nothing for: sk8s adds no defaults of its own, so the CLUSTER decides what such a pod gets
    (GKE Autopilot: 0.5 vCPU and 2 GiB; a plain cluster: best-effort), which a preview cannot
    see without contacting it. ``payload_bytes`` is the pickled size of the representative job's
    function and arguments. ``budget`` is the budget verdict (see :mod:`sk8s.budget`).
    """

    jobs: list[dict[str, Any]]
    image: str
    store: str
    namespace: str
    n_jobs: int
    total_cpu_request: float
    total_memory_request: int
    workflow_id: str = ""
    n_items: int | None = None
    payload_bytes: int = 0
    unset_requests: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    # Staging, from the Artifacts' declared metadata only (a preview pins and hashes nothing):
    n_artifacts: int = 0  # distinct Artifact arguments of the representative job
    artifact_input_bytes: int | None = None  # their summed declared sizes; None if none declared
    # The active Budget measured against this preview (None when no budget applies). A preview
    # REPORTS the verdict; a real submission with the same numbers raises BudgetExceeded.
    budget: BudgetVerdict | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def serialize_job(job: Any) -> dict[str, Any]:
    """A client model as the plain dict the API server would be sent."""
    body: dict[str, Any] = client.ApiClient().sanitize_for_serialization(job)  # type: ignore[no-untyped-call]
    return body


def resource_totals(
    requests: Mapping[str, Any] | None, limits: Mapping[str, Any] | None
) -> tuple[float, int]:
    """(cpu cores, memory bytes) a job's requests (defaulting to its limits) ask for."""
    cpu = cpu_cores(effective_request(requests, limits, "cpu")) or 0.0
    memory = memory_bytes(effective_request(requests, limits, "memory")) or 0
    return cpu, memory


def unset_requests(requests: Mapping[str, Any] | None, limits: Mapping[str, Any] | None) -> list[str]:
    """Which of ``cpu``/``memory`` neither a request nor a limit sets (the cluster fills those in)."""
    return [r for r in ("cpu", "memory") if effective_request(requests, limits, r) is None]


def unset_requests_note(unset: list[str]) -> str:
    what = " or ".join(unset) if len(unset) == 2 else unset[0]
    return (
        f"no {what} request is set, so the totals count it as 0, but a pod is never free: sk8s adds "
        "no defaults, so the cluster decides (GKE Autopilot assigns 0.5 vCPU and 2 GiB; a plain "
        "cluster schedules it best-effort). Set requests= to make the preview and the schedule exact"
    )
