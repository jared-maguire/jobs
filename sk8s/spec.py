"""Build Kubernetes objects with client models. No YAML, no templates."""
from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass
from typing import Any, Sequence

from kubernetes import client

MANAGED_BY = "sk8s"


@dataclass(frozen=True)
class PodMount:
    """One PVC mount in a job's pod, after ``sk8s.volumes`` has resolved the claim.

    ``name`` is the logical volume name (used for the pod volume, so it stays a valid DNS
    label even when ``claim`` is a longer hidden clone name); ``claim`` is the PVC actually
    claimed. ``read_only`` sets ``readOnly: true`` on BOTH the pod volume and the volume
    mount (the GKE documentation asks for both); a read-write mount sets neither, which
    leaves the built Job exactly as it was before mounts had modes.
    """

    name: str
    claim: str
    mountpoint: str
    read_only: bool


def job_name(prefix: str) -> str:
    clean = re.sub(r"[^a-z0-9-]+", "-", prefix.lower()).strip("-") or "job"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{clean[:56]}-{suffix}"


def build_job(
    name: str,
    *,
    image: str,
    namespace: str,
    workflow_id: str,
    payload_uri: str,
    result_key: str,
    store: str,
    service_account: str,
    pull_policy: str,
    requests: dict[str, str],
    limits: dict[str, str],
    volumes: Sequence[PodMount] = (),
    backoff_limit: int,
    ttl: int,
    privileged: bool,
    owner: Any | None = None,
    anchor_name: str | None = None,
    anchor_uid: str | None = None,
    store_endpoint: str | None = None,
    store_env: dict[str, str] | None = None,
    budget_json: str | None = None,
    spot: bool = False,
) -> client.V1Job:
    labels = {"app.kubernetes.io/managed-by": MANAGED_BY, "sk8s/workflow-id": workflow_id}
    env = [
        client.V1EnvVar(name="SK8S_STORE", value=store),
        client.V1EnvVar(name="SK8S_NAMESPACE", value=namespace),
        client.V1EnvVar(name="SK8S_PAYLOAD_URI", value=payload_uri),
        client.V1EnvVar(name="SK8S_RESULT_KEY", value=result_key),
        client.V1EnvVar(name="SK8S_WORKFLOW_ID", value=workflow_id),
        # Nested submissions from inside the pod build their Cluster from SK8S_* env, so
        # they must inherit how this pod itself was launched.
        client.V1EnvVar(name="SK8S_IMAGE", value=image),
        client.V1EnvVar(name="SK8S_PULL_POLICY", value=pull_policy),
        client.V1EnvVar(name="SK8S_SERVICE_ACCOUNT", value=service_account),
    ]
    if anchor_name and anchor_uid:  # lets the worker own its result blob via the anchor
        env.append(client.V1EnvVar(name="SK8S_ANCHOR_NAME", value=anchor_name))
        env.append(client.V1EnvVar(name="SK8S_ANCHOR_UID", value=anchor_uid))
    if store_endpoint:
        env.append(client.V1EnvVar(name="SK8S_S3_ENDPOINT", value=store_endpoint))
        # Nested clusters rebuild their Config from SK8S_* env (load_config's generic layer),
        # so they need the config-level name too to propagate the endpoint onward.
        env.append(client.V1EnvVar(name="SK8S_STORE_ENDPOINT", value=store_endpoint))
    if budget_json:
        # Nested submissions in the pod (a detached workflow's driver) rebuild their Config from
        # SK8S_* env, so the budget rides along and each of them is admitted against it.
        env.append(client.V1EnvVar(name="SK8S_BUDGET", value=budget_json))
    for key, value in (store_env or {}).items():
        env.append(client.V1EnvVar(name=key, value=value))
    resources = None
    if requests or limits:
        resources = client.V1ResourceRequirements(
            requests=requests or None, limits=limits or None
        )
    container = client.V1Container(
        name="worker",
        image=image,
        image_pull_policy=pull_policy,
        command=["python", "-m", "sk8s.worker"],
        env=env,
        resources=resources,
        security_context=(
            client.V1SecurityContext(privileged=True) if privileged else None
        ),
        volume_mounts=[
            client.V1VolumeMount(
                name=m.name, mount_path=m.mountpoint, read_only=True if m.read_only else None
            )
            for m in volumes
        ] or None,
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        service_account_name=service_account,
        # Spot placement: the nodeSelector is what GKE Autopilot keys on (it injects the
        # matching toleration itself); the explicit toleration also covers Standard
        # clusters with spot node pools. Preemption can kill the pod at any moment, which
        # is why cluster.py refuses spot without retries.
        node_selector={"cloud.google.com/gke-spot": "true"} if spot else None,
        tolerations=[client.V1Toleration(
            key="cloud.google.com/gke-spot", operator="Equal", value="true",
            effect="NoSchedule",
        )] if spot else None,
        containers=[container],
        volumes=[
            client.V1Volume(
                name=m.name,
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=m.claim, read_only=True if m.read_only else None
                ),
            )
            for m in volumes
        ] or None,
    )
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=name, namespace=namespace, labels=labels,
            owner_references=[owner] if owner is not None else None,
        ),
        spec=client.V1JobSpec(
            backoff_limit=backoff_limit,
            ttl_seconds_after_finished=ttl,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec
            ),
        ),
    )
