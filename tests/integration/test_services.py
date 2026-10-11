import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from kubernetes.client import ApiException

from sk8s.cluster import Cluster
from sk8s.config import load_config
from sk8s.errors import BlobNotFound, Sk8sError
from sk8s.services import Service, get_service, serve
from tests.integration.conftest import requires_cluster, suite_namespace, suite_pull_policy

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(600)]

IMAGE = os.environ.get("SK8S_SERVICES_TEST_IMAGE", "sk8s-dev-services:latest")
NAME = "sk8s-it-http"
PORT = 8080


def _new_cluster() -> Cluster:
    return Cluster(
        load_config(namespace=suite_namespace(), image=IMAGE, pull_policy=suite_pull_policy(), ttl_seconds_after_finished=30)
    )


def _poll(cond: Callable[[], Any], timeout: float, what: str, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = cond()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last={last!r})")


def _probe(cluster: Cluster, url: str, timeout: float = 120) -> int:
    """GET url from inside the cluster via a job, retrying while the pods come up."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = cluster.run(
                lambda u=url: __import__("urllib.request", fromlist=["urlopen"]).urlopen(
                    u, timeout=5
                ).status,
                timeout=60,
            )
            return int(status)
        except Exception as e:  # noqa: BLE001 — pod not ready yet, job failed/timed out
            last = e
            time.sleep(2)
    raise AssertionError(f"{url} not reachable within {timeout}s: {last!r}")


def _gone(read: Callable[..., Any], **kw: Any) -> bool:
    try:
        read(**kw)
    except ApiException as e:
        if e.status == 404:
            return True
        raise
    return False


@pytest.fixture(scope="module")
def svc_cluster() -> Iterator[Cluster]:
    c = _new_cluster()
    yield c
    c.close()


@pytest.fixture(scope="module")
def svc(svc_cluster: Cluster) -> Iterator[Service]:
    try:  # leftovers from an aborted earlier run
        get_service(NAME, cluster=svc_cluster).delete()
        _poll(
            lambda: _gone(
                svc_cluster.apps.read_namespaced_deployment,
                name=NAME, namespace=svc_cluster.config.namespace,
            ),
            60, "stale deployment removal",
        )
    except Sk8sError:
        pass
    def srv() -> None:
        # Defined locally so it is pickled by value: the worker image has no `tests` package.
        import http.server
        import socketserver

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        socketserver.ThreadingTCPServer(
            ("", 8080), http.server.SimpleHTTPRequestHandler
        ).serve_forever()

    s = serve(srv, name=NAME, port=PORT, image=IMAGE, cluster=svc_cluster)
    yield s
    s.delete()  # finalizer: runs even when a test failed; delete() is idempotent


def test_serve_reachable_from_cluster(svc: Service, svc_cluster: Cluster) -> None:
    assert svc.url == f"http://{NAME}.{svc_cluster.config.namespace}.svc.cluster.local:{PORT}"
    assert _probe(svc_cluster, svc.url) == 200


def test_get_service_from_fresh_cluster(svc: Service) -> None:
    fresh = _new_cluster()
    try:
        again = get_service(NAME, cluster=fresh)
        assert again.url == svc.url
        assert _probe(fresh, again.url) == 200
    finally:
        fresh.close()


def test_get_service_refuses_unmanaged(svc_cluster: Cluster) -> None:
    with pytest.raises(Sk8sError, match="no service named 'kubernetes'"):
        get_service("kubernetes", cluster=svc_cluster)


def test_scale_to_two_replicas(svc: Service, svc_cluster: Cluster) -> None:
    svc.scale(2)

    def ready() -> bool:
        dep = svc_cluster.apps.read_namespaced_deployment(
            name=NAME, namespace=svc.namespace
        )
        return (dep.status.ready_replicas or 0) == 2

    _poll(ready, 120, "2 ready replicas")


def _blob_gone(store: Any, uri: str) -> bool:
    try:
        store.get(uri)
    except BlobNotFound:
        return True
    return False


def test_delete_removes_everything(svc: Service, svc_cluster: Cluster) -> None:
    ns = svc.namespace
    payload_key = f"svc/{NAME}/payload"
    store = svc_cluster.store
    payload_uri = store.uri_for(payload_key)
    assert store.get(payload_uri)  # the payload blob exists before delete (any store)

    svc.delete()

    def all_gone() -> bool:
        return (
            _gone(svc_cluster.apps.read_namespaced_deployment, name=NAME, namespace=ns)
            and _gone(svc_cluster.core.read_namespaced_service, name=NAME, namespace=ns)
            and _blob_gone(store, payload_uri)
        )

    _poll(all_gone, 30, "Deployment, Service and payload to be gone")
    with pytest.raises(Sk8sError):
        get_service(NAME, cluster=svc_cluster)
    svc.delete()  # idempotent

