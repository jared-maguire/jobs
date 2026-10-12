"""Retention: delete expired workflow anchors (and everything they own).

``clean()`` is run explicitly by users and, best-effort, by every worker before it
executes the user function. It only ever deletes *anchors* (via
``anchors.delete_anchor``: Kubernetes garbage collection cascades to the owned jobs
and blob ConfigMaps, and the object-store prefix is removed explicitly). Objects
labeled ``sk8s/kind=service`` are never selected or deleted.
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
from typing import Any, Collection

from sk8s import anchors
from sk8s.cluster import job_phase

log = logging.getLogger("sk8s.cleaning")

# Never reap an anchor younger than this, whatever retention / older_than_days say
# (the workflow= force path is exempt). Closes two windows: (1) between a client creating
# an anchor and its first Job existing (and its driver being recorded), when "no active
# jobs" is vacuously true; (2) retention_days=0 / older_than_days=0 footguns that would
# otherwise make every worker's start-up sweep delete brand-new workflows.
_MIN_ANCHOR_AGE_SECONDS = 300.0


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


@dataclasses.dataclass
class CleanReport:
    """What a sweep did: the workflow ids deleted, and for every workflow it looked at and
    kept, ``{"workflow": id, "reason": why}``."""

    deleted: list[str] = dataclasses.field(default_factory=list)
    kept: list[dict[str, str]] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"deleted": list(self.deleted), "kept": [dict(k) for k in self.kept]}


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

    See :func:`clean_report` for the rules; this is its ``deleted`` list.
    """
    return clean_report(
        namespace, older_than_days=older_than_days, workflow=workflow, core_api=core_api,
        store=store, batch_api=batch_api, exclude=exclude,
    ).deleted


def clean_report(
    namespace: str | None = None,
    *,
    older_than_days: float | None = None,
    workflow: str | None = None,
    core_api: Any | None = None,
    store: Any | None = None,
    batch_api: Any | None = None,
    exclude: Collection[str] = (),
) -> CleanReport:
    """Delete expired workflows in ``namespace``; report what was deleted and what was kept, why.

    With ``workflow=<id>`` that one workflow is deleted unconditionally (even if live).
    Otherwise an anchor is deleted when its age exceeds ``older_than_days`` (if given;
    must be >= 0) or else its own ``sk8s/retention-days`` annotation, AND none of its
    jobs is still active. Younger or live workflows are kept; anchors with missing or
    malformed annotations, an unresolvable store, or a job listing that fails are
    skipped and logged (a failed safety check never falls through to a delete).
    An anchor younger than 300 seconds is never deleted this way, whatever the limit.
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

    report = CleanReport()
    stores: dict[str, Any] = {}

    def keep(wf: str, reason: str) -> None:
        report.kept.append({"workflow": wf, "reason": reason})

    def store_for(anchor: Any) -> tuple[Any | None, str]:
        """(blob store for this anchor, "") or (None, why not) — the problem is also logged."""
        if store is not None:
            return store, ""
        name = (anchor.metadata.annotations or {}).get(anchors.STORE)
        if not name:
            log.warning("skipping anchor %s: no %s annotation", anchor.metadata.name, anchors.STORE)
            return None, f"its anchor has no {anchors.STORE} annotation, so its blobs cannot be located"
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
        if stores[name] is None:
            return None, f"cannot open its store {name!r} (missing extra or credentials?)"
        return stores[name], ""

    if workflow is not None:
        anchor = anchors.get_anchor(core_api, namespace=namespace, workflow_id=workflow)
        if anchor is None or not _is_anchor(anchor):
            keep(workflow, "no such workflow in this namespace")
            return report
        target, why = store_for(anchor)
        if target is None:
            keep(workflow, why)
            return report
        anchors.delete_anchor(core_api, target, namespace=namespace, workflow_id=workflow)
        report.deleted.append(workflow)
        return report

    items: list[Any] = core_api.list_namespaced_config_map(
        namespace=namespace, label_selector=f"{anchors.ANCHOR_KIND_LABEL}={anchors.ANCHOR_KIND}"
    ).items
    now = datetime.datetime.now(datetime.timezone.utc)
    for cm in items:
        if not _is_anchor(cm):  # belt and braces: never act on anything but anchors
            continue
        name = cm.metadata.name
        wf = (cm.metadata.labels or {}).get(anchors.WORKFLOW_LABEL)
        if not wf:
            log.warning("skipping anchor %s: no %s label", name, anchors.WORKFLOW_LABEL)
            keep(name, f"its anchor has no {anchors.WORKFLOW_LABEL} label")
            continue
        if wf in exclude:
            keep(wf, "excluded from this sweep (the calling worker's own workflow)")
            continue
        try:
            age = _age_days(cm, now)
            limit = _limit_days(cm, older_than_days)
            over = age > limit
            expired = over and (age * 86400.0 >= _MIN_ANCHOR_AGE_SECONDS)
        except (KeyError, TypeError, ValueError):
            log.warning("skipping anchor %s: malformed or missing annotations", name, exc_info=True)
            keep(wf, "its anchor has malformed or missing annotations (never deleted on a guess)")
            continue
        if not expired:
            if over:
                keep(wf, f"younger than the {int(_MIN_ANCHOR_AGE_SECONDS)}s minimum age no sweep goes below")
            else:
                keep(wf, f"not expired: {age:.2f} days old, limit {limit:g} days")
            continue
        try:
            if batch_api is None:
                from sk8s import k8s

                batch_api = k8s.batch_api()
            live = _is_live(batch_api, namespace, wf)
        except Exception:  # noqa: BLE001 — cannot prove it is idle: keep it
            log.warning("skipping anchor %s: could not check for active jobs", name, exc_info=True)
            keep(wf, "expired, but its jobs could not be listed to prove it idle")
            continue
        if live:
            log.debug("keeping expired anchor %s: workflow has active jobs", name)
            keep(wf, "expired, but the workflow still has active jobs")
            continue
        target, why = store_for(cm)
        if target is None:
            keep(wf, f"expired, but {why}")
            continue
        anchors.delete_anchor(core_api, target, namespace=namespace, workflow_id=wf)
        report.deleted.append(wf)
    return report
