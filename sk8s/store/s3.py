from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from sk8s.errors import BlobNotFound, Sk8sError, StoreError, VersionChanged
from sk8s.store.base import CHUNK, BlobInfo, BlobStat, BlobStore, Hasher
from sk8s.store.retry import retry_conflicts

ENDPOINT_ENV = "SK8S_S3_ENDPOINT"
_MAX_DELETE_BATCH = 1000  # S3 DeleteObjects limit


def _error(exc: Exception) -> tuple[str, int]:
    """(Error.Code, HTTP status) of a botocore ClientError; ("", 0) for anything else."""
    response = getattr(exc, "response", None) or {}
    return (
        str(response.get("Error", {}).get("Code", "")),
        int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0),
    )


_MISSING = ("NoSuchKey", "404", "NotFound")


def _same_service(a: Any, b: Any) -> bool:
    """True when two boto clients talk to the same endpoint (so CopyObject can span them)."""
    if a is b:
        return True
    ea = getattr(getattr(a, "meta", None), "endpoint_url", None)
    eb = getattr(getattr(b, "meta", None), "endpoint_url", None)
    return ea is not None and ea == eb


class S3Store(BlobStore):
    """S3-compatible object store. URI: s3://<bucket>/<key>.

    Credentials come from the boto3 default chain only. The endpoint is resolved
    as env SK8S_S3_ENDPOINT > the ``endpoint`` argument (Config.store_endpoint) >
    the SDK default."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        endpoint: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3  # lazy: only users of s3:// stores need the extra
            except ImportError as exc:
                raise Sk8sError(
                    'the s3 store needs boto3; install with: pip install "sk8s[s3]"'
                ) from exc
            endpoint_url = os.environ.get(ENDPOINT_ENV) or endpoint
            kwargs: dict[str, Any] = {}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            client = boto3.client("s3", **kwargs)
        self._client = client

    @classmethod
    def from_url(
        cls, url: str, endpoint: str | None = None, *, client: Any | None = None
    ) -> S3Store:
        parts = urlsplit(url)
        if parts.scheme != "s3" or not parts.netloc:
            raise ValueError(f"not an s3://bucket[/prefix/] url: {url!r}")
        return cls(parts.netloc, parts.path.strip("/"), endpoint=endpoint, client=client)

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def uri_for(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._full_key(key)}"

    def _key_of(self, uri: str) -> str:
        parts = urlsplit(uri)
        if parts.scheme != "s3" or parts.netloc != self.bucket:
            raise ValueError(f"{uri!r} is not a blob of bucket {self.bucket!r}")
        return parts.path[1:] if parts.path.startswith("/") else parts.path

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        self._client.put_object(Bucket=self.bucket, Key=self._full_key(key), Body=data)
        return self.uri_for(key)

    # --- conditional writes: PutObject If-None-Match / If-Match (AWS docs; SPIKE-PENDING) -----
    # NOT verified against a live server (SPIKE-CCW E8 never ran MinIO). Expected losers: 412
    # PreconditionFailed; 409 ConditionalRequestConflict means a concurrent in-flight write on
    # the key, so the call is retried (bounded, jittered) until it resolves to a 412 or wins.
    def _conditional_put(self, key: str, data: bytes, **condition: str) -> bool:
        def attempt() -> bool | None:
            try:
                self._client.put_object(
                    Bucket=self.bucket, Key=self._full_key(key), Body=data, **condition
                )
            except Exception as exc:  # noqa: BLE001 — botocore may be absent with an injected client
                code, status = _error(exc)
                if code == "ConditionalRequestConflict" or status == 409:
                    return None  # retry
                if code in ("PreconditionFailed", "NoSuchKey") or status in (404, 412):
                    return False  # exists / changed / gone
                raise
            return True

        return retry_conflicts(attempt, what=f"conditional put of {self.uri_for(key)}")

    def put_if_absent(self, key: str, data: bytes, owner: Any | None = None) -> bool:
        return self._conditional_put(key, data, IfNoneMatch="*")

    def read_with_version(self, key: str) -> tuple[bytes, str]:
        uri = self.uri_for(key)
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._full_key(key))
        except Exception as exc:  # noqa: BLE001
            if _error(exc)[0] in ("NoSuchKey", "404", "NotFound"):
                raise BlobNotFound(uri) from exc
            raise
        body: bytes = resp["Body"].read()
        return body, str(resp["ETag"])

    def put_if_version(self, key: str, data: bytes, version: str) -> bool:
        return self._conditional_put(key, data, IfMatch=version)

    def get(self, uri: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._key_of(uri))
        except Exception as exc:  # noqa: BLE001 — botocore may be absent with an injected client
            error = getattr(exc, "response", {}).get("Error", {})
            if error.get("Code") in ("NoSuchKey", "404", "NotFound"):
                raise BlobNotFound(uri) from exc
            raise
        body: bytes = resp["Body"].read()
        return body

    def delete(self, uri: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key_of(uri))

    def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        target = self._full_key(prefix)
        under = target + "/"
        # An object stored exactly at `target` counts as the prefix itself.
        self._client.delete_object(Bucket=self.bucket, Key=target)
        batch: list[dict[str, str]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": under}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                if obj["Key"].startswith(under):  # whole-segment match only
                    batch.append({"Key": obj["Key"]})
                    if len(batch) >= _MAX_DELETE_BATCH:
                        self._flush(batch)
                        batch = []
            if not resp.get("IsTruncated"):
                break
            token = resp["NextContinuationToken"]
        if batch:
            self._flush(batch)

    def _flush(self, batch: list[dict[str, str]]) -> None:
        resp = self._client.delete_objects(
            Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
        )
        errors = resp.get("Errors")
        if errors:
            raise StoreError(f"failed to delete {len(errors)} object(s): {errors[0]!r}")

    def count_prefix(self, prefix: str) -> int:
        prefix = prefix.strip("/")
        if not prefix:
            raise ValueError("count_prefix requires a non-empty prefix")
        target = self._full_key(prefix)
        under = target + "/"
        count = 0
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": target}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                if obj["Key"] == target or obj["Key"].startswith(under):  # whole segments only
                    count += 1
            if not resp.get("IsTruncated"):
                return count
            token = resp["NextContinuationToken"]

    # --- object operations (SPIKE-PENDING: modelled on AWS docs, not a live server) ----------
    def _stat_from(self, uri: str, resp: dict[str, Any]) -> BlobStat:
        return BlobStat(
            uri=uri, size=int(resp.get("ContentLength", 0)), version=str(resp["ETag"]),
            updated=resp.get("LastModified"), metadata=dict(resp.get("Metadata") or {}),
        )

    def stat(self, uri: str) -> BlobStat | None:
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=self._key_of(uri))
        except Exception as exc:  # noqa: BLE001
            code, status = _error(exc)
            if code in _MISSING or status == 404:
                return None
            raise
        return self._stat_from(uri, resp)

    def list_blobs(self, prefix: str) -> Iterator[BlobInfo]:
        full = self._full_key(prefix) if prefix else self.prefix
        strip = len(self.prefix) + 1 if self.prefix else 0
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": full}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                yield BlobInfo(
                    key=obj["Key"][strip:], size=int(obj.get("Size", 0)),
                    updated=obj.get("LastModified"),
                )
            if not resp.get("IsTruncated"):
                return
            token = resp["NextContinuationToken"]

    def upload_file(
        self, key: str, path: Path, *, metadata: dict[str, str] | None = None,
        if_absent: bool = False,
    ) -> bool:
        # upload_file's ExtraArgs cannot carry If-None-Match, so if_absent is a HEAD check
        # followed by the upload: NOT atomic. For content-addressed keys that is harmless (two
        # racers write identical bytes); callers needing exclusion use put_if_absent.
        full = self._full_key(key)
        if if_absent and self.stat(self.uri_for(key)) is not None:
            return False
        extra = {"Metadata": dict(metadata)} if metadata else None
        self._client.upload_file(str(path), self.bucket, full, ExtraArgs=extra)
        return True

    def download_file(
        self, uri: str, dest: Path, *, version: str | None = None, hasher: Hasher | None = None,
    ) -> BlobStat:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self._key_of(uri)}
        if version is not None:
            kwargs["IfMatch"] = version
        try:
            resp = self._client.get_object(**kwargs)
        except Exception as exc:  # noqa: BLE001
            code, status = _error(exc)
            if code == "PreconditionFailed" or status == 412:
                raise VersionChanged(f"{uri} changed since it was pinned") from exc
            if code in _MISSING or status == 404:
                raise (VersionChanged(uri) if version is not None else BlobNotFound(uri)) from exc
            raise
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            for chunk in resp["Body"].iter_chunks(CHUNK):
                out.write(chunk)
                if hasher is not None:
                    hasher.update(chunk)
        return self._stat_from(uri, resp)

    def server_copy(
        self, src_uri: str, dst: BlobStore, dst_key: str, *, metadata: dict[str, str] | None,
        if_absent: bool,
    ) -> bool | None:
        if not isinstance(dst, S3Store) or not _same_service(self._client, dst._client):
            return None
        if if_absent and dst.stat(dst.uri_for(dst_key)) is not None:  # HEAD-then-copy: not atomic
            return False
        extra: dict[str, Any] = {}
        if metadata is not None:
            extra = {"Metadata": dict(metadata), "MetadataDirective": "REPLACE"}
        try:
            self._client.copy(
                CopySource={"Bucket": self.bucket, "Key": self._key_of(src_uri)},
                Bucket=dst.bucket, Key=dst._full_key(dst_key), ExtraArgs=extra or None,
            )
        except Exception as exc:  # noqa: BLE001
            code, status = _error(exc)
            if code in _MISSING or status == 404:
                raise BlobNotFound(src_uri) from exc
            raise
        return True
