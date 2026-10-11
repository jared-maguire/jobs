"""Build Kubernetes objects with client models. No YAML, no templates."""
from __future__ import annotations

import random
import re
import string

from typing import Any

from kubernetes import client

MANAGED_BY = "sk8s"


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
    volumes: dict[str, str],
    backoff_limit: int,
    ttl: int,
    privileged: bool,
    owner: Any | None = None,
    anchor_name: str | None = None,
    anchor_uid: str | None = None,
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
            client.V1VolumeMount(name=pvc, mount_path=path)
            for pvc, path in volumes.items()
        ] or None,
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        service_account_name=service_account,
        containers=[container],
        volumes=[
            client.V1Volume(
                name=pvc,
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=pvc
                ),
            )
            for pvc in volumes
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
