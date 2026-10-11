"""Deploy (or reuse) a single-pod MinIO in the cluster for S3 integration tests.

Dev tooling only; uses the kubernetes client, no YAML. Idempotent: if the
Deployment/Service already exist they are left alone.
"""
from __future__ import annotations

import os
import time
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

NAME = "minio"
PORT = 9000
ACCESS_KEY = "minioadmin"
SECRET_KEY = "minioadmin"
# minio/minio is no longer published on Docker Hub or quay.io (MinIO went source-only in
# late 2025), so use Chainguard's public build. Override with SK8S_TEST_MINIO_IMAGE.
IMAGE = os.environ.get("SK8S_TEST_MINIO_IMAGE", "cgr.dev/chainguard/minio:latest")


def _deployment(namespace: str) -> client.V1Deployment:
    labels = {"app": NAME}
    container = client.V1Container(
        name=NAME,
        image=IMAGE,
        image_pull_policy="IfNotPresent",
        volume_mounts=[client.V1VolumeMount(name="data", mount_path="/data")],
        args=["server", "/data"],
        env=[
            client.V1EnvVar(name="MINIO_ROOT_USER", value=ACCESS_KEY),
            client.V1EnvVar(name="MINIO_ROOT_PASSWORD", value=SECRET_KEY),
        ],
        ports=[client.V1ContainerPort(container_port=PORT)],
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path="/minio/health/ready", port=PORT),
            period_seconds=2,
        ),
    )
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=NAME, namespace=namespace, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    containers=[container],
                    volumes=[client.V1Volume(name="data", empty_dir=client.V1EmptyDirVolumeSource())],
                    security_context=client.V1PodSecurityContext(fs_group=65532),
                ),
            ),
        ),
    )


def _service(namespace: str) -> client.V1Service:
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=NAME, namespace=namespace),
        spec=client.V1ServiceSpec(
            selector={"app": NAME},
            ports=[client.V1ServicePort(port=PORT, target_port=PORT)],
        ),
    )


def _create_if_missing(create: Any, **kwargs: Any) -> None:
    try:
        create(**kwargs)
    except ApiException as exc:
        if exc.status != 409:  # AlreadyExists -> reuse
            raise


def ensure_minio(namespace: str = "default", timeout: float = 180) -> str:
    """Make sure MinIO is deployed and ready; return its in-cluster endpoint URL."""
    from sk8s import k8s

    core = k8s.core_api()  # loads kube config
    apps = client.AppsV1Api()
    _create_if_missing(
        apps.create_namespaced_deployment, namespace=namespace, body=_deployment(namespace)
    )
    _create_if_missing(core.create_namespaced_service, namespace=namespace, body=_service(namespace))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = apps.read_namespaced_deployment_status(NAME, namespace).status
        if (status.ready_replicas or 0) >= 1:
            return f"http://{NAME}.{namespace}.svc.cluster.local:{PORT}"
        time.sleep(2)
    raise TimeoutError(f"deployment/{NAME} not ready after {timeout}s")


def teardown_minio(namespace: str = "default") -> None:
    """Remove the MinIO Deployment and Service (not called by the fixtures)."""
    from sk8s import k8s

    core = k8s.core_api()  # loads kube config
    apps = client.AppsV1Api()
    for delete, kwargs in (
        (apps.delete_namespaced_deployment, {"name": NAME, "namespace": namespace}),
        (core.delete_namespaced_service, {"name": NAME, "namespace": namespace}),
    ):
        try:
            delete(**kwargs)
        except ApiException as exc:
            if exc.status != 404:
                raise
