"""In-memory fakes for the kubernetes client surface sk8s uses."""
from __future__ import annotations

import copy
import datetime
import threading
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
        self.pvc_deleted: list[str] = []  # every PVC delete that was issued, in order
        self.pvc_patches: list[tuple[str, dict[str, Any]]] = []  # (name, body), in order
        self.pods: dict[tuple[str, str], Any] = {}  # (ns, name) -> pod-like object
        self.events: list[Any] = []  # event-like objects served by list_namespaced_event
        self.pod_logs: dict[str, str] = {}  # pod name -> log text served by read_namespaced_pod_log
        self.events_forbidden = False  # list_namespaced_event answers 403 (Role predates the rule)
        self.lock = threading.RLock()  # ConfigMap writes are atomic, like the API server's
        self._rv = 1000  # monotonically increasing resourceVersion source

    def _bump(self, cm: Any) -> None:
        self._rv += 1
        cm.metadata.resource_version = str(self._rv)

    def create_namespaced_config_map(self, namespace: str, body: Any) -> Any:
        with self.lock:
            self.create_calls.append(body.metadata.name)
            if (namespace, body.metadata.name) in self.configmaps:
                raise ApiException(status=409, reason="Conflict")
            if not body.metadata.uid:
                body.metadata.uid = uuid.uuid4().hex
            if body.metadata.creation_timestamp is None:
                body.metadata.creation_timestamp = datetime.datetime.now(datetime.timezone.utc)
            self._bump(body)
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

    def patch_namespaced_config_map(self, name: str, namespace: str, body: Any, **_: Any) -> Any:
        """Merge-patch of metadata.annotations / labels / binaryData from a dict body.

        Like the API server, a body carrying metadata.resourceVersion is a precondition (409
        when stale); one without is an unconditional blind write. A patch that changes
        something bumps the resourceVersion."""
        with self.lock:
            cm = self.read_namespaced_config_map(name=name, namespace=namespace)
            wanted = (body.get("metadata") or {}).get("resourceVersion")
            if wanted is not None and wanted != cm.metadata.resource_version:
                raise ApiException(status=409, reason="Conflict")
            changed = False
            for field in ("annotations", "labels"):
                patch = (body.get("metadata") or {}).get(field)
                if patch:
                    setattr(cm.metadata, field, {**(getattr(cm.metadata, field) or {}), **patch})
                    changed = True
            binary = body.get("binaryData")
            if binary:
                cm.binary_data = {**(cm.binary_data or {}), **binary}
                changed = True
            if changed:
                self._bump(cm)
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

    def list_namespace(self, label_selector: str = "") -> Any:
        """Honors the simple `k=v[,k=v...]` equality form of label selectors."""
        wanted = dict(p.split("=", 1) for p in label_selector.split(",") if p)
        items = [
            ns for ns in self.namespaces.values()
            if all((ns.metadata.labels or {}).get(k) == v for k, v in wanted.items())
        ]
        return SimpleNamespace(items=items)

    def patch_namespace(self, name: str, body: Any) -> Any:
        ns = self.read_namespace(name)
        meta = body.get("metadata", {})
        ns.metadata.labels = {**(ns.metadata.labels or {}), **meta.get("labels", {})}
        ns.metadata.annotations = {**(ns.metadata.annotations or {}), **meta.get("annotations", {})}
        return ns

    def delete_namespace(self, name: str) -> Any:
        """Deleting a namespace takes everything in it (what the real cascade does)."""
        self.actions.append(("delete", "Namespace", name))
        self.read_namespace(name)
        del self.namespaces[name]
        for table in (self.configmaps, self.services, self.service_accounts, self.pvcs, self.pods):
            for key in [k for k in table if k[0] == name]:
                del table[key]

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

    def patch_namespaced_service_account(self, name: str, namespace: str, body: Any) -> Any:
        """Merge-patch of metadata.annotations from a dict body."""
        self.actions.append(("patch", "ServiceAccount", name))
        sa = self.read_namespaced_service_account(name=name, namespace=namespace)
        patch = (body.get("metadata") or {}).get("annotations")
        if patch:
            sa.metadata.annotations = {**(sa.metadata.annotations or {}), **patch}
        return sa

    def create_namespaced_persistent_volume_claim(self, namespace: str, body: Any) -> Any:
        if (namespace, body.metadata.name) in self.pvcs:
            raise ApiException(status=409, reason="Conflict")
        if not body.metadata.uid:
            body.metadata.uid = uuid.uuid4().hex
        if body.metadata.creation_timestamp is None:
            body.metadata.creation_timestamp = datetime.datetime.now(datetime.timezone.utc)
        self.pvcs[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str) -> Any:
        # A SNAPSHOT, like a real API server's response: a caller holding an earlier read must
        # not see later patches (returning the stored object hid a stale-snapshot bug in the
        # read-only mount path, found live on GKE in Plan 10 Task 4).
        try:
            return copy.deepcopy(self.pvcs[(namespace, name)])
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def delete_namespaced_persistent_volume_claim(
        self, name: str, namespace: str, propagation_policy: str | None = None
    ) -> None:
        if (namespace, name) not in self.pvcs:
            raise ApiException(status=404, reason="Not Found")
        self.pvc_delete_policies[name] = propagation_policy
        self.pvc_deleted.append(name)
        del self.pvcs[(namespace, name)]

    def patch_namespaced_persistent_volume_claim(self, name: str, namespace: str, body: Any) -> Any:
        """JSON merge-patch of metadata.annotations / metadata.labels; a None value deletes a key."""
        self.pvc_patches.append((name, body))
        try:
            pvc = self.pvcs[(namespace, name)]  # the stored object itself: patches mutate it
        except KeyError:
            raise ApiException(status=404, reason="Not Found") from None
        for field in ("annotations", "labels"):
            patch = (body.get("metadata") or {}).get(field)
            if patch:
                merged = dict(getattr(pvc.metadata, field) or {})
                for key, value in patch.items():
                    if value is None:
                        merged.pop(key, None)
                    else:
                        merged[key] = value
                setattr(pvc.metadata, field, merged)
        return pvc

    def list_namespaced_persistent_volume_claim(self, namespace: str, label_selector: str = "") -> Any:
        wanted = dict(p.split("=", 1) for p in label_selector.split(",") if p)
        items = [
            v
            for (ns, _), v in self.pvcs.items()
            if ns == namespace
            and all((v.metadata.labels or {}).get(k) == val for k, val in wanted.items())
        ]
        return SimpleNamespace(items=copy.deepcopy(items))

    def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        try:
            return self.pods[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def list_namespaced_event(self, namespace: str, field_selector: str = "") -> Any:
        """Honors `a.b=v[,...]` field selectors over the event-like objects in ``self.events``."""
        if self.events_forbidden:
            raise ApiException(status=403, reason="Forbidden")
        wanted = dict(p.split("=", 1) for p in field_selector.split(",") if p)

        def get(ev: Any, path: str) -> Any:
            cur = ev
            for part in path.split("."):
                if part == "involvedObject":
                    part = "involved_object"
                cur = getattr(cur, part, None)
            return cur

        items = [e for e in self.events if all(get(e, k) == v for k, v in wanted.items())]
        return SimpleNamespace(items=items)

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        wanted = dict(p.split("=", 1) for p in label_selector.split(",") if p)
        items = [
            p
            for (ns, _), p in self.pods.items()
            if ns == namespace
            and all(((p.metadata.labels or {}) if p.metadata else {}).get(k) == v for k, v in wanted.items())
        ]
        return SimpleNamespace(items=items)

    def read_namespaced_pod_log(self, name: str, namespace: str, tail_lines: int | None = None) -> str:
        text = self.pod_logs.get(name, "")
        if tail_lines is None:
            return text
        return "\n".join(text.splitlines()[-tail_lines:])


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

    def list_namespaced_deployment(self, namespace: str, **_: Any) -> Any:
        return SimpleNamespace(items=[d for (ns, _n), d in self.deployments.items() if ns == namespace])

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
        self.created: dict[str, Any] = {}  # name -> V1StorageClass body, as created
        self.actions: list[tuple[str, str, str]] = []  # (verb, kind, name); 409s included

    def create_storage_class(self, body: Any) -> Any:
        name = body.metadata.name
        self.actions.append(("create", "StorageClass", name))
        if name in self.classes:
            raise ApiException(status=409, reason="Conflict")
        self.classes.add(name)
        self.created[name] = body
        return body

    def read_storage_class(self, name: str) -> Any:
        if name not in self.classes:
            raise ApiException(status=404, reason="Not Found")
        return SimpleNamespace(metadata=SimpleNamespace(name=name))
