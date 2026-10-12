from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from sk8s.errors import BlobNotFound, Sk8sError
from sk8s.store.base import BlobStore


def _is_not_found(exc: Exception) -> bool:
    # google.api_core.exceptions.NotFound carries code == 404. Checked by attribute so
    # this module never imports google.api_core (it may be absent with an injected client).
    return getattr(exc, "code", None) == 404


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
