"""Workflow anchors: one ConfigMap per workflow that owns everything it spawns.

Jobs and blob ConfigMaps carry an ownerReference to the anchor, so deleting the
anchor cascades through Kubernetes garbage collection. Object stores are not
covered by owner references, so delete_anchor also removes the workflow's blobs.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

from kubernetes import client
from kubernetes.client import ApiException

from sk8s.spec import MANAGED_BY

ANCHOR_KIND_LABEL = "sk8s/kind"
ANCHOR_KIND = "anchor"
WORKFLOW_LABEL = "sk8s/workflow-id"
DRIVER_JOB = "sk8s/driver-job"
DETACHED = "sk8s/detached"
CREATED_AT = "sk8s/created-at"
RETENTION_DAYS = "sk8s/retention-days"
STORE = "sk8s/store"
STORE_PREFIX = "sk8s/store-prefix"

log = logging.getLogger("sk8s.anchors")


def anchor_name(workflow_id: str) -> str:
    return f"sk8s-anchor-{workflow_id}"


def get_anchor(core_api: Any, *, namespace: str, workflow_id: str) -> Any | None:
    try:
        return core_api.read_namespaced_config_map(
            name=anchor_name(workflow_id), namespace=namespace
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def create_anchor(
    core_api: Any,
    *,
    namespace: str,
    workflow_id: str,
    store: str,
    store_prefix: str,
    retention_days: int,
    detached: bool,
) -> Any:
    """Create the workflow's anchor; if it already exists (409), return the existing one."""
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=anchor_name(workflow_id),
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": MANAGED_BY,
                WORKFLOW_LABEL: workflow_id,
                ANCHOR_KIND_LABEL: ANCHOR_KIND,
            },
            annotations={
                DRIVER_JOB: "",
                DETACHED: "true" if detached else "false",
                CREATED_AT: datetime.datetime.now(datetime.timezone.utc).isoformat(),
                RETENTION_DAYS: str(retention_days),
                STORE: store,
                STORE_PREFIX: store_prefix,
            },
        )
    )
    try:
        return core_api.create_namespaced_config_map(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
    existing = get_anchor(core_api, namespace=namespace, workflow_id=workflow_id)
    if existing is None:  # deleted between our 409 and the read: try once more
        return core_api.create_namespaced_config_map(namespace=namespace, body=body)
    return existing


def owner_ref(anchor: Any) -> Any:
    return client.V1OwnerReference(
        api_version="v1",
        kind="ConfigMap",
        name=anchor.metadata.name,
        uid=anchor.metadata.uid,
        controller=True,
    )


def set_annotation(core_api: Any, *, namespace: str, workflow_id: str, key: str, value: str) -> None:
    core_api.patch_namespaced_config_map(
        name=anchor_name(workflow_id),
        namespace=namespace,
        body={"metadata": {"annotations": {key: value}}},
    )


def delete_anchor(core_api: Any, store: Any, *, namespace: str, workflow_id: str) -> None:
    """Delete the anchor (Kubernetes cascades to owned objects) and the workflow's blobs.

    Best-effort: each step is attempted and logged on failure, never raised.
    """
    prefix = workflow_id  # blob keys are "<workflow_id>/..."; used if the anchor is unreadable
    try:
        anchor = get_anchor(core_api, namespace=namespace, workflow_id=workflow_id)
        recorded = ((anchor.metadata.annotations or {}).get(STORE_PREFIX) if anchor else None)
        if recorded:
            prefix = recorded
    except Exception:  # noqa: BLE001
        log.exception("could not read anchor for workflow %s", workflow_id)
    try:
        core_api.delete_namespaced_config_map(
            name=anchor_name(workflow_id), namespace=namespace, propagation_policy="Background"
        )
    except ApiException as e:
        if e.status != 404:
            log.exception("could not delete anchor for workflow %s", workflow_id)
    except Exception:  # noqa: BLE001
        log.exception("could not delete anchor for workflow %s", workflow_id)
    try:
        store.delete_prefix(prefix)
    except Exception:  # noqa: BLE001
        log.exception("could not delete blobs under %r for workflow %s", prefix, workflow_id)
