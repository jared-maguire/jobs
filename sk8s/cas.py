"""The content-addressed artifact store: layout, index manifests, workflow reference sets.

Layout (all keys relative to the configured store; SPEC-DRAFT section 3.1)::

    artifacts/cas/sha256/<hh>/<hash>            the bytes, immutable
    artifacts/index/sha256/<hh>/<hash>.json     manifest: locations, size, checksums, provenance
    artifacts/memo/<key>                        memoization entries (see sk8s.memo)
    workflows/<wf>/artifacts.json               the workflow's reference set (GC roots)
    workflows/<wf>/<job>/artifacts.json         one writer's shard of the same set (see below)

Every index/manifest write is an optimistic, preconditioned read-modify-write through
``sk8s.store.retry.update_blob`` and every mutation is an idempotent set-style merge.

Deviation from the spec text, on purpose: the reference set is sharded per writer. One
``workflows/<wf>/artifacts.json`` appended to by every pod of a 1000-way map is exactly the
"hot key" SPIKE-CCW warns about (~5 lost races per success at 10-way). Clients record into the
workflow file; each job records its outputs into its own ``<job>`` shard (a single writer, so
no contention). The GC mark phase unions every ``artifacts.json`` under ``workflows/``.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from sk8s.errors import BlobNotFound, StoreError
from sk8s.store import get_store
from sk8s.store.base import CHUNK, BlobStat, BlobStore
from sk8s.store.retry import update_blob

if TYPE_CHECKING:
    from sk8s.artifacts import Artifact

log = logging.getLogger("sk8s.cas")

INDEX_SCHEMA = 1
MANIFEST_SCHEMA = 1
# Do not re-write an index entry's ``last_referenced`` more often than this: touching is a
# protective timestamp for the GC age floor, not a counter.
TOUCH_INTERVAL_SECONDS = 60.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --- layout ---------------------------------------------------------------------------


def check_hash(sha256: str) -> str:
    if not _HEX64.match(sha256):
        raise ValueError(f"not a sha256 hex digest: {sha256!r}")
    return sha256


def cas_key(sha256: str) -> str:
    check_hash(sha256)
    return f"artifacts/cas/sha256/{sha256[:2]}/{sha256}"


def index_key(sha256: str) -> str:
    check_hash(sha256)
    return f"artifacts/index/sha256/{sha256[:2]}/{sha256}.json"


def memo_entry_key(memo_key: str) -> str:
    check_hash(memo_key)
    return f"artifacts/memo/{memo_key}"


def manifest_key(workflow: str, job: str | None = None) -> str:
    return f"workflows/{workflow}/{job}/artifacts.json" if job else f"workflows/{workflow}/artifacts.json"


CAS_PREFIX = "artifacts/cas/sha256/"
INDEX_PREFIX = "artifacts/index/sha256/"
MEMO_PREFIX = "artifacts/memo/"
WORKFLOWS_PREFIX = "workflows/"
MANIFEST_NAME = "artifacts.json"


def hash_file(path: Path) -> tuple[str, int]:
    """(sha256 hex, size) of a local file, streamed."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(ts: datetime.datetime) -> str:
    return ts.isoformat()


def _parse_ts(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)


# --- reaching URIs --------------------------------------------------------------------

_bucket_stores: dict[tuple[str, str, str], BlobStore] = {}
_bucket_lock = threading.Lock()
_gcs_client: Any = None
_s3_clients: dict[str, Any] = {}


def _endpoint() -> str | None:
    return os.environ.get("SK8S_S3_ENDPOINT") or os.environ.get("SK8S_STORE_ENDPOINT") or None


def _new_bucket_store(scheme: str, bucket: str) -> BlobStore:
    """A prefix-less store over one bucket (any key of it is reachable). Clients are shared."""
    global _gcs_client
    if scheme == "gs":
        from sk8s.store.gcs import GCSStore

        if _gcs_client is None:
            from google.cloud import storage

            _gcs_client = storage.Client()
        return GCSStore(bucket, client=_gcs_client)
    if scheme == "s3":
        from sk8s.store.s3 import S3Store

        endpoint = _endpoint() or ""
        if endpoint not in _s3_clients:
            import boto3

            _s3_clients[endpoint] = boto3.client("s3", **({"endpoint_url": endpoint} if endpoint else {}))
        return S3Store(bucket, client=_s3_clients[endpoint])
    raise ValueError(f"unsupported scheme {scheme!r}")


def normalize_uri(uri: str) -> str:
    """Canonical form of a location: gs://, s3://, k8s:// pass through; a filesystem path or
    file:// URL becomes ``local:/abs/path``."""
    if uri.startswith(("gs://", "s3://", "k8s://", "local:")):
        return uri
    if uri.startswith("file://"):
        return "local:" + uri[len("file://"):]
    if uri.startswith("/"):
        return "local:" + uri
    raise ValueError(
        f"unsupported artifact location {uri!r}: use gs://, s3://, an absolute path or local:/path"
    )


def split_uri(uri: str) -> tuple[str, str, str]:
    """(scheme, bucket, key) of a normalized uri; bucket is "" for local."""
    uri = normalize_uri(uri)
    if uri.startswith("local:"):
        return "local", "", uri[len("local:"):].lstrip("/")
    scheme, _, rest = uri.partition("://")
    bucket, _, key = rest.partition("/")
    return scheme, bucket, key


def store_for_uri(uri: str, hint: BlobStore | None = None) -> tuple[BlobStore, str]:
    """A store that can read/write ``uri``, and the key of the object in it.

    Object-store and local URIs are self-describing; a k8s:// URI is only reachable through
    the ConfigMap store that ``hint`` carries."""
    uri = normalize_uri(uri)
    scheme, bucket, key = split_uri(uri)
    if scheme == "local":
        from sk8s.store.local import LocalStore

        return LocalStore("/"), key
    if scheme == "k8s":
        if hint is None:
            from sk8s.store import get_store as _get

            ns = uri[len("k8s://"):].split("/", 1)[0]
            hint = _get("k8s", namespace=ns)
        return hint, key
    with _bucket_lock:
        cached = _bucket_stores.get((scheme, bucket, _endpoint() or ""))
        if cached is None:
            cached = _new_bucket_store(scheme, bucket)
            _bucket_stores[(scheme, bucket, _endpoint() or "")] = cached
    return cached, key


# --- index manifests -----------------------------------------------------------------


def _loc_id(loc: dict[str, Any]) -> tuple[str, str]:
    return (str(loc.get("kind")), str(loc.get("uri")))


def merge_index(
    current: dict[str, Any] | None,
    *,
    sha256: str,
    size: int | None,
    add: Iterable[dict[str, Any]] = (),
    remove: Iterable[tuple[str, str]] = (),
    provenance: dict[str, Any] | None = None,
    touch: datetime.datetime | None = None,
) -> dict[str, Any] | None:
    """Pure, idempotent merge for ``update_blob``: the new manifest, or None if nothing changes.

    Locations are a set keyed by (kind, uri): adding re-states, removing is idempotent.
    Provenance is set-once (the first producer wins). ``touch`` refreshes ``last_referenced``
    unless it is already fresher than TOUCH_INTERVAL_SECONDS."""
    doc: dict[str, Any] = json.loads(json.dumps(current)) if current else {
        "schema": INDEX_SCHEMA, "sha256": sha256, "size": size, "checksums": {"sha256": sha256},
        "locations": [], "provenance": {}, "created_at": _iso(_now()),
    }
    changed = current is None
    if size is not None and doc.get("size") is None:
        doc["size"] = size
        changed = True
    by_id = {_loc_id(loc): loc for loc in doc["locations"]}
    for loc in add:
        if by_id.get(_loc_id(loc)) != loc:
            by_id[_loc_id(loc)] = dict(loc)
            changed = True
    for ident in remove:
        if ident in by_id:
            del by_id[ident]
            changed = True
    doc["locations"] = [by_id[k] for k in sorted(by_id)]
    if provenance and not doc.get("provenance"):
        doc["provenance"] = provenance
        changed = True
    if touch is not None:
        last = _parse_ts(doc.get("last_referenced"))
        if last is None or (touch - last).total_seconds() >= TOUCH_INTERVAL_SECONDS:
            doc["last_referenced"] = _iso(touch)
            changed = True
    return doc if changed else None


def location(kind: str, uri: str, version: str | None = None, **extra: Any) -> dict[str, Any]:
    loc: dict[str, Any] = {"kind": kind, "uri": uri}
    if version is not None:
        loc["version"] = version
    loc.update(extra)
    return loc


# --- the artifact store ---------------------------------------------------------------


def _dump(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Resolved:
    """A readable location of an artifact."""

    uri: str
    kind: str
    version: str | None
    stat: BlobStat


class ArtifactStore:
    """The ``artifacts/`` and ``workflows/`` namespaces of one BlobStore."""

    def __init__(self, store: BlobStore, spec: str) -> None:
        self.store = store
        self.spec = spec  # how to reopen this store; recorded on every Artifact it mints
        self._recorded: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    @classmethod
    def from_spec(cls, spec: str, *, namespace: str | None = None, core_api: Any | None = None,
                  endpoint: str | None = None) -> ArtifactStore:
        store = get_store(spec, namespace=namespace, core_api=core_api, endpoint=endpoint or _endpoint())
        return cls(store, spec)

    # --- index ---------------------------------------------------------------------
    def read_index(self, sha256: str) -> dict[str, Any] | None:
        try:
            data, _ = self.store.read_with_version(index_key(sha256))
        except BlobNotFound:
            return None
        doc = json.loads(data)
        return doc if isinstance(doc, dict) else None

    def update_index(
        self,
        sha256: str,
        *,
        size: int | None = None,
        add: Iterable[dict[str, Any]] = (),
        remove: Iterable[tuple[str, str]] = (),
        provenance: dict[str, Any] | None = None,
        touch: bool = False,
    ) -> None:
        adds, removes = list(add), list(remove)
        stamp = _now() if touch else None

        def mutate(current: bytes | None) -> bytes | None:
            doc = merge_index(
                json.loads(current) if current else None, sha256=sha256, size=size,
                add=adds, remove=removes, provenance=provenance, touch=stamp,
            )
            return None if doc is None else _dump(doc)

        update_blob(self.store, index_key(sha256), mutate)

    # --- bytes -----------------------------------------------------------------------
    def put_file(
        self,
        path: Path,
        *,
        name: str | None = None,
        workflow: str | None = None,
        job: str | None = None,
        inputs: Iterable[str] = (),
    ) -> Artifact:
        """Hash a file, record the intent, make sure the CAS holds it, return the Artifact.

        Order matters: hash first (the CAS key depends on it), then record the hash in the
        workflow's reference set BEFORE the bytes land (record-intent-first: a sweeper can never
        see the object unreferenced), then upload only if the key is not already there, then
        index it. Re-uploading identical content is therefore a no-op, which makes the
        two-clients race resolve itself: the loser's create-if-absent is refused and harmless."""
        from sk8s.artifacts import Artifact

        sha, size = hash_file(path)
        if workflow:
            self.record_refs(workflow, [sha], job=job)
        key = cas_key(sha)
        uri = self.store.uri_for(key)
        existing = self.store.stat(uri)
        if existing is None or existing.size != size:
            # A same-key object of a different size is corruption: overwrite it (repair).
            self.store.upload_file(key, path, if_absent=existing is None)
        produced_by = f"{workflow}/{job}" if workflow and job else workflow
        provenance: dict[str, Any] = {"created_at": _iso(_now())}
        if produced_by:
            provenance["produced_by"] = produced_by
        if inputs:
            provenance["inputs"] = sorted(set(inputs))
        self.update_index(
            sha, size=size, add=[location("cas", uri)], provenance=provenance, touch=True
        )
        return Artifact(
            uri=uri, sha256=sha, size=size, name=name or path.name, store=self.spec,
            produced_by=produced_by,
        )

    # --- workflow reference sets (the GC mark roots) ---------------------------------
    def record_refs(self, workflow: str, hashes: Iterable[str], *, job: str | None = None) -> None:
        """Add hashes to the workflow's (or one job's) reference set. Idempotent; refreshes the
        index entry's ``last_referenced`` so a stale-but-reused object is not swept from under
        a workflow that is just now adopting it."""
        wanted = sorted({check_hash(h) for h in hashes})
        key = manifest_key(workflow, job)
        with self._lock:
            fresh = [h for h in wanted if (key, h) not in self._recorded]
        if not fresh:
            return
        for h in fresh:
            if self.store.stat(self.store.uri_for(index_key(h))) is not None:
                self.update_index(h, touch=True)

        def mutate(current: bytes | None) -> bytes | None:
            doc: dict[str, Any] = json.loads(current) if current else {
                "schema": MANIFEST_SCHEMA, "workflow": workflow, "refs": [],
            }
            have = set(doc["refs"])
            if have.issuperset(fresh):
                return None
            doc["refs"] = sorted(have | set(fresh))
            return _dump(doc)

        update_blob(self.store, key, mutate)
        with self._lock:
            self._recorded.update((key, h) for h in fresh)

    # --- reading ---------------------------------------------------------------------
    def candidate_locations(self, artifact: Artifact) -> list[tuple[str, str, str | None]]:
        """(kind, uri, version) to try, in order: the artifact's own uri first, then the
        index's other known locations (a published move retires the CAS copy)."""
        out: list[tuple[str, str, str | None]] = [("own", artifact.uri, artifact.version)]
        if artifact.sha256:
            idx = self.read_index(artifact.sha256)
            for loc in (idx or {}).get("locations", []):
                if loc.get("uri") != artifact.uri:
                    out.append((str(loc.get("kind")), str(loc["uri"]), loc.get("version")))
        return out

    def resolve(self, artifact: Artifact, problems: list[str] | None = None) -> Resolved | None:
        """The first location that still exists with the right size and pinned version, or None.
        ``problems`` (if given) collects one line per location that was tried and rejected."""
        notes = problems if problems is not None else []
        for kind, uri, version in self.candidate_locations(artifact):
            try:
                reader, _ = store_for_uri(uri, self.store)
                st = reader.stat(uri)
            except StoreError as exc:
                notes.append(f"{uri}: {exc}")
                continue
            if st is None:
                notes.append(f"{uri}: not found")
                continue
            if artifact.size is not None and st.size != artifact.size:
                notes.append(f"{uri}: size {st.size} != expected {artifact.size}")
                continue
            if version is not None and st.version != version:
                notes.append(f"{uri}: changed (version {st.version}, pinned {version})")
                continue
            return Resolved(uri=uri, kind=kind, version=st.version, stat=st)
        return None
