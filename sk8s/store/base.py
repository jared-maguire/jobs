from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


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
        """Delete every blob whose key starts with prefix."""

    @abstractmethod
    def uri_for(self, key: str) -> str:
        """The URI put(key, ...) would return. Deterministic; no I/O."""
