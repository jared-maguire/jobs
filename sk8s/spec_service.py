"""Build Deployment/Service objects for long-running services (client models only).

Services are exempt from workflow lifecycle: no sk8s/workflow-id label and no
owner references, so they outlive any workflow until explicitly deleted.
"""
from __future__ import annotations

from kubernetes import client

from sk8s.spec import MANAGED_BY, POD_ANNOTATIONS


def _labels(name: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "sk8s/kind": "service",
        "sk8s/service": name,
    }


def build_deployment(
    name: str,
    *,
    image: str,
    namespace: str,
    payload_uri: str,
    store: str,
    port: int,
    replicas: int,
    service_account: str,
    pull_policy: str,
    requests: dict[str, str],
    limits: dict[str, str],
) -> client.V1Deployment:
    labels = _labels(name)
    env = [
        client.V1EnvVar(name="SK8S_STORE", value=store),
        client.V1EnvVar(name="SK8S_NAMESPACE", value=namespace),
        client.V1EnvVar(name="SK8S_PAYLOAD_URI", value=payload_uri),
        client.V1EnvVar(name="SK8S_SERVICE", value="1"),
    ]
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
        ports=[client.V1ContainerPort(container_port=port)],
    )
    pod_spec = client.V1PodSpec(
        service_account_name=service_account,
        containers=[container],
    )
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"sk8s/service": name}),
            template=client.V1PodTemplateSpec(
                # Service pods are long-running and never spot, so they always get the
                # voluntary-eviction protection (see sk8s.spec.POD_ANNOTATIONS).
                metadata=client.V1ObjectMeta(labels=labels, annotations=dict(POD_ANNOTATIONS)),
                spec=pod_spec,
            ),
        ),
    )


def build_service(name: str, *, namespace: str, port: int) -> client.V1Service:
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels(name)),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={"sk8s/service": name},
            ports=[client.V1ServicePort(port=port, target_port=port)],
        ),
    )
