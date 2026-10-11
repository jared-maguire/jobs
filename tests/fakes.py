"""In-memory fakes for the kubernetes client surface sk8s uses."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from kubernetes.client import ApiException


class FakeCoreV1:
    def __init__(self) -> None:
        self.configmaps: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1ConfigMap
        self.create_calls: list[str] = []  # every create attempt, 409s included
        self.delete_policies: dict[str, str | None] = {}  # configmap name -> propagation policy
        self.services: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1Service
        self.pvcs: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1PersistentVolumeClaim
        self.pvc_delete_policies: dict[str, str | None] = {}  # pvc name -> propagation policy

    def create_namespaced_config_map(self, namespace: str, body: Any) -> Any:
        self.create_calls.append(body.metadata.name)
        if (namespace, body.metadata.name) in self.configmaps:
            raise ApiException(status=409, reason="Conflict")
        if not body.metadata.uid:
            body.metadata.uid = uuid.uuid4().hex
        self.configmaps[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_config_map(self, name: str, namespace: str) -> Any:
        try:
            return self.configmaps[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def delete_namespaced_config_map(
        self, name: str, namespace: str, propagation_policy: str | None = None
    ) -> None:
        if (namespace, name) not in self.configmaps:
            raise ApiException(status=404, reason="Not Found")
        self.delete_policies[name] = propagation_policy
        del self.configmaps[(namespace, name)]

    def patch_namespaced_config_map(self, name: str, namespace: str, body: Any) -> Any:
        """Merge-patch of metadata.annotations / metadata.labels from a dict body."""
        cm = self.read_namespaced_config_map(name=name, namespace=namespace)
        for field in ("annotations", "labels"):
            patch = (body.get("metadata") or {}).get(field)
            if patch:
                setattr(cm.metadata, field, {**(getattr(cm.metadata, field) or {}), **patch})
        return cm

    def list_namespaced_config_map(self, namespace: str, label_selector: str = "") -> Any:
        """Honors the simple `k=v[,k=v...]` equality form of label selectors."""
        wanted = dict(p.split("=", 1) for p in label_selector.split(",") if p)
        items = [
            v
            for (ns, _), v in self.configmaps.items()
            if ns == namespace
            and all((v.metadata.labels or {}).get(k) == val for k, val in wanted.items())
        ]
        return SimpleNamespace(items=items)

    def create_namespaced_service(self, namespace: str, body: Any) -> Any:
        if (namespace, body.metadata.name) in self.services:
            raise ApiException(status=409, reason="Conflict")
        self.services[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_service(self, name: str, namespace: str) -> Any:
        try:
            return self.services[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def delete_namespaced_service(self, name: str, namespace: str) -> None:
        if (namespace, name) not in self.services:
            raise ApiException(status=404, reason="Not Found")
        del self.services[(namespace, name)]

    def create_namespaced_persistent_volume_claim(self, namespace: str, body: Any) -> Any:
        if (namespace, body.metadata.name) in self.pvcs:
            raise ApiException(status=409, reason="Conflict")
        if not body.metadata.uid:
            body.metadata.uid = uuid.uuid4().hex
        self.pvcs[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str) -> Any:
        try:
            return self.pvcs[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def delete_namespaced_persistent_volume_claim(
        self, name: str, namespace: str, propagation_policy: str | None = None
    ) -> None:
        if (namespace, name) not in self.pvcs:
            raise ApiException(status=404, reason="Not Found")
        self.pvc_delete_policies[name] = propagation_policy
        del self.pvcs[(namespace, name)]

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        class R:
            items: list[Any] = []

        return R()

    def read_namespaced_pod_log(self, name: str, namespace: str) -> str:
        return ""


class FakeBatchV1:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], Any] = {}
        self.deleted: list[str] = []

    def create_namespaced_job(self, namespace: str, body: Any) -> Any:
        self.jobs[(namespace, body.metadata.name)] = body
        return body

    def delete_namespaced_job(
        self, name: str, namespace: str, propagation_policy: str = "Background"
    ) -> None:
        self.deleted.append(name)
        self.jobs.pop((namespace, name), None)

    def read_namespaced_job(self, name: str, namespace: str) -> Any:
        try:
            return self.jobs[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def list_namespaced_job(self, namespace: str, label_selector: str = "", **_: Any) -> Any:
        """Honors the simple `k=v[,k=v...]` equality form of label selectors."""
        wanted = dict(p.split("=", 1) for p in label_selector.split(",") if p)
        items = [
            j
            for (ns, _name), j in self.jobs.items()
            if ns == namespace
            and all((j.metadata.labels or {}).get(k) == v for k, v in wanted.items())
        ]
        return SimpleNamespace(items=items, metadata=SimpleNamespace(resource_version="1"))


class FakeAppsV1:
    def __init__(self) -> None:
        self.deployments: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1Deployment

    def create_namespaced_deployment(self, namespace: str, body: Any) -> Any:
        if (namespace, body.metadata.name) in self.deployments:
            raise ApiException(status=409, reason="Conflict")
        self.deployments[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_deployment(self, name: str, namespace: str) -> Any:
        try:
            return self.deployments[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def patch_namespaced_deployment(self, name: str, namespace: str, body: Any) -> Any:
        """Applies a merge-patch of the form {"spec": {"replicas": n}}."""
        dep = self.read_namespaced_deployment(name, namespace)
        for key, value in (body.get("spec") or {}).items():
            setattr(dep.spec, key, value)
        return dep

    def delete_namespaced_deployment(
        self, name: str, namespace: str, propagation_policy: str = "Background"
    ) -> None:
        if (namespace, name) not in self.deployments:
            raise ApiException(status=404, reason="Not Found")
        del self.deployments[(namespace, name)]
