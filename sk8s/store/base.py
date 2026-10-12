from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sk8s.errors import StoreError


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
