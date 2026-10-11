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
from sk8s.cluster import job_phase

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


def _is_live(batch_api: Any, namespace: str, workflow_id: str) -> bool:
    """True when any of the workflow's jobs is still active. Raises if it cannot tell."""
    jobs = batch_api.list_namespaced_job(
        namespace=namespace, label_selector=f"{anchors.WORKFLOW_LABEL}={workflow_id}"
    )
    return any(job_phase(job) == "active" for job in jobs.items)


def clean(
    namespace: str | None = None,
    *,
    older_than_days: float | None = None,
    workflow: str | None = None,
    core_api: Any | None = None,
    store: Any | None = None,
    batch_api: Any | None = None,
    exclude: Collection[str] = (),
) -> list[str]:
    """Delete expired workflows in ``namespace`` and return the deleted workflow ids.

    With ``workflow=<id>`` that one workflow is deleted unconditionally (even if live).
    Otherwise an anchor is deleted when its age exceeds ``older_than_days`` (if given;
    must be >= 0) or else its own ``sk8s/retention-days`` annotation, AND none of its
    jobs is still active. Younger or live workflows are kept; anchors with missing or
    malformed annotations, an unresolvable store, or a job listing that fails are
    skipped and logged (a failed safety check never falls through to a delete).
    ``exclude`` lists workflow ids the sweep never touches (a worker excludes its own).

    Each anchor's blobs are removed from the store named in its ``sk8s/store``
    annotation; pass ``store=`` to override that for every anchor.
    """
    if older_than_days is not None and older_than_days < 0:
        raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")
    if namespace is None:
        from sk8s import k8s

        namespace = k8s.current_namespace()
    if core_api is None:
        from sk8s import k8s

        core_api = k8s.core_api()

    stores: dict[str, Any] = {}

    def store_for(anchor: Any) -> Any | None:
        """The blob store for this anchor, or None (logged) if it cannot be built."""
        if store is not None:
            return store
        name = (anchor.metadata.annotations or {}).get(anchors.STORE)
        if not name:
            log.warning("skipping anchor %s: no %s annotation", anchor.metadata.name, anchors.STORE)
            return None
        if name not in stores:
            from sk8s.store import get_store

            try:
                stores[name] = get_store(name, namespace=namespace, core_api=core_api)
            except Exception:  # noqa: BLE001 — e.g. a backend this install does not have
                log.warning(
                    "skipping anchor %s: cannot open store %r", anchor.metadata.name, name,
                    exc_info=True,
                )
                stores[name] = None
        return stores[name]

    if workflow is not None:
        anchor = anchors.get_anchor(core_api, namespace=namespace, workflow_id=workflow)
        if anchor is None or not _is_anchor(anchor):
            return []
        target = store_for(anchor)
        if target is None:
            return []
        anchors.delete_anchor(core_api, target, namespace=namespace, workflow_id=workflow)
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
        try:
            if batch_api is None:
                from sk8s import k8s

                batch_api = k8s.batch_api()
            live = _is_live(batch_api, namespace, wf)
        except Exception:  # noqa: BLE001 — cannot prove it is idle: keep it
            log.warning("skipping anchor %s: could not check for active jobs", name, exc_info=True)
            continue
        if live:
            log.debug("keeping expired anchor %s: workflow has active jobs", name)
            continue
        target = store_for(cm)
        if target is None:
            continue
        anchors.delete_anchor(core_api, target, namespace=namespace, workflow_id=wf)
        deleted.append(wf)
    return deleted
