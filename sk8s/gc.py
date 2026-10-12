"""Mark-and-sweep of the artifact CAS (SPEC 3.3). No live refcounts, ever.

Roots (what keeps a CAS object alive):

* every ``workflows/**/artifacts.json`` reference set (written BEFORE first use);
* every memo entry younger than the memo retention (the memo's promise is "the outputs still
  exist"; see sk8s.memo) - a deliberate extension of the spec's two root kinds;
* a published ref: an object that has been published with ``copy=True`` (the index lists a
  ``published`` location next to the CAS copy) is kept, because the user asked for both. A
  published MOVE has no CAS object left to collect.

An unreachable CAS object is deleted only if it is older than ``min_age`` (both its own
modification time and the index entry's ``last_referenced``). "When in doubt, don't delete":
any parse failure, listing failure or odd manifest ABORTS the whole sweep before the first
delete, and roots are re-read immediately before deleting so a workflow that adopted an old
object while the sweep was running is honoured.

Known residual race (documented, loud rather than silent): a workflow that starts referencing an
old unreferenced object between this object's final index re-read and its delete (one round
trip) loses it; its staging then fails with ArtifactNotFound.
"""
from __future__ import annotations

import datetime
import json
import logging
import posixpath
from dataclasses import dataclass, field
from typing import Any

from sk8s import cas
from sk8s.errors import BlobNotFound, Sk8sError
from sk8s.store.base import BlobStore

log = logging.getLogger("sk8s.gc")

# Same family as cleaning._MIN_ANCHOR_AGE_SECONDS: nothing younger than this is ever collected.
CAS_MIN_AGE_SECONDS = 300.0
MEMO_RETENTION_DAYS = 7.0
_REMARK_EVERY = 10  # deletes between root re-reads


class SweepAborted(Sk8sError):
    """The mark phase met something it could not prove safe; nothing was deleted."""


@dataclass
class SweepReport:
    scanned: int = 0
    deleted: list[str] = field(default_factory=list)  # sha256s whose CAS object was deleted
    deleted_bytes: int = 0
    kept_reachable: int = 0
    kept_young: int = 0
    kept_published: int = 0
    memo_expired: list[str] = field(default_factory=list)
    aborted: str | None = None  # why nothing was deleted, if the sweep gave up
    dry_run: bool = False

    def merge(self, other: SweepReport) -> None:
        self.scanned += other.scanned
        self.deleted += other.deleted
        self.deleted_bytes += other.deleted_bytes
        self.kept_reachable += other.kept_reachable
        self.kept_young += other.kept_young
        self.kept_published += other.kept_published
        self.memo_expired += other.memo_expired
        self.dry_run = self.dry_run or other.dry_run
        if other.aborted:
            self.aborted = other.aborted if not self.aborted else f"{self.aborted}; {other.aborted}"

    def summary(self) -> str:
        verb = "would delete" if self.dry_run else "deleted"
        if self.aborted:
            return f"artifact sweep skipped, nothing deleted: {self.aborted}"
        return (
            f"artifact sweep: {verb} {len(self.deleted)} unreachable object(s) "
            f"({self.deleted_bytes} bytes) of {self.scanned} scanned; kept "
            f"{self.kept_reachable} referenced, {self.kept_published} published, "
            f"{self.kept_young} too young; expired {len(self.memo_expired)} memo entr(ies)"
        )


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _read_json(store: BlobStore, key: str) -> dict[str, Any] | None:
    try:
        raw = store.get(store.uri_for(key))
    except BlobNotFound:
        return None  # deleted while we listed: not a root any more
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise SweepAborted(f"{key} is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise SweepAborted(f"{key} is not a JSON object")
    return doc


def _hashes(key: str, values: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(values, list):
        raise SweepAborted(f"{key}: expected a list of hashes")
    for v in values:
        try:
            out.add(cas.check_hash(str(v)))
        except ValueError as exc:
            raise SweepAborted(f"{key}: {exc}") from exc
    return out


def mark(
    store: BlobStore, now: datetime.datetime, memo_retention_days: float
) -> tuple[set[str], list[str]]:
    """(reachable hashes, expired memo entry keys). Raises SweepAborted on anything odd."""
    roots: set[str] = set()
    expired: list[str] = []
    for info in store.list_blobs(cas.WORKFLOWS_PREFIX):
        if posixpath.basename(info.key) != cas.MANIFEST_NAME:
            continue
        doc = _read_json(store, info.key)
        if doc is not None:
            roots |= _hashes(info.key, doc.get("refs", []))
    ttl = datetime.timedelta(days=memo_retention_days)
    for info in store.list_blobs(cas.MEMO_PREFIX):
        doc = _read_json(store, info.key)
        if doc is None:
            continue
        created = cas._parse_ts(doc.get("created_at"))
        if created is None:
            raise SweepAborted(f"{info.key} has no readable created_at")
        if now - created >= ttl:
            expired.append(info.key)
            continue
        outputs = doc.get("outputs", [])
        if not isinstance(outputs, list):
            raise SweepAborted(f"{info.key}: outputs is not a list")
        roots |= _hashes(info.key, [o["sha256"] for o in outputs if isinstance(o, dict) and o.get("sha256")])
    return roots, expired


def sweep(
    store: BlobStore,
    *,
    now: datetime.datetime | None = None,
    min_age_seconds: float | None = None,
    memo_retention_days: float | None = None,
    dry_run: bool = False,
) -> SweepReport:
    """Collect unreachable CAS objects of ``store``. Never raises: an unsafe situation aborts
    the sweep (see SweepReport.aborted) with nothing deleted."""
    report = SweepReport(dry_run=dry_run)
    when = now or _utc_now()
    floor = datetime.timedelta(seconds=CAS_MIN_AGE_SECONDS if min_age_seconds is None else min_age_seconds)
    memo_retention_days = MEMO_RETENTION_DAYS if memo_retention_days is None else memo_retention_days
    try:
        roots, expired_memos = mark(store, when, memo_retention_days)
        candidates: list[tuple[str, str, int]] = []  # (hash, key, size)
        for info in store.list_blobs(cas.CAS_PREFIX):
            report.scanned += 1
            sha = posixpath.basename(info.key)
            try:
                cas.check_hash(sha)
            except ValueError:
                log.warning("ignoring %r in the CAS: not a sha256 key", info.key)
                continue
            if sha in roots:
                report.kept_reachable += 1
            elif info.updated is None or when - info.updated < floor:
                report.kept_young += 1  # unknown age counts as young: when in doubt, keep
            else:
                candidates.append((sha, info.key, info.size))
        survivors: list[tuple[str, str, int]] = []
        for sha, key, size in candidates:
            idx = _read_json(store, cas.index_key(sha))
            last = cas._parse_ts((idx or {}).get("last_referenced"))
            if last is not None and when - last < floor:
                report.kept_young += 1
            elif any(loc.get("kind") == "published" for loc in (idx or {}).get("locations", [])):
                report.kept_published += 1
            else:
                survivors.append((sha, key, size))
        if not dry_run and survivors:
            # Re-mark immediately before deleting: a workflow that recorded one of these hashes
            # while we were scanning is a root now.
            roots2, _ = mark(store, _utc_now() if now is None else when, memo_retention_days)
            for n, (sha, key, size) in enumerate(survivors):
                if n and n % _REMARK_EVERY == 0:  # the delete loop is long: keep the roots fresh
                    roots2, _ = mark(store, _utc_now() if now is None else when, memo_retention_days)
                if sha in roots2:
                    report.kept_reachable += 1
                    continue
                # Re-read the index immediately before this delete: a workflow that adopted the
                # object (touch, then record) since the scan makes it young again.
                idx = _read_json(store, cas.index_key(sha))
                last = cas._parse_ts((idx or {}).get("last_referenced"))
                if last is not None and (_utc_now() if now is None else when) - last < floor:
                    report.kept_young += 1
                    continue
                uri = store.uri_for(key)
                store.delete(uri)
                report.deleted.append(sha)
                report.deleted_bytes += size
                _retire_index(store, sha, uri)
        elif dry_run:
            report.deleted = [s for s, _, _ in survivors]
            report.deleted_bytes = sum(sz for _, _, sz in survivors)
        if not dry_run:
            for key in expired_memos:
                store.delete(store.uri_for(key))
        report.memo_expired = [posixpath.basename(k) for k in expired_memos]
    except SweepAborted as exc:
        report.aborted = str(exc)
        report.deleted, report.deleted_bytes = [], 0
        log.warning("artifact sweep aborted, nothing deleted: %s", exc)
    except Exception as exc:  # noqa: BLE001 — a failed safety check never falls through to a delete
        report.aborted = f"{type(exc).__name__}: {exc}"
        log.warning("artifact sweep failed", exc_info=True)
    return report


def _retire_index(store: BlobStore, sha: str, cas_uri: str) -> None:
    """Drop the deleted object's CAS location from its index entry; an entry left with no
    location at all is garbage and goes too."""
    from sk8s.store.retry import update_blob

    def mutate(current: bytes | None) -> bytes | None:
        if current is None:
            return None
        doc = cas.merge_index(json.loads(current), sha256=sha, size=None, remove=[("cas", cas_uri)])
        return None if doc is None else cas._dump(doc)

    try:
        update_blob(store, cas.index_key(sha), mutate)
        idx = _read_json(store, cas.index_key(sha))
        if idx is not None and not idx.get("locations"):
            store.delete(store.uri_for(cas.index_key(sha)))
    except Exception:  # noqa: BLE001 — index tidying is best-effort; the object is already gone
        log.warning("could not tidy the index entry of %s", sha, exc_info=True)
