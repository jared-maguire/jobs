from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes.client import ApiException

import sk8s
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import BlobNotFound, ImageNotConfigured, Sk8sError
from sk8s.serialize import decode_payload
from sk8s.services import Service, get_service, serve
from sk8s.store.local import LocalStore
from tests.fakes import FakeAppsV1, FakeBatchV1, FakeCoreV1


class Harness:
    def __init__(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        self.apps = FakeAppsV1()
        self.core = FakeCoreV1()
        self.store = LocalStore(tmp_path)
        cfg = Config(namespace="ns1", image="sk8s:dev", store=f"local:{tmp_path}")
        self.cluster = Cluster(
            cfg, batch_api=FakeBatchV1(), core_api=self.core, apps_api=self.apps,
            store=self.store, watcher_events=lambda: iter(()),
        )

    def payload_uri(self, name: str) -> str:
        return self.store.uri_for(f"svc/{name}/payload")

    def payload_exists(self, name: str) -> bool:
        try:
            self.store.get(self.payload_uri(name))
        except BlobNotFound:
            return False
        return True


@pytest.fixture
def h(tmp_path):  # type: ignore[no-untyped-def]
    harness = Harness(tmp_path)
    yield harness
    harness.cluster.close()


def _serve(h: Harness, name: str = "api", **kw: Any) -> Service:
    return serve(lambda: None, name=name, port=8080, cluster=h.cluster, **kw)


def test_serve_creates_deployment_service_and_payload(h: Harness) -> None:
    svc = _serve(h, replicas=3)
    dep = h.apps.deployments[("ns1", "api")]
    assert dep.spec.replicas == 3
    container = dep.spec.template.spec.containers[0]
    assert container.image == "sk8s:dev"
    env = {e.name: e.value for e in container.env}
    assert env["SK8S_SERVICE"] == "1"
    assert env["SK8S_PAYLOAD_URI"] == h.payload_uri("api")
    assert env["SK8S_STORE"] == h.cluster.config.store
    assert container.ports[0].container_port == 8080
    ksvc = h.core.services[("ns1", "api")]
    assert ksvc.spec.ports[0].port == 8080
    assert ksvc.spec.selector == {"sk8s/service": "api"}
    assert h.payload_exists("api")
    assert isinstance(svc, Service)
    assert (svc.name, svc.namespace, svc.port) == ("api", "ns1", 8080)


def test_no_owner_references(h: Harness) -> None:
    _serve(h)
    assert h.apps.deployments[("ns1", "api")].metadata.owner_references is None
    assert h.core.services[("ns1", "api")].metadata.owner_references is None


def test_url_string_exact(h: Harness) -> None:
    assert _serve(h).url == "http://api.ns1.svc.cluster.local:8080"


def test_serve_without_any_image_fails_fast_and_creates_nothing(h: Harness) -> None:
    h.cluster.config = Config(namespace="ns1", store=h.cluster.config.store)  # no image
    with pytest.raises(ImageNotConfigured):
        _serve(h)
    assert h.apps.deployments == {}
    assert not h.payload_exists("api")


def test_serve_image_argument_suffices_without_a_configured_image(h: Harness) -> None:
    h.cluster.config = Config(namespace="ns1", store=h.cluster.config.store)
    _serve(h, image="custom:3")
    assert h.apps.deployments[("ns1", "api")].spec.template.spec.containers[0].image == "custom:3"


def test_image_override(h: Harness) -> None:
    _serve(h, image="custom:2")
    dep = h.apps.deployments[("ns1", "api")]
    assert dep.spec.template.spec.containers[0].image == "custom:2"


def test_function_args_and_kwargs_reach_payload(h: Harness) -> None:
    def handler(a: int, b: int, *, scale: int = 1) -> int:
        return (a + b) * scale

    serve(handler, 1, 2, name="api", port=80, cluster=h.cluster, scale=10)
    func, args, kwargs = decode_payload(h.store.get(h.payload_uri("api")))
    assert (args, kwargs) == ((1, 2), {"scale": 10})
    assert func(*args, **kwargs) == 30


@pytest.mark.parametrize(
    "bad", ["", "API", "has_underscore", "-lead", "trail-", "a.b", "x" * 64, "1abc", "sp ace"]
)
def test_invalid_name_raises_before_any_api_call(h: Harness, bad: str) -> None:
    with pytest.raises(ValueError):
        _serve(h, name=bad)
    assert not h.apps.deployments and not h.core.services
    assert not list(h.store.root.rglob("*payload*"))


def test_invalid_replicas_and_port(h: Harness) -> None:
    with pytest.raises(ValueError):
        _serve(h, replicas=-1)
    with pytest.raises(ValueError):
        serve(lambda: None, name="api", port=0, cluster=h.cluster)
    assert not h.apps.deployments


def test_duplicate_serve_raises_and_leaves_original_untouched(h: Harness) -> None:
    _serve(h)
    before = (
        h.apps.deployments[("ns1", "api")],
        h.core.services[("ns1", "api")],
        h.store.get(h.payload_uri("api")),
    )
    with pytest.raises(Sk8sError, match="api"):
        serve(lambda: 1, name="api", port=9999, cluster=h.cluster)
    after = (
        h.apps.deployments[("ns1", "api")],
        h.core.services[("ns1", "api")],
        h.store.get(h.payload_uri("api")),
    )
    assert after[0] is before[0] and after[1] is before[1]
    assert after[2] == before[2]
    assert len(h.apps.deployments) == 1 and len(h.core.services) == 1


def test_conflict_on_service_only_rolls_back_this_calls_deployment(h: Harness) -> None:
    # A pre-existing Service with the same name but no Deployment.
    pre = SimpleNamespace(metadata=SimpleNamespace(name="api"), spec=None)
    h.core.services[("ns1", "api")] = pre
    with pytest.raises(Sk8sError, match="api"):
        _serve(h)
    assert h.core.services[("ns1", "api")] is pre
    assert not h.apps.deployments
    assert not h.payload_exists("api")


def test_race_409_on_service_rolls_back_deployment_and_payload(h: Harness) -> None:
    real = h.core.create_namespaced_service

    def racing(namespace: str, body: Any) -> Any:
        raise ApiException(status=409, reason="Conflict")

    h.core.create_namespaced_service = racing  # type: ignore[method-assign]
    with pytest.raises(Sk8sError, match="api"):
        _serve(h)
    h.core.create_namespaced_service = real  # type: ignore[method-assign]
    assert not h.apps.deployments and not h.core.services
    assert not h.payload_exists("api")


def test_race_409_on_deployment_rolls_back_payload(h: Harness) -> None:
    def racing(namespace: str, body: Any) -> Any:
        raise ApiException(status=409, reason="Conflict")

    h.apps.create_namespaced_deployment = racing  # type: ignore[method-assign]
    with pytest.raises(Sk8sError, match="api"):
        _serve(h)
    assert not h.core.services
    assert not h.payload_exists("api")


def test_non_conflict_error_rolls_back_and_propagates(h: Harness) -> None:
    def boom(namespace: str, body: Any) -> Any:
        raise ApiException(status=500, reason="boom")

    h.core.create_namespaced_service = boom  # type: ignore[method-assign]
    with pytest.raises(ApiException):
        _serve(h)
    assert not h.apps.deployments
    assert not h.payload_exists("api")


def test_scale_patches_replicas(h: Harness) -> None:
    svc = _serve(h, replicas=1)
    svc.scale(5)
    assert h.apps.deployments[("ns1", "api")].spec.replicas == 5
    with pytest.raises(ValueError):
        svc.scale(-1)


def test_delete_removes_all_three_and_is_idempotent(h: Harness) -> None:
    svc = _serve(h)
    svc.delete()
    assert not h.apps.deployments and not h.core.services
    assert not h.payload_exists("api")
    svc.delete()  # second call is a no-op


def test_delete_only_touches_own_name(h: Harness) -> None:
    a, _ = _serve(h, "a"), _serve(h, "b")
    a.delete()
    assert ("ns1", "b") in h.apps.deployments and ("ns1", "b") in h.core.services
    assert h.payload_exists("b")


def test_delete_propagates_non_404_errors(h: Harness) -> None:
    svc = _serve(h)

    def boom(name: str, namespace: str, **_: Any) -> None:
        raise ApiException(status=500, reason="boom")

    h.apps.delete_namespaced_deployment = boom  # type: ignore[assignment]
    with pytest.raises(ApiException):
        svc.delete()


def test_get_service_roundtrip(h: Harness) -> None:
    _serve(h)
    svc = get_service("api", cluster=h.cluster)
    assert (svc.name, svc.namespace, svc.port) == ("api", "ns1", 8080)
    assert svc.url == "http://api.ns1.svc.cluster.local:8080"
    svc.delete()
    assert not h.apps.deployments


def test_get_service_missing_name(h: Harness) -> None:
    with pytest.raises(Sk8sError, match="no service named 'ghost'"):
        get_service("ghost", cluster=h.cluster)


def test_get_service_rejects_unmanaged_service(h: Harness) -> None:
    from kubernetes import client

    for labels in (None, {"app": "x"}, {"sk8s/kind": "job"}):
        h.core.services[("ns1", "kubernetes")] = client.V1Service(
            metadata=client.V1ObjectMeta(name="kubernetes", namespace="ns1", labels=labels),
            spec=client.V1ServiceSpec(ports=[client.V1ServicePort(port=443)]),
        )
        with pytest.raises(Sk8sError, match="no service named 'kubernetes'"):
            get_service("kubernetes", cluster=h.cluster)
    assert ("ns1", "kubernetes") in h.core.services  # untouched


def test_logs_concatenates_pod_logs_by_selector(h: Harness) -> None:
    svc = _serve(h)
    seen: dict[str, Any] = {}

    def list_pods(namespace: str, label_selector: str = "") -> Any:
        seen["selector"] = label_selector
        return SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in ("p1", "p2")]
        )

    h.core.list_namespaced_pod = list_pods  # type: ignore[method-assign]
    h.core.read_namespaced_pod_log = lambda name, namespace: f"log-{name}"  # type: ignore[method-assign]
    assert svc.logs() == "log-p1\nlog-p2"
    assert seen["selector"] == "sk8s/service=api"


def test_exports() -> None:
    assert sk8s.serve is serve and sk8s.get_service is get_service and sk8s.Service is Service
    assert {"serve", "get_service", "Service"} <= set(sk8s.__all__)
