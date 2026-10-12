from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sk8s.errors import BlobNotFound
from sk8s.store.base import BlobStore


class LocalStore(BlobStore):
    """Directory-backed store for LocalCluster and tests. URI: local:<abs path>."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def uri_for(self, key: str) -> str:
        return f"local:{self.root / key}"

    def _path(self, uri: str) -> Path:
        assert uri.startswith("local:"), uri
        return Path(uri[len("local:"):])

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.uri_for(key)

    def get(self, uri: str) -> bytes:
        path = self._path(uri)
        if not path.exists():
            raise BlobNotFound(uri)
        return path.read_bytes()

    def delete(self, uri: str) -> None:
        self._path(uri).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        target = self.root / prefix  # directory tree = whole-segment matching
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    def count_prefix(self, prefix: str) -> int:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("count_prefix requires a non-empty prefix")
        target = self.root / prefix
        if target.is_dir():
            return sum(1 for p in target.rglob("*") if p.is_file())
        return 1 if target.exists() else 0
