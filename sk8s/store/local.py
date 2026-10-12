from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from sk8s.errors import BlobNotFound
from sk8s.store.base import BlobStore


TMP_PREFIX = ".sk8s-tmp-"
LOCK_DIR = ".sk8s-locks"


class LocalStore(BlobStore):
    """Directory-backed store for LocalCluster and tests. URI: local:<abs path>."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def uri_for(self, key: str) -> str:
        return f"local:{self.root / key}"

    def _path(self, uri: str) -> Path:
        assert uri.startswith("local:"), uri
        return Path(uri[len("local:"):])

    def _write_tmp(self, path: Path, data: bytes) -> str:
        """Fully write data to a sibling temp file; the caller publishes it atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=TMP_PREFIX)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return tmp

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        path = self.root / key
        tmp = self._write_tmp(path, data)
        os.replace(tmp, path)  # atomic: a concurrent reader sees the old or the new content
        return self.uri_for(key)

    # --- conditional writes: hard link = create-if-absent; a flock'd read-compare-replace = CAS
    def put_if_absent(self, key: str, data: bytes, owner: Any | None = None) -> bool:
        path = self.root / key
        tmp = self._write_tmp(path, data)
        try:
            os.link(tmp, path)  # atomic and fails if path exists; readers never see a partial file
        except FileExistsError:
            return False
        finally:
            os.unlink(tmp)
        return True

    @staticmethod
    def _version_of(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def read_with_version(self, key: str) -> tuple[bytes, str]:
        path = self.root / key
        try:
            data = path.read_bytes()  # os.replace is atomic, so this is one consistent version
        except FileNotFoundError:
            raise BlobNotFound(self.uri_for(key)) from None
        return data, self._version_of(data)

    def put_if_version(self, key: str, data: bytes, version: str) -> bool:
        path = self.root / key
        lock_dir = self.root / LOCK_DIR
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / hashlib.sha1(key.encode()).hexdigest()  # noqa: S324 — a name, not security
        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # serialises writers across processes
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                return False
            if self._version_of(current) != version:
                return False
            tmp = self._write_tmp(path, data)
            os.replace(tmp, path)
            return True

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
