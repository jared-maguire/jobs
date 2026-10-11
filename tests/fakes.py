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
        self.namespaces: dict[str, Any] = {}  # name -> V1Namespace
        self.service_accounts: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1ServiceAccount
        # Every namespace/serviceaccount write attempt as (verb, kind, name); 409s included.
        self.actions: list[tuple[str, str, str]] = []
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

    def create_namespace(self, body: Any) -> Any:
        self.actions.append(("create", "Namespace", body.metadata.name))
        if body.metadata.name in self.namespaces:
            raise ApiException(status=409, reason="Conflict")
        self.namespaces[body.metadata.name] = body
        return body

    def get_api_resources(self) -> Any:
        return SimpleNamespace(resources=[])

    def read_namespace(self, name: str) -> Any:
        try:
            return self.namespaces[name]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def create_namespaced_service_account(self, namespace: str, body: Any) -> Any:
        self.actions.append(("create", "ServiceAccount", body.metadata.name))
        if (namespace, body.metadata.name) in self.service_accounts:
            raise ApiException(status=409, reason="Conflict")
        self.service_accounts[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_service_account(self, name: str, namespace: str) -> Any:
        try:
            return self.service_accounts[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def replace_namespaced_service_account(self, name: str, namespace: str, body: Any) -> Any:
        self.actions.append(("replace", "ServiceAccount", name))
        if (namespace, name) not in self.service_accounts:
            raise ApiException(status=404, reason="Not Found")
        self.service_accounts[(namespace, name)] = body
        return body

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


class FakeRbacV1:
    """Role / RoleBinding fakes with 409-on-duplicate-create and 404-on-missing semantics."""

    def __init__(self) -> None:
        self.roles: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1Role
        self.role_bindings: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1RoleBinding
        # Every write attempt as (verb, kind, name); 409s included.
        self.actions: list[tuple[str, str, str]] = []

    def _create(self, store: dict[tuple[str, str], Any], kind: str, ns: str, body: Any) -> Any:
        self.actions.append(("create", kind, body.metadata.name))
        if (ns, body.metadata.name) in store:
            raise ApiException(status=409, reason="Conflict")
        store[(ns, body.metadata.name)] = body
        return body

    def _read(self, store: dict[tuple[str, str], Any], ns: str, name: str) -> Any:
        try:
            return store[(ns, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def _replace(
        self, store: dict[tuple[str, str], Any], kind: str, ns: str, name: str, body: Any
    ) -> Any:
        self.actions.append(("replace", kind, name))
        if (ns, name) not in store:
            raise ApiException(status=404, reason="Not Found")
        store[(ns, name)] = body
        return body

    def _delete(self, store: dict[tuple[str, str], Any], kind: str, ns: str, name: str) -> None:
        self.actions.append(("delete", kind, name))
        if (ns, name) not in store:
            raise ApiException(status=404, reason="Not Found")
        del store[(ns, name)]

    def create_namespaced_role(self, namespace: str, body: Any) -> Any:
        return self._create(self.roles, "Role", namespace, body)

    def read_namespaced_role(self, name: str, namespace: str) -> Any:
        return self._read(self.roles, namespace, name)

    def replace_namespaced_role(self, name: str, namespace: str, body: Any) -> Any:
        return self._replace(self.roles, "Role", namespace, name, body)

    def delete_namespaced_role(self, name: str, namespace: str) -> None:
        self._delete(self.roles, "Role", namespace, name)

    def create_namespaced_role_binding(self, namespace: str, body: Any) -> Any:
        return self._create(self.role_bindings, "RoleBinding", namespace, body)

    def read_namespaced_role_binding(self, name: str, namespace: str) -> Any:
        return self._read(self.role_bindings, namespace, name)

    def replace_namespaced_role_binding(self, name: str, namespace: str, body: Any) -> Any:
        return self._replace(self.role_bindings, "RoleBinding", namespace, name, body)

    def delete_namespaced_role_binding(self, name: str, namespace: str) -> None:
        self._delete(self.role_bindings, "RoleBinding", namespace, name)


class FakeStorageV1:
    def __init__(self, classes: tuple[str, ...] = ()) -> None:
        self.classes = set(classes)

    def read_storage_class(self, name: str) -> Any:
        if name not in self.classes:
            raise ApiException(status=404, reason="Not Found")
        return SimpleNamespace(metadata=SimpleNamespace(name=name))
