from __future__ import annotations

import base64
import re
from typing import Any, Iterator

from kubernetes import client
from kubernetes.client import ApiException

from sk8s.errors import BlobNotFound, BlobTooLarge
from sk8s.store.base import BlobInfo, BlobStat, BlobStore

MAX_CONFIGMAP_BLOB = 950_000
MANAGED_BY = {"app.kubernetes.io/managed-by": "sk8s"}
KEY_ANNOTATION = "sk8s/key"  # original blob key; names are lossy after sanitizing
SERVICE_KEY_ROOT = "svc"  # keys under svc/<name>/ belong to a service, not a workflow


def _blob_labels(key: str) -> dict[str, str]:
    """Labels for a blob's ConfigMap: service blobs carry service labels, others a workflow id."""
    root, _, rest = key.partition("/")
    if root == SERVICE_KEY_ROOT:
        labels = {**MANAGED_BY, "sk8s/kind": "service"}
        name = rest.split("/", 1)[0]
        if name:  # a bare "svc" prefix (delete_prefix) spans every service
            labels["sk8s/service"] = name
        return labels
    return {**MANAGED_BY, "sk8s/workflow-id": root}


def check_blob_size(key: str, data: bytes) -> None:
    """Raise ``BlobTooLarge`` if ``data`` would not fit in a ConfigMap (also used by dry runs)."""
    if len(data) > MAX_CONFIGMAP_BLOB:
        raise BlobTooLarge(
            f"blob for {key!r} is {len(data)} bytes; the ConfigMap store caps at "
            f"{MAX_CONFIGMAP_BLOB}. Configure an object store (s3:// or gs://) — "
            f"see `sk8s config --store`."
        )


def _sanitize(key: str) -> str:
    name = "sk8s-" + re.sub(r"[^a-z0-9-]+", "-", key.lower()).strip("-")
    return name[:253]


class ConfigMapStore(BlobStore):
    """Zero-setup default store. One ConfigMap per blob. URI: k8s://<ns>/<name>."""

    def __init__(self, namespace: str, core_api: Any) -> None:
        self.namespace = namespace
        self.core_api = core_api

    def uri_for(self, key: str) -> str:
        return f"k8s://{self.namespace}/{_sanitize(key)}"

    def _check_size(self, key: str, data: bytes) -> None:
        check_blob_size(key, data)

    def _body(self, key: str, data: bytes, owner: Any | None) -> Any:
        meta = client.V1ObjectMeta(
            name=_sanitize(key),
            labels=_blob_labels(key),
            annotations={KEY_ANNOTATION: key},
            owner_references=[owner] if owner is not None else None,
        )
        return client.V1ConfigMap(
            metadata=meta, binary_data={"blob": base64.b64encode(data).decode()}
        )

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        self._check_size(key, data)
        body = self._body(key, data, owner)
        name = body.metadata.name
        try:
            self.core_api.create_namespaced_config_map(namespace=self.namespace, body=body)
        except ApiException as e:
            if e.status == 409:  # retried worker overwrites its previous attempt
                self.core_api.delete_namespaced_config_map(name=name, namespace=self.namespace)
                self.core_api.create_namespaced_config_map(namespace=self.namespace, body=body)
            else:
                raise
        return self.uri_for(key)

    # --- conditional writes: create-only = create-if-absent; resourceVersion = the version token.
    # SPIKE-CCW E7: replace/patch WITHOUT a resourceVersion is a blind write, so put_if_version
    # always sends one (merge-patch with metadata.resourceVersion in the body: 409 when stale).
    def put_if_absent(self, key: str, data: bytes, owner: Any | None = None) -> bool:
        self._check_size(key, data)
        body = self._body(key, data, owner)
        try:
            self.core_api.create_namespaced_config_map(namespace=self.namespace, body=body)
        except ApiException as e:
            if e.status == 409:  # AlreadyExists: the loser's create is a no-op
                return False
            raise
        return True

    def read_with_version(self, key: str) -> tuple[bytes, str]:
        uri = self.uri_for(key)
        ns, name = self._split(uri)
        try:
            cm = self.core_api.read_namespaced_config_map(name=name, namespace=ns)
        except ApiException as e:
            if e.status == 404:
                raise BlobNotFound(uri) from None
            raise
        return base64.b64decode(cm.binary_data["blob"]), str(cm.metadata.resource_version)

    def put_if_version(self, key: str, data: bytes, version: str) -> bool:
        self._check_size(key, data)
        body = {
            "metadata": {"resourceVersion": version},
            "binaryData": {"blob": base64.b64encode(data).decode()},
        }
        try:
            self.core_api.patch_namespaced_config_map(
                name=_sanitize(key), namespace=self.namespace, body=body,
                _content_type="application/merge-patch+json",
            )
        except ApiException as e:
            if e.status in (404, 409):  # gone, or modified since the version was read
                return False
            raise
        return True

    def _split(self, uri: str) -> tuple[str, str]:
        assert uri.startswith("k8s://"), uri
        ns, name = uri[len("k8s://"):].split("/", 1)
        return ns, name

    def get(self, uri: str) -> bytes:
        ns, name = self._split(uri)
        try:
            cm = self.core_api.read_namespaced_config_map(name=name, namespace=ns)
        except ApiException as e:
            if e.status == 404:
                raise BlobNotFound(uri) from None
            raise
        return base64.b64decode(cm.binary_data["blob"])

    def stat(self, uri: str) -> BlobStat | None:
        ns, name = self._split(uri)
        try:
            cm = self.core_api.read_namespaced_config_map(name=name, namespace=ns)
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        return BlobStat(
            uri=uri, size=len(base64.b64decode(cm.binary_data["blob"])),
            version=str(cm.metadata.resource_version), updated=cm.metadata.creation_timestamp,
        )

    def list_blobs(self, prefix: str) -> Iterator[BlobInfo]:
        root = prefix.split("/", 1)[0]  # the first segment is a label (see _blob_labels)
        selector = ",".join(f"{k}={v}" for k, v in _blob_labels(root).items())
        cms = self.core_api.list_namespaced_config_map(
            namespace=self.namespace, label_selector=selector
        )
        for cm in cms.items:
            key = (cm.metadata.annotations or {}).get(KEY_ANNOTATION, "")
            if key.startswith(prefix):
                yield BlobInfo(
                    key=key, size=len(base64.b64decode(cm.binary_data["blob"])),
                    updated=cm.metadata.creation_timestamp,
                )

    def delete(self, uri: str) -> None:
        ns, name = self._split(uri)
        try:
            self.core_api.delete_namespaced_config_map(name=name, namespace=ns)
        except ApiException as e:
            if e.status != 404:
                raise

    def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        # The first segment (workflow id, or svc/<name>) is stored by put() as labels;
        # narrow by label server-side, then match whole segments on the exact key
        # (sanitized names are lossy, so they can't express the boundary).
        selector = ",".join(f"{k}={v}" for k, v in _blob_labels(prefix).items())
        cms = self.core_api.list_namespaced_config_map(
            namespace=self.namespace, label_selector=selector
        )
        for cm in cms.items:
            key = (cm.metadata.annotations or {}).get(KEY_ANNOTATION, "")
            if key == prefix or key.startswith(prefix + "/"):
                self.delete(f"k8s://{self.namespace}/{cm.metadata.name}")

    def count_prefix(self, prefix: str) -> int:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("count_prefix requires a non-empty prefix")
        selector = ",".join(f"{k}={v}" for k, v in _blob_labels(prefix).items())
        cms = self.core_api.list_namespaced_config_map(
            namespace=self.namespace, label_selector=selector
        )
        count = 0
        for cm in cms.items:
            key = (cm.metadata.annotations or {}).get(KEY_ANNOTATION, "")
            if key == prefix or key.startswith(prefix + "/"):
                count += 1
        return count
