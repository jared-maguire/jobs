"""Dict-backed fake of the google-cloud-storage client surface that GCSStore uses.

Models the semantics verified live in SPIKE-CCW.md: generations, ``if_generation_match``
(0 = create-if-absent; a missing object never matches N > 0), a plain content overwrite
WIPES custom metadata, a read pinned to an overwritten generation is a 404, ``rewrite``
preserves source metadata unless the destination blob carries its own.
"""
from __future__ import annotations

import datetime
import io
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.api_core.exceptions import NotFound, PreconditionFailed


@dataclass
class _Meta:
    generation: int
    metageneration: int = 1
    metadata: dict[str, str] | None = None
    updated: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class FakeBlob:
    def __init__(self, client: FakeGCSClient, bucket: str, name: str) -> None:
        self._client = client
        self._bucket = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None
        self.generation: int | None = None
        self.metageneration: int | None = None
        self.size: int | None = None
        self.updated: datetime.datetime | None = None
        self.content_type: str | None = None

    # --- internals -------------------------------------------------------------
    @property
    def _key(self) -> tuple[str, str]:
        return (self._bucket, self.name)

    def _populate(self) -> None:
        meta = self._client.meta_of(self._key)
        self.generation = meta.generation
        self.metageneration = meta.metageneration
        self.metadata = dict(meta.metadata) if meta.metadata else None
        self.updated = meta.updated
        self.size = len(self._client.objects[self._key])

    def _check(self, if_generation_match: int | None) -> None:
        """Caller holds the client lock."""
        if if_generation_match is None:
            return
        exists = self._key in self._client.objects
        current = self._client.meta_of(self._key).generation if exists else None
        ok = (current is None) if if_generation_match == 0 else (current == if_generation_match)
        if not ok:
            raise PreconditionFailed("At least one of the pre-conditions you specified did not hold.")

    def _store(self, raw: bytes, metadata: dict[str, str] | None) -> None:
        """Caller holds the lock. A content write replaces metadata wholesale (the wipe trap)."""
        self._client.objects[self._key] = raw
        self._client.meta[self._key] = _Meta(
            generation=self._client.next_generation(),
            metadata=dict(metadata) if metadata else None,
        )
        self._populate()

    # --- the client surface ------------------------------------------------------
    def upload_from_string(
        self, data: bytes | str, content_type: str | None = None,
        if_generation_match: int | None = None, **_: Any,
    ) -> None:
        raw = data.encode() if isinstance(data, str) else bytes(data)
        with self._client.lock:
            self._check(if_generation_match)
            self._client.upload_calls += 1
            self._store(raw, self.metadata)

    def upload_from_filename(
        self, filename: str | Path, if_generation_match: int | None = None, **_: Any
    ) -> None:
        self.upload_from_string(Path(filename).read_bytes(), if_generation_match=if_generation_match)

    def download_as_bytes(
        self, if_generation_match: int | None = None, **_: Any
    ) -> bytes:
        with self._client.lock:
            if self._key not in self._client.objects:
                raise NotFound(f"No such object: {self._bucket}/{self.name}")
            current = self._client.meta_of(self._key).generation
            if self.generation is not None and self.generation != current:
                # Versioning is off: a read pinned to an overwritten generation is a 404.
                raise NotFound(f"No such object: {self._bucket}/{self.name} (generation)")
            self._check(if_generation_match)
            raw = self._client.objects[self._key]
            self._populate()
            return raw

    def open(self, mode: str = "rb", **kw: Any) -> io.BytesIO:
        assert mode == "rb", mode
        return io.BytesIO(self.download_as_bytes(**{
            k: v for k, v in kw.items() if k == "if_generation_match"
        }))

    def reload(self, **_: Any) -> None:
        with self._client.lock:
            if self._key not in self._client.objects:
                raise NotFound(f"No such object: {self._bucket}/{self.name}")
            pinned = self.generation
            if pinned is not None and pinned != self._client.meta_of(self._key).generation:
                raise NotFound(f"No such object: {self._bucket}/{self.name} (generation {pinned})")
            self._populate()

    def rewrite(
        self, source: FakeBlob, token: str | None = None,
        if_generation_match: int | None = None, **_: Any,
    ) -> tuple[str | None, int, int]:
        with self._client.lock:
            if source._key not in self._client.objects:
                raise NotFound(f"No such object: {source._bucket}/{source.name}")
            self._check(if_generation_match)
            raw = self._client.objects[source._key]
            src_meta = self._client.meta_of(source._key).metadata
            # dst.metadata, when set, replaces the source's wholesale; otherwise it is preserved.
            self._store(raw, self.metadata if self.metadata is not None else src_meta)
            self._client.rewrite_calls += 1
            return (None, len(raw), len(raw))

    def patch(self, if_metageneration_match: int | None = None, **_: Any) -> None:
        with self._client.lock:
            if self._key not in self._client.objects:
                raise NotFound(f"No such object: {self._bucket}/{self.name}")
            meta = self._client.meta_of(self._key)
            if if_metageneration_match is not None and meta.metageneration != if_metageneration_match:
                raise PreconditionFailed("At least one of the pre-conditions you specified did not hold.")
            meta.metadata = dict(self.metadata) if self.metadata else None
            meta.metageneration += 1
            self._populate()

    def delete(self, if_generation_match: int | None = None, **_: Any) -> None:
        # Like the real API, deleting a missing object is a 404.
        with self._client.lock:
            if self._key not in self._client.objects:
                raise NotFound(f"No such object: {self._bucket}/{self.name}")
            self._check(if_generation_match)
            del self._client.objects[self._key]
            self._client.meta.pop(self._key, None)
            self._client.delete_calls += 1


class FakeBucket:
    def __init__(self, client: FakeGCSClient, name: str) -> None:
        self._client = client
        self.name = name

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self._client, self.name, name)

    def get_blob(self, name: str, **_: Any) -> FakeBlob | None:
        blob = FakeBlob(self._client, self.name, name)
        with self._client.lock:
            if (self.name, name) not in self._client.objects:
                return None
            blob._populate()
        return blob


class FakeGCSClient:
    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.meta: dict[tuple[str, str], _Meta] = {}
        self.page_size = page_size
        self.list_calls = 0  # server pages fetched
        self.delete_calls = 0  # successful deletes
        self.upload_calls = 0  # successful content uploads
        self.rewrite_calls = 0
        self.lock = threading.RLock()
        self._generation = 1_000_000

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def meta_of(self, key: tuple[str, str]) -> _Meta:
        """Metadata for an object; objects planted directly in ``objects`` get a fresh record."""
        if key not in self.meta:
            self.meta[key] = _Meta(generation=self.next_generation())
        return self.meta[key]

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self, name)

    def list_blobs(
        self, bucket_or_name: str | FakeBucket, *, prefix: str | None = None, **_: Any
    ) -> Iterator[FakeBlob]:
        bucket = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        return self._pages(bucket, prefix or "")

    def _pages(self, bucket: str, prefix: str) -> Iterator[FakeBlob]:
        # Like GCS, the page token is key-based, so it stays valid while keys are deleted.
        token = ""
        while True:
            self.list_calls += 1
            keys = sorted(k for (b, k) in self.objects if b == bucket and k.startswith(prefix))
            keys = [k for k in keys if k > token]
            page = keys[: self.page_size]
            for k in page:
                blob = FakeBlob(self, bucket, k)
                with self.lock:
                    if (bucket, k) in self.objects:
                        blob._populate()
                yield blob
            if len(keys) <= self.page_size:
                return
            token = page[-1]
