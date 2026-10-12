from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from sk8s.errors import BlobNotFound, Sk8sError, StoreError
from sk8s.store.base import BlobStore
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
