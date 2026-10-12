from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

from sk8s.errors import BlobNotFound, VersionChanged
from sk8s.store.base import CHUNK, BlobInfo, BlobStat, BlobStore, Hasher


TMP_PREFIX = ".sk8s-tmp-"
LOCK_DIR = ".sk8s-locks"
META_SUFFIX = ".sk8s-meta.json"  # sidecar holding an object's custom metadata (no portable xattrs)


def _utc(ts: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)


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
        path = self._path(uri)
        path.unlink(missing_ok=True)
        Path(str(path) + META_SUFFIX).unlink(missing_ok=True)

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

    # --- object operations --------------------------------------------------------------
    @staticmethod
    def _version_of_stat(st: os.stat_result) -> str:
        return f"{st.st_ino}-{st.st_size}-{st.st_mtime_ns}"  # changes on any overwrite or replace

    def _read_meta(self, path: Path) -> dict[str, str]:
        try:
            raw = json.loads(Path(str(path) + META_SUFFIX).read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def stat(self, uri: str) -> BlobStat | None:
        path = self._path(uri)
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if not path.is_file():
            return None
        return BlobStat(
            uri=uri, size=st.st_size, version=self._version_of_stat(st),
            updated=_utc(st.st_mtime), metadata=self._read_meta(path),
        )

    def list_blobs(self, prefix: str) -> Iterator[BlobInfo]:
        base = self.root
        # A string prefix: walk the deepest existing directory it names, then filter.
        start = base / prefix.rsplit("/", 1)[0] if "/" in prefix else base
        if not start.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d != LOCK_DIR]
            for name in filenames:
                if name.startswith(TMP_PREFIX) or name.endswith(META_SUFFIX):
                    continue
                full = Path(dirpath) / name
                key = str(full.relative_to(base))
                if not key.startswith(prefix):
                    continue
                try:
                    st = full.stat()
                except FileNotFoundError:
                    continue  # deleted while listing
                yield BlobInfo(key=key, size=st.st_size, updated=_utc(st.st_mtime))

    def _install(self, tmp: str, path: Path, *, if_absent: bool) -> bool:
        """Publish a fully written temp file at path atomically; False if if_absent and it exists."""
        try:
            if if_absent:
                os.link(tmp, path)
            else:
                os.replace(tmp, path)
                return True
        except FileExistsError:
            return False
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True

    def _write_meta(self, path: Path, metadata: dict[str, str] | None) -> None:
        meta_path = Path(str(path) + META_SUFFIX)
        if metadata:
            meta_path.write_text(json.dumps(metadata, sort_keys=True))
        else:
            meta_path.unlink(missing_ok=True)

    def upload_file(
        self, key: str, path: Path, *, metadata: dict[str, str] | None = None,
        if_absent: bool = False,
    ) -> bool:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=TMP_PREFIX)
        os.close(fd)
        shutil.copyfile(path, tmp)
        shutil.copymode(path, tmp)  # mkstemp's 0600 would make published files owner-only
        if not self._install(tmp, dest, if_absent=if_absent):
            return False
        self._write_meta(dest, metadata)
        return True

    def download_file(
        self, uri: str, dest: Path, *, version: str | None = None, hasher: Hasher | None = None,
    ) -> BlobStat:
        src = self._path(uri)
        try:
            with open(src, "rb") as f:
                st = os.fstat(f.fileno())
                if version is not None and self._version_of_stat(st) != version:
                    raise VersionChanged(f"{uri} changed since it was pinned")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as out:
                    while chunk := f.read(CHUNK):
                        out.write(chunk)
                        if hasher is not None:
                            hasher.update(chunk)
        except FileNotFoundError:
            raise (VersionChanged(uri) if version is not None else BlobNotFound(uri)) from None
        return BlobStat(
            uri=uri, size=st.st_size, version=self._version_of_stat(st),
            updated=_utc(st.st_mtime), metadata=self._read_meta(src),
        )

    def server_copy(
        self, src_uri: str, dst: BlobStore, dst_key: str, *, metadata: dict[str, str] | None,
        if_absent: bool,
    ) -> bool | None:
        if not isinstance(dst, LocalStore):
            return None
        src = self._path(src_uri)
        if not src.is_file():
            raise BlobNotFound(src_uri)
        carried = metadata if metadata is not None else self._read_meta(src)
        return dst.upload_file(dst_key, src, metadata=carried, if_absent=if_absent)
