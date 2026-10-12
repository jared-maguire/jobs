from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from sk8s.errors import BlobNotFound, StoreError, VersionChanged

CHUNK = 1024 * 1024  # streaming transfers move this many bytes at a time


class Hasher(Protocol):
    """The slice of a hashlib object that streaming transfers feed."""

    def update(self, data: bytes, /) -> Any: ...


@dataclass(frozen=True)
class BlobStat:
    """What a metadata-only lookup tells about one object."""

    uri: str
    size: int
    version: str  # opaque; equality only (GCS generation, S3 ETag, ConfigMap resourceVersion, ...)
    updated: datetime.datetime | None = None  # tz-aware UTC when known
    metadata: dict[str, str] = field(default_factory=dict)  # custom object metadata


@dataclass(frozen=True)
class BlobInfo:
    """One row of list_blobs()."""

    key: str  # relative to the store (its prefix stripped), usable with every key-based method
    size: int
    updated: datetime.datetime | None = None


class BlobStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        """Store data under key; returns the blob's URI. owner (a
        V1OwnerReference) is honored by Kubernetes-backed stores, ignored elsewhere."""

    @abstractmethod
    def get(self, uri: str) -> bytes:
        """Fetch a blob by URI. Raises BlobNotFound."""

    @abstractmethod
    def delete(self, uri: str) -> None:
        """Delete one blob. Idempotent: missing blobs are not an error."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Delete every blob whose key is prefix or lies under it.

        Matching is by whole "/"-separated segments: prefix "wf-a" removes
        "wf-a/..." but never "wf-ab/..." or "wf-a1/...". A trailing or leading
        "/" on prefix is ignored. An empty prefix raises ValueError rather than
        matching everything."""

    @abstractmethod
    def uri_for(self, key: str) -> str:
        """The URI put(key, ...) would return. Deterministic; no I/O."""

    def count_prefix(self, prefix: str) -> int:
        """How many blobs lie at or under ``prefix`` (read-only; same whole-segment matching and
        empty-prefix rule as ``delete_prefix``). Backends that cannot count raise
        ``NotImplementedError``."""
        raise NotImplementedError(f"{type(self).__name__} cannot count blobs under a prefix")

    # --- conditional writes (the artifact layer's concurrency primitives) -------------
    # Verified semantics per backend are in SPIKE-CCW.md. Versions are opaque strings: compare
    # for equality only. None of these fall back to an unconditional write.

    def put_if_absent(self, key: str, data: bytes, owner: Any | None = None) -> bool:
        """Atomically create key with data. True if this call created it; False if the key
        already existed (the existing content is left untouched). Never overwrites."""
        raise StoreError(f"{type(self).__name__} does not support conditional writes")

    def read_with_version(self, key: str) -> tuple[bytes, str]:
        """The blob's bytes and an opaque version token taken from the SAME read (so a
        following put_if_version is conditional on exactly the bytes returned).
        Raises BlobNotFound."""
        raise StoreError(f"{type(self).__name__} does not support conditional writes")

    def put_if_version(self, key: str, data: bytes, version: str) -> bool:
        """Replace key's content with data only if it is still at ``version`` (a token from
        read_with_version). True if written; False if the blob changed or is gone since."""
        raise StoreError(f"{type(self).__name__} does not support conditional writes")

    # --- object operations the artifact layer needs (streaming, listing, metadata) ---------
    # Defaults are built on get/put so any store works for small objects; the object stores
    # and LocalStore override them with streaming implementations.

    def stat(self, uri: str) -> BlobStat | None:
        """Size, version token and custom metadata of one object without reading its bytes;
        None if it does not exist."""
        raise StoreError(f"{type(self).__name__} cannot stat objects")

    def list_blobs(self, prefix: str) -> Iterator[BlobInfo]:
        """Every object whose key starts with ``prefix`` (a plain string prefix: pass a trailing
        "/" for a directory). Order is unspecified. An O(n) operation: sweeps only, never lookups."""
        raise StoreError(f"{type(self).__name__} cannot list objects")

    def upload_file(
        self, key: str, path: Path, *, metadata: dict[str, str] | None = None,
        if_absent: bool = False,
    ) -> bool:
        """Stream a local file into key, with ``metadata`` landing together with the content (a
        content write that omits metadata would WIPE it on GCS, so it is never sent separately).
        With if_absent the key is created only if absent. True if written, False if it existed."""
        if metadata:
            raise StoreError(
                f"{type(self).__name__} cannot carry object metadata; publish to gs://, s3:// "
                "or a local path"
            )
        data = path.read_bytes()
        if if_absent:
            return self.put_if_absent(key, data)
        self.put(key, data)
        return True

    def download_file(
        self, uri: str, dest: Path, *, version: str | None = None, hasher: Hasher | None = None,
    ) -> BlobStat:
        """Stream one object to ``dest``, feeding ``hasher`` the same bytes (hash-while-streaming).
        With ``version`` the read is pinned to that version: VersionChanged if the object was
        overwritten or deleted. BlobNotFound if absent and unpinned."""
        info = self.stat(uri)
        if info is None:
            raise VersionChanged(uri) if version is not None else BlobNotFound(uri)
        if version is not None and info.version != version:
            raise VersionChanged(f"{uri} is now at version {info.version}, expected {version}")
        data = self.get(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if hasher is not None:
            hasher.update(data)
        return info

    def server_copy(
        self, src_uri: str, dst: BlobStore, dst_key: str, *, metadata: dict[str, str] | None,
        if_absent: bool,
    ) -> bool | None:
        """Copy an object to ``dst_key`` of ``dst`` without the bytes passing through this
        process, replacing custom metadata with ``metadata`` (None keeps the source's). True if
        copied, False if if_absent and the destination exists. None means this pair of stores
        has no server-side path: the caller streams instead."""
        return None
