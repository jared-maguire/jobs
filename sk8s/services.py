"""Long-running services: ``serve()``, the ``Service`` handle and ``get_service()``.

A service is a Deployment + ClusterIP Service running ``func(*args, **kwargs)``
in the worker's service mode, plus a payload blob at ``svc/<name>/payload``.
Services are exempt from workflow lifecycle: nothing owns them, so they survive
everything except an explicit ``Service.delete()``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from kubernetes.client import ApiException

from sk8s import serialize
from sk8s.errors import Sk8sError
from sk8s.spec_service import build_deployment, build_service

log = logging.getLogger("sk8s.services")

# Kubernetes requires a Service name to be a DNS-1035 label: DNS-1123 plus a
# leading letter. Validating that here avoids failing after the Deployment exists.
_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_MAX_NAME = 63


def _validate_name(name: str) -> None:
    if len(name) > _MAX_NAME or not _NAME_RE.match(name):
        raise ValueError(
            f"invalid service name {name!r}: must be a DNS-1123 label (lowercase letters, "
            f"digits and '-'; start with a letter, end alphanumeric; at most {_MAX_NAME} chars)"
        )


def _payload_key(name: str) -> str:
    return f"svc/{name}/payload"


def _resolve(cluster: Any | None) -> Any:
    if cluster is not None:
        return cluster
    from sk8s.api import _cluster

    return _cluster()


def _status(exc: ApiException) -> int | None:
    return exc.status  # type: ignore[no-any-return]


def _exists(read: Callable[..., Any], name: str, namespace: str) -> bool:
    try:
        read(name=name, namespace=namespace)
    except ApiException as e:
        if _status(e) == 404:
            return False
        raise
    return True


def _ignore_404(call: Callable[..., Any], **kwargs: Any) -> None:
    try:
        call(**kwargs)
    except ApiException as e:
        if _status(e) != 404:
            raise


class Service:
    """Handle to a running service. Obtain one from ``serve()`` or ``get_service()``."""

    def __init__(self, name: str, namespace: str, port: int, cluster: Any) -> None:
        self.name = name
        self.namespace = namespace
        self.port = port
        self._cluster = cluster

    def __repr__(self) -> str:
        return f"Service(name={self.name!r}, namespace={self.namespace!r}, port={self.port})"

    @property
    def url(self) -> str:
        return f"http://{self.name}.{self.namespace}.svc.cluster.local:{self.port}"

    def logs(self) -> str:
        """Logs of every pod backing the service, concatenated."""
        core = self._cluster.core
        pods = core.list_namespaced_pod(
            namespace=self.namespace, label_selector=f"sk8s/service={self.name}"
        )
        return "\n".join(
            core.read_namespaced_pod_log(name=pod.metadata.name, namespace=self.namespace)
            for pod in pods.items
        )

    def scale(self, replicas: int) -> None:
        if replicas < 0:
            raise ValueError(f"replicas must be >= 0, got {replicas!r}")
        self._cluster.apps.patch_namespaced_deployment(
            name=self.name, namespace=self.namespace, body={"spec": {"replicas": replicas}}
        )

    def delete(self) -> None:
        """Remove the Deployment, Service and payload blob. Safe to call twice."""
        apps, core, store = self._cluster.apps, self._cluster.core, self._cluster.store
        _ignore_404(
            apps.delete_namespaced_deployment, name=self.name, namespace=self.namespace,
            propagation_policy="Foreground",
        )
        _ignore_404(core.delete_namespaced_service, name=self.name, namespace=self.namespace)
        store.delete(store.uri_for(_payload_key(self.name)))


def serve(
    func: Callable[..., Any],
    /,
    *args: Any,
    name: str,
    port: int,
    replicas: int = 1,
    image: str | None = None,
    cluster: Any | None = None,
    **kwargs: Any,
) -> Service:
    """Run ``func(*args, **kwargs)`` as a long-lived service and return its handle.

    ``func`` must block forever serving requests on ``port``. The service is
    reachable in-cluster at ``Service.url``. Raises ``ValueError`` for a bad
    name, port or replica count, and ``Sk8sError`` if a service of that name
    already exists. A failed call leaves nothing of its own behind.

    Limitation: two concurrent ``serve()`` calls with the same name can race.
    Exactly one wins the Deployment/Service create, but both write the payload
    blob at ``svc/<name>/payload`` first, and the loser's rollback then deletes
    it, which can leave the winner's pods without a payload. The window is
    narrow (between the existence pre-check and the create), and the store has
    no conditional writes to close it. Do not serve the same name concurrently.
    """
    _validate_name(name)
    if port < 1 or port > 65535:
        raise ValueError(f"port must be in 1..65535, got {port!r}")
    if replicas < 0:
        raise ValueError(f"replicas must be >= 0, got {replicas!r}")
    c = _resolve(cluster)
    namespace = c.config.namespace
    assert namespace is not None
    from sk8s.cluster import resolve_image

    resolved_image = resolve_image(c.config, image)  # ImageNotConfigured before anything is created
    apps, core, store = c.apps, c.core, c.store

    def conflict() -> Sk8sError:
        return Sk8sError(
            f"service {name!r} already exists in namespace {namespace!r}; "
            f"delete it first (get_service({name!r}).delete()) or pick another name"
        )

    # Pre-check: writing the payload would overwrite an existing service's blob,
    # so refuse before touching anything.
    if _exists(apps.read_namespaced_deployment, name, namespace) or _exists(
        core.read_namespaced_service, name, namespace
    ):
        raise conflict()

    payload_uri = store.put(_payload_key(name), serialize.encode_payload(func, args, kwargs))
    created: list[Callable[[], None]] = []  # undo steps, newest last

    def rollback() -> None:
        for undo in reversed(created):
            try:
                undo()
            except Exception:  # noqa: BLE001 — best-effort; the original error matters
                log.exception("rollback step failed while serving %s", name)

    created.append(lambda: store.delete(payload_uri))
    try:
        deployment = build_deployment(
            name,
            image=resolved_image,
            namespace=namespace,
            payload_uri=payload_uri,
            store=c.config.store,
            port=port,
            replicas=replicas,
            service_account=c.config.service_account,
            pull_policy=c.config.pull_policy,
            requests={},
            limits={},
        )
        apps.create_namespaced_deployment(namespace=namespace, body=deployment)
        created.append(
            lambda: _ignore_404(
                apps.delete_namespaced_deployment, name=name, namespace=namespace,
                propagation_policy="Foreground",
            )
        )
        core.create_namespaced_service(
            namespace=namespace, body=build_service(name, namespace=namespace, port=port)
        )
    except BaseException as exc:
        rollback()
        if isinstance(exc, ApiException) and _status(exc) == 409:
            raise conflict() from exc
        raise
    return Service(name, namespace, port, c)


def get_service(name: str, cluster: Any | None = None) -> Service:
    """Look up a running service by name. Raises ``Sk8sError`` if there is none."""
    c = _resolve(cluster)
    namespace = c.config.namespace
    assert namespace is not None
    try:
        obj = c.core.read_namespaced_service(name=name, namespace=namespace)
    except ApiException as e:
        if _status(e) == 404:
            raise Sk8sError(f"no service named {name!r} in namespace {namespace!r}") from None
        raise
    labels = obj.metadata.labels or {}
    if labels.get("sk8s/kind") != "service":
        # Not ours (e.g. the cluster's "kubernetes" Service): refuse, so that
        # .delete() on the returned handle can never touch an unmanaged Service.
        raise Sk8sError(f"no service named {name!r} in namespace {namespace!r}")
    return Service(name, namespace, int(obj.spec.ports[0].port), c)
