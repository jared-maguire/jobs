from sk8s.spec_service import build_deployment, build_service


def make(**over):  # type: ignore[no-untyped-def]
    args = dict(
        image="sk8s:dev", namespace="ns1",
        payload_uri="k8s://ns1/sk8s-svc-api-payload", store="k8s", port=8080,
        replicas=2, service_account="sk8s", pull_policy="IfNotPresent",
        requests={}, limits={},
    )
    args.update(over)
    return build_deployment("api", **args)  # type: ignore[arg-type]


EXPECTED_LABELS = {
    "app.kubernetes.io/managed-by": "sk8s",
    "sk8s/kind": "service",
    "sk8s/service": "api",
}


def test_deployment_basic_fields() -> None:
    dep = make()
    assert dep.api_version == "apps/v1"
    assert dep.kind == "Deployment"
    assert dep.metadata.name == "api"
    assert dep.metadata.namespace == "ns1"
    assert dep.metadata.labels == EXPECTED_LABELS
    assert dep.spec.replicas == 2
    assert dep.spec.selector.match_labels == {"sk8s/service": "api"}
    assert dep.spec.template.metadata.labels == EXPECTED_LABELS
    pod = dep.spec.template.spec
    assert pod.service_account_name == "sk8s"
    c = pod.containers[0]
    assert c.name == "worker"
    assert c.image == "sk8s:dev"
    assert c.image_pull_policy == "IfNotPresent"
    assert c.command == ["python", "-m", "sk8s.worker"]
    assert c.ports[0].container_port == 8080


def test_deployment_selector_matches_template_labels() -> None:
    dep = make()
    sel = dep.spec.selector.match_labels
    labels = dep.spec.template.metadata.labels
    assert all(labels[k] == v for k, v in sel.items())


def test_deployment_no_workflow_id_or_owner() -> None:
    dep = make()
    assert "sk8s/workflow-id" not in dep.metadata.labels
    assert "sk8s/workflow-id" not in dep.spec.template.metadata.labels
    assert dep.metadata.owner_references is None


def test_deployment_env() -> None:
    env = {e.name: e.value for e in make().spec.template.spec.containers[0].env}
    assert env == {
        "SK8S_STORE": "k8s",
        "SK8S_NAMESPACE": "ns1",
        "SK8S_PAYLOAD_URI": "k8s://ns1/sk8s-svc-api-payload",
        "SK8S_SERVICE": "1",
    }
    assert "SK8S_RESULT_KEY" not in env


def test_deployment_resources() -> None:
    c = make(requests={"cpu": "1"}, limits={"memory": "1Gi"}).spec.template.spec.containers[0]
    assert c.resources.requests == {"cpu": "1"}
    assert c.resources.limits == {"memory": "1Gi"}


def test_deployment_no_resources_means_none() -> None:
    assert make().spec.template.spec.containers[0].resources is None


def test_deployment_partial_resources() -> None:
    c = make(requests={"cpu": "1"}).spec.template.spec.containers[0]
    assert c.resources.requests == {"cpu": "1"}
    assert c.resources.limits is None


def test_service() -> None:
    svc = build_service("api", namespace="ns1", port=8080)
    assert svc.api_version == "v1"
    assert svc.kind == "Service"
    assert svc.metadata.name == "api"
    assert svc.metadata.namespace == "ns1"
    assert svc.metadata.labels == EXPECTED_LABELS
    assert svc.metadata.owner_references is None
    assert svc.spec.type == "ClusterIP"
    assert svc.spec.selector == {"sk8s/service": "api"}
    p = svc.spec.ports[0]
    assert p.port == 8080
    assert p.target_port == 8080
