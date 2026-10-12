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
import posixpath
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from sk8s.errors import (
    ArtifactChanged, ArtifactError, ArtifactNotFound, BlobNotFound, PublishConflict, StoreError,
    VersionChanged,
)
from sk8s.store import get_store
from sk8s.store.base import CHUNK, BlobStat, BlobStore
from sk8s.store.retry import update_blob

if TYPE_CHECKING:
    from sk8s.artifacts import Artifact

log = logging.getLogger("sk8s.cas")

# Custom object metadata stamped on every published object (SPEC 3.1): points back to the hash
# and to the producing job.
META_SHA = "sk8s-artifact-sha256"
META_PRODUCED_BY = "sk8s-produced-by"

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
                notes.append(f"{uri}: changed (size {st.size}, expected {artifact.size})")
                continue
            if version is not None and st.version != version:
                notes.append(f"{uri}: changed (version {st.version}, pinned {version})")
                continue
            return Resolved(uri=uri, kind=kind, version=st.version, stat=st)
        return None


# --- fetching, publishing, downloading -----------------------------------------------------


def fetch(
    reader: BlobStore, uri: str, dest: Path, *, version: str | None, expected_sha: str | None
) -> tuple[str, int]:
    """Stream ``uri`` to ``dest`` hashing as it goes; verify against ``expected_sha`` and pin to
    ``version``. Returns (sha256, size). On any failure nothing is left at ``dest``."""
    h = hashlib.sha256()
    try:
        reader.download_file(uri, dest, version=version, hasher=h)
    except VersionChanged as exc:
        dest.unlink(missing_ok=True)
        raise ArtifactChanged(f"artifact {uri} changed while it was being read") from exc
    except BlobNotFound as exc:
        raise ArtifactNotFound(f"artifact {uri} vanished before it was read") from exc
    sha = h.hexdigest()
    if expected_sha and sha != expected_sha:
        dest.unlink(missing_ok=True)
        raise ArtifactChanged(
            f"artifact {uri} hashes to sha256:{sha[:16]}..., expected sha256:{expected_sha[:16]}...: "
            "its bytes are not what was pinned"
        )
    return sha, dest.stat().st_size


def not_available(art: Artifact, problems: list[str]) -> ArtifactError:
    detail = "; ".join(problems) or "no known location"
    if any("changed" in p for p in problems):
        return ArtifactChanged(
            f"artifact {art.uri} was overwritten after it was pinned ({detail}); "
            "a changed input is a different artifact, so it was refused"
        )
    return ArtifactNotFound(f"artifact {art.uri} is not available anywhere: {detail}")


def _local_scratch_copy(reader: BlobStore, uri: str, version: str | None) -> tuple[Path, str]:
    """Stream an object to a temp file (for stores with no server-side path to the destination)."""
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="sk8s-publish-")
    os.close(fd)
    path = Path(tmp)
    try:
        sha, _ = fetch(reader, uri, path, version=version, expected_sha=None)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, sha


def publish(
    astore: ArtifactStore | None, art: Artifact, dest_uri: str, *, copy: bool = False
) -> Artifact:
    """Make an artifact available at a human-legible location (SPEC 3.2, decision A3).

    Move by default: the destination becomes the hash's recorded location and the CAS copy is
    retired; ``copy=True`` keeps both. An external reference (not CAS-resident) is always copied:
    sk8s never deletes bytes it does not own. The destination is created if absent and never
    overwritten with different content; re-publishing the same content there is a no-op.

    Provenance metadata (``sk8s-artifact-sha256``, ``sk8s-produced-by``) is sent WITH the
    content (server-side copy or upload), so there is no window where the object exists without
    it, and no later content write that could wipe it (the GCS overwrite trap). It is read back
    and verified afterwards."""
    dest_uri = normalize_uri(dest_uri)
    scheme, _, _ = split_uri(dest_uri)
    if scheme == "k8s":
        raise ValueError("cannot publish into the ConfigMap store; use gs://, s3:// or a path")
    problems: list[str] = []
    res = astore.resolve(art, problems) if astore is not None else _resolve_bare(art, problems)
    if res is None:
        raise not_available(art, problems)
    reader, _ = store_for_uri(res.uri, astore.store if astore else None)
    sha = art.sha256
    if sha is None:  # an unhashed external reference: hash it now (streamed, nothing kept)
        sha, _ = fetch(reader, res.uri, Path(os.devnull), version=res.version, expected_sha=None)
    size = res.stat.size
    dst_store, dst_key = store_for_uri(dest_uri)
    metadata = {META_SHA: sha, META_PRODUCED_BY: art.produced_by or "unknown"}

    existing = dst_store.stat(dest_uri)
    if existing is None:
        done = reader.server_copy(res.uri, dst_store, dst_key, metadata=metadata, if_absent=True)
        if done is None:  # no server-side path between these stores: stream through a temp file
            tmp, got = _local_scratch_copy(reader, res.uri, res.version)
            try:
                if got != sha:
                    raise ArtifactChanged(
                        f"artifact {res.uri} hashes to sha256:{got[:16]}..., expected sha256:{sha[:16]}..."
                    )
                done = dst_store.upload_file(dst_key, tmp, metadata=metadata, if_absent=True)
            finally:
                tmp.unlink(missing_ok=True)
        landed = dst_store.stat(dest_uri)  # also covers losing the create race to a same-content publish
    else:
        landed = existing
    if landed is None:
        raise ArtifactError(f"publish to {dest_uri} reported success but the object is not there")
    if landed.metadata.get(META_SHA) != sha:
        if existing is not None or landed.size != size or META_SHA in landed.metadata:
            raise PublishConflict(
                f"{dest_uri} already holds different content "
                f"(sk8s-artifact-sha256={landed.metadata.get(META_SHA)!r}); publish never overwrites"
            )
        raise ArtifactError(
            f"published {dest_uri} but its {META_SHA} metadata is missing - the destination store "
            "dropped custom metadata, so provenance would be lost; refusing to retire the source"
        )
    if landed.size != size:
        raise ArtifactError(f"published {dest_uri} is {landed.size} bytes, expected {size}")

    out = _moved(art, dest_uri, sha, size, landed.version)
    if astore is not None and art.store is not None:
        cas_uri = astore.store.uri_for(cas_key(sha))
        retire = not copy and res.uri == cas_uri
        astore.update_index(
            sha, size=size, touch=True,
            add=[location("published", dest_uri, landed.version, copy=copy or not retire)],
        )
        if retire:
            astore.update_index(sha, remove=[("cas", cas_uri)])
            astore.store.delete(cas_uri)
    return out


def _moved(art: Artifact, uri: str, sha: str, size: int, version: str | None) -> Artifact:
    import dataclasses

    return dataclasses.replace(
        art, uri=uri, sha256=sha, size=size, version=version,
        name=posixpath.basename(uri) or art.name,
    )


def _resolve_bare(art: Artifact, problems: list[str]) -> Resolved | None:
    """resolve() for an artifact with no artifact store: only its own uri is known."""
    try:
        reader, _ = store_for_uri(art.uri)
        st = reader.stat(art.uri)
    except StoreError as exc:
        problems.append(f"{art.uri}: {exc}")
        return None
    if st is None:
        problems.append(f"{art.uri}: not found")
        return None
    if (art.size is not None and st.size != art.size) or (
        art.version is not None and st.version != art.version
    ):
        problems.append(f"{art.uri}: changed (version {st.version}, pinned {art.version})")
        return None
    return Resolved(uri=art.uri, kind="own", version=st.version, stat=st)


def download(astore: ArtifactStore | None, art: Artifact, path: str | os.PathLike[str]) -> Path:
    """Copy an artifact's bytes to a local ``path`` (a directory gets ``art.filename`` inside),
    verifying the hash as they stream. Written to a temp name and renamed, so a failed download
    leaves nothing at ``path``."""
    target = Path(path)
    if target.is_dir():
        target = target / posixpath.basename(art.filename)
    problems: list[str] = []
    res = astore.resolve(art, problems) if astore is not None else _resolve_bare(art, problems)
    if res is None:
        raise not_available(art, problems)
    reader, _ = store_for_uri(res.uri, astore.store if astore else None)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".sk8s-part")
    try:
        fetch(reader, res.uri, part, version=res.version, expected_sha=art.sha256)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target


# --- opening the artifact store an Artifact belongs to -----------------------------------------

_artifact_stores: dict[tuple[str, str], ArtifactStore] = {}


def open_artifact_store(spec: str | None, uri: str = "") -> ArtifactStore | None:
    """The ArtifactStore named by an Artifact's ``store`` spec (None for a bare reference).
    Shared per process so its record cache and clients are reused."""
    if spec is None:
        return None
    namespace = None
    if spec == "k8s":
        namespace = uri[len("k8s://"):].split("/", 1)[0] if uri.startswith("k8s://") else (
            os.environ.get("SK8S_NAMESPACE") or None
        )
    cache_key = (spec, namespace or "")
    with _bucket_lock:
        found = _artifact_stores.get(cache_key)
        if found is None:
            found = ArtifactStore.from_spec(spec, namespace=namespace)
            _artifact_stores[cache_key] = found
    return found
