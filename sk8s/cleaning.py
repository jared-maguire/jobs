"""Retention: delete expired workflow anchors (and everything they own).

``clean()`` is run explicitly by users and, best-effort, by every worker before it
executes the user function. It only ever deletes *anchors* (via
``anchors.delete_anchor``: Kubernetes garbage collection cascades to the owned jobs
and blob ConfigMaps, and the object-store prefix is removed explicitly). Objects
labeled ``sk8s/kind=service`` are never selected or deleted.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Collection

from sk8s import anchors

log = logging.getLogger("sk8s.cleaning")


def _age_days(anchor: Any, now: datetime.datetime) -> float:
    created = datetime.datetime.fromisoformat(anchor.metadata.annotations[anchors.CREATED_AT])
    if created.tzinfo is None:  # we always write UTC; tolerate hand-edited naive stamps
        created = created.replace(tzinfo=datetime.timezone.utc)
    return (now - created).total_seconds() / 86400.0


def _limit_days(anchor: Any, older_than_days: float | None) -> float:
    if older_than_days is not None:
        return float(older_than_days)
    days = float(anchor.metadata.annotations[anchors.RETENTION_DAYS])
    if days < 0:
        raise ValueError(f"negative retention {days}")
    return days


def _is_anchor(obj: Any) -> bool:
    return ((obj.metadata.labels or {}).get(anchors.ANCHOR_KIND_LABEL)) == anchors.ANCHOR_KIND


def clean(
    namespace: str | None = None,
    *,
    older_than_days: float | None = None,
    workflow: str | None = None,
    core_api: Any | None = None,
    store: Any | None = None,
    exclude: Collection[str] = (),
) -> list[str]:
    """Delete expired workflows in ``namespace`` and return the deleted workflow ids.

    With ``workflow=<id>`` that one workflow is deleted unconditionally (if it exists).
    Otherwise an anchor is deleted when its age exceeds ``older_than_days`` (if given)
    or else its own ``sk8s/retention-days`` annotation; younger anchors are kept and
    anchors with missing or malformed annotations are skipped and logged. ``exclude``
    lists workflow ids that are never touched by the sweep (a worker excludes its own).
    ``core_api``/``store`` default to the current kube context and the configured store.
    """
    if namespace is None:
        from sk8s import k8s

        namespace = k8s.current_namespace()
    if core_api is None:
        from sk8s import k8s

        core_api = k8s.core_api()
    if store is None:
        from sk8s.config import load_config
        from sk8s.store import get_store

        store = get_store(load_config().store, namespace=namespace, core_api=core_api)

    if workflow is not None:
        anchor = anchors.get_anchor(core_api, namespace=namespace, workflow_id=workflow)
        if anchor is None or not _is_anchor(anchor):
            return []
        anchors.delete_anchor(core_api, store, namespace=namespace, workflow_id=workflow)
        return [workflow]

    items: list[Any] = core_api.list_namespaced_config_map(
        namespace=namespace, label_selector=f"{anchors.ANCHOR_KIND_LABEL}={anchors.ANCHOR_KIND}"
    ).items
    now = datetime.datetime.now(datetime.timezone.utc)
    deleted: list[str] = []
    for cm in items:
        if not _is_anchor(cm):  # belt and braces: never act on anything but anchors
            continue
        name = cm.metadata.name
        wf = (cm.metadata.labels or {}).get(anchors.WORKFLOW_LABEL)
        if not wf:
            log.warning("skipping anchor %s: no %s label", name, anchors.WORKFLOW_LABEL)
            continue
        if wf in exclude:
            continue
        try:
            expired = _age_days(cm, now) > _limit_days(cm, older_than_days)
        except (KeyError, TypeError, ValueError):
            log.warning("skipping anchor %s: malformed or missing annotations", name, exc_info=True)
            continue
        if not expired:
            continue
        anchors.delete_anchor(core_api, store, namespace=namespace, workflow_id=wf)
        deleted.append(wf)
    return deleted
