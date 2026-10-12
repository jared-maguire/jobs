from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from sk8s.errors import BlobNotFound, Sk8sError, VersionChanged
from sk8s.store.base import CHUNK, BlobInfo, BlobStat, BlobStore, Hasher


def _is_not_found(exc: Exception) -> bool:
    # google.api_core.exceptions.NotFound carries code == 404. Checked by attribute so
    # this module never imports google.api_core (it may be absent with an injected client).
    return getattr(exc, "code", None) == 404


def _is_precondition_failed(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 412  # PreconditionFailed (conditionNotMet)


class GCSStore(BlobStore):
    """Google Cloud Storage object store. URI: gs://<bucket>/<key>.

    Credentials come from the google-cloud-storage default chain only (ADC)."""

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            try:
                from google.cloud import storage  # lazy: only users of gs:// stores need the extra
            except ImportError as exc:
                raise Sk8sError(
                    'the gcs store needs google-cloud-storage; install with: pip install "sk8s[gcs]"'
                ) from exc
            client = storage.Client()
        self._client = client
        self._bucket = client.bucket(bucket)

    @classmethod
    def from_url(cls, url: str, *, client: Any | None = None) -> GCSStore:
        parts = urlsplit(url)
        if parts.scheme != "gs" or not parts.netloc:
            raise ValueError(f"not a gs://bucket[/prefix/] url: {url!r}")
        return cls(parts.netloc, parts.path.strip("/"), client=client)

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def uri_for(self, key: str) -> str:
        return f"gs://{self.bucket}/{self._full_key(key)}"

    def _key_of(self, uri: str) -> str:
        parts = urlsplit(uri)
        if parts.scheme != "gs" or parts.netloc != self.bucket:
            raise ValueError(f"{uri!r} is not a blob of bucket {self.bucket!r}")
        return parts.path[1:] if parts.path.startswith("/") else parts.path

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        self._bucket.blob(self._full_key(key)).upload_from_string(data)
        return self.uri_for(key)

    # --- conditional writes (spec Appendix B.1: generation preconditions; verified live) ----------
    # ifGenerationMatch=0 is create-if-absent; =N is compare-and-swap on the generation. A
    # missing object never satisfies N > 0 (also a 412), so "changed" and "gone" read alike.
    def put_if_absent(self, key: str, data: bytes, owner: Any | None = None) -> bool:
        try:
            self._bucket.blob(self._full_key(key)).upload_from_string(data, if_generation_match=0)
        except Exception as exc:  # noqa: BLE001 — google.api_core may be absent with an injected client
            if _is_precondition_failed(exc):
                return False
            raise
        return True

    def read_with_version(self, key: str) -> tuple[bytes, str]:
        blob = self._bucket.blob(self._full_key(key))  # a fresh handle: its generation comes from the GET
        try:
            data: bytes = blob.download_as_bytes()
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise BlobNotFound(self.uri_for(key)) from exc
            raise
        return data, str(blob.generation)  # same response as the bytes (spike E6/X4)

    def put_if_version(self, key: str, data: bytes, version: str) -> bool:
        if not version.isdigit():
            return False  # not a token this store issued: it cannot match anything
        try:
            self._bucket.blob(self._full_key(key)).upload_from_string(
                data, if_generation_match=int(version)
            )
        except Exception as exc:  # noqa: BLE001
            if _is_precondition_failed(exc) or _is_not_found(exc):
                return False
            raise
        return True

    def get(self, uri: str) -> bytes:
        try:
            data: bytes = self._bucket.blob(self._key_of(uri)).download_as_bytes()
        except Exception as exc:  # noqa: BLE001 — google.api_core may be absent with an injected client
            if _is_not_found(exc):
                raise BlobNotFound(uri) from exc
            raise
        return data

    def _delete_key(self, key: str) -> None:
        try:
            self._bucket.blob(key).delete()
        except Exception as exc:  # noqa: BLE001 — see get()
            if not _is_not_found(exc):
                raise

    def delete(self, uri: str) -> None:
        self._delete_key(self._key_of(uri))

    def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        target = self._full_key(prefix)
        under = target + "/"
        # An object stored exactly at `target` counts as the prefix itself.
        self._delete_key(target)
        for blob in self._client.list_blobs(self.bucket, prefix=under):  # pages transparently
            if blob.name.startswith(under):  # whole-segment match only
                self._delete_key(blob.name)

    def count_prefix(self, prefix: str) -> int:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("count_prefix requires a non-empty prefix")
        target = self._full_key(prefix)
        under = target + "/"
        # An object stored exactly at `target` counts as the prefix itself; anything else must
        # lie under "target/" (whole-segment match only).
        return sum(
            1
            for blob in self._client.list_blobs(self.bucket, prefix=target)  # pages transparently
            if blob.name == target or blob.name.startswith(under)
        )

    # --- object operations ---------------------------------------------------------------
    @staticmethod
    def _stat_of(uri: str, blob: Any) -> BlobStat:
        return BlobStat(
            uri=uri, size=int(blob.size or 0), version=str(blob.generation),
            updated=blob.updated, metadata=dict(blob.metadata or {}),
        )

    def stat(self, uri: str) -> BlobStat | None:
        blob = self._bucket.get_blob(self._key_of(uri))  # None when absent
        return None if blob is None else self._stat_of(uri, blob)

    def list_blobs(self, prefix: str) -> Iterator[BlobInfo]:
        full = self._full_key(prefix) if prefix else self.prefix
        strip = len(self.prefix) + 1 if self.prefix else 0
        for blob in self._client.list_blobs(self.bucket, prefix=full):  # pages transparently
            yield BlobInfo(key=blob.name[strip:], size=int(blob.size or 0), updated=blob.updated)

    def upload_file(
        self, key: str, path: Path, *, metadata: dict[str, str] | None = None,
        if_absent: bool = False,
    ) -> bool:
        # Metadata is attached to the upload itself so it lands atomically with the content: a
        # content write WITHOUT metadata would wipe it (spec Appendix B.1: the overwrite-wipes-metadata trap), so there is no window in
        # which the object exists unlabelled.
        blob = self._bucket.blob(self._full_key(key))
        if metadata:
            blob.metadata = dict(metadata)
        try:
            blob.upload_from_filename(str(path), if_generation_match=0 if if_absent else None)
        except Exception as exc:  # noqa: BLE001
            if _is_precondition_failed(exc):
                return False
            raise
        return True

    def download_file(
        self, uri: str, dest: Path, *, version: str | None = None, hasher: Hasher | None = None,
    ) -> BlobStat:
        key = self._key_of(uri)
        kwargs: dict[str, Any] = {}
        if version is not None:
            if not version.isdigit():
                raise VersionChanged(f"{uri}: {version!r} is not a GCS generation")
            # Blob.generation is read-only; the constructor argument pins every request.
            blob = self._bucket.blob(key, generation=int(version))
            kwargs["if_generation_match"] = int(version)
        else:
            blob = self._bucket.blob(key)
        try:
            # reload() fixes blob.generation, so every ranged read below is bound to ONE
            # generation: a concurrent overwrite is a clean error, never a spliced file.
            blob.reload()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with blob.open("rb", **kwargs) as src, open(dest, "wb") as out:
                while chunk := src.read(CHUNK):
                    out.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise (VersionChanged(uri) if version is not None else BlobNotFound(uri)) from exc
            if _is_precondition_failed(exc):
                raise VersionChanged(f"{uri} changed since it was pinned") from exc
            raise
        return self._stat_of(uri, blob)

    def server_copy(
        self, src_uri: str, dst: BlobStore, dst_key: str, *, metadata: dict[str, str] | None,
        if_absent: bool,
    ) -> bool | None:
        if not isinstance(dst, GCSStore):
            return None
        src_blob = self._bucket.blob(self._key_of(src_uri))
        dst_blob = dst._bucket.blob(dst._full_key(dst_key))
        if metadata is not None:
            dst_blob.metadata = dict(metadata)  # replaces the source's wholesale (spec Appendix B.1: the overwrite-wipes-metadata trap)
        token: str | None = None
        try:
            while True:  # rewrite is resumable: large or cross-location copies take several calls
                token, _, _ = dst_blob.rewrite(
                    src_blob, token=token, if_generation_match=0 if if_absent else None
                )
                if token is None:
                    return True
        except Exception as exc:  # noqa: BLE001
            if _is_precondition_failed(exc):
                return False
            if _is_not_found(exc):
                raise BlobNotFound(src_uri) from exc
            raise
