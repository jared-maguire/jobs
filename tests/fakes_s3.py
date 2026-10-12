"""Dict-backed fake of the boto3 S3 client surface that S3Store uses.

Conditional writes (PutObject ``IfNoneMatch="*"`` / ``IfMatch=<etag>``) are modelled on what
tests/integration/test_artifacts_s3.py observed live against MinIO RELEASE.2026-09-22 (the
S3/MinIO column the spike left open; real AWS S3 is NOT verified): 412 ``PreconditionFailed``
for If-None-Match on an existing key and for a stale If-Match; 404 ``NoSuchKey`` for If-Match on
a missing key. 409 ``ConditionalRequestConflict`` (documented by AWS for concurrent in-flight
writes; never observed on MinIO) is injectable via ``inject_conflicts``. Like real S3, a
single-part ETag is the md5 of the content, so rewriting identical bytes keeps the ETag.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import threading
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError


def _client_error(code: str, op: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


class _Body(io.BytesIO):
    """Stand-in for botocore's StreamingBody."""

    def iter_chunks(self, chunk_size: int = 1024 * 1024) -> Any:
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                return
            yield chunk


class FakeS3Client:
    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.meta: dict[tuple[str, str], dict[str, Any]] = {}  # etag, metadata, modified
        self.page_size = page_size
        self.list_calls = 0
        self.delete_batches: list[int] = []
        self.put_calls = 0
        self.copy_calls = 0
        self.lock = threading.RLock()
        self.inject_conflicts = 0  # the next N conditional puts fail with 409

    def _etag(self, raw: bytes) -> str:
        return '"' + hashlib.md5(raw).hexdigest() + '"'  # noqa: S324 — an ETag, not security

    def _record(self, bucket: str, key: str, raw: bytes, metadata: dict[str, str] | None) -> str:
        etag = self._etag(raw)
        self.objects[(bucket, key)] = raw
        self.meta[(bucket, key)] = {
            "etag": etag, "metadata": dict(metadata or {}),
            "modified": datetime.datetime.now(datetime.timezone.utc),
        }
        return etag

    def _meta(self, bucket: str, key: str) -> dict[str, Any]:
        if (bucket, key) not in self.meta:  # planted directly in .objects
            self.meta[(bucket, key)] = {
                "etag": self._etag(self.objects[(bucket, key)]), "metadata": {},
                "modified": datetime.datetime.now(datetime.timezone.utc),
            }
        return self.meta[(bucket, key)]

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes | Any,
        IfNoneMatch: str | None = None, IfMatch: str | None = None,
        Metadata: dict[str, str] | None = None, **_: Any,
    ) -> dict[str, Any]:
        raw = Body if isinstance(Body, (bytes, bytearray)) else Body.read()
        with self.lock:
            if (IfNoneMatch or IfMatch) and self.inject_conflicts > 0:
                self.inject_conflicts -= 1
                raise _client_error("ConditionalRequestConflict", "PutObject", 409)
            exists = (Bucket, Key) in self.objects
            if IfNoneMatch == "*" and exists:
                raise _client_error("PreconditionFailed", "PutObject", 412)
            if IfMatch is not None:
                if not exists:
                    raise _client_error("NoSuchKey", "PutObject", 404)
                if self._meta(Bucket, Key)["etag"] != IfMatch:
                    raise _client_error("PreconditionFailed", "PutObject", 412)
            self.put_calls += 1
            return {"ETag": self._record(Bucket, Key, bytes(raw), Metadata)}

    def get_object(
        self, *, Bucket: str, Key: str, IfMatch: str | None = None, **_: Any
    ) -> dict[str, Any]:
        with self.lock:
            if (Bucket, Key) not in self.objects:
                raise _client_error("NoSuchKey", "GetObject", 404)
            meta = self._meta(Bucket, Key)
            if IfMatch is not None and meta["etag"] != IfMatch:
                raise _client_error("PreconditionFailed", "GetObject", 412)
            raw = self.objects[(Bucket, Key)]
            return {
                "Body": _Body(raw), "ETag": meta["etag"], "ContentLength": len(raw),
                "Metadata": dict(meta["metadata"]), "LastModified": meta["modified"],
            }

    def head_object(self, *, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:
        with self.lock:
            if (Bucket, Key) not in self.objects:
                raise _client_error("404", "HeadObject", 404)
            meta = self._meta(Bucket, Key)
            return {
                "ETag": meta["etag"], "ContentLength": len(self.objects[(Bucket, Key)]),
                "Metadata": dict(meta["metadata"]), "LastModified": meta["modified"],
            }

    def upload_file(
        self, Filename: str, Bucket: str, Key: str, ExtraArgs: dict[str, Any] | None = None, **_: Any
    ) -> None:
        with self.lock:
            self.put_calls += 1
            self._record(Bucket, Key, Path(Filename).read_bytes(), (ExtraArgs or {}).get("Metadata"))

    def copy(
        self, CopySource: dict[str, str], Bucket: str, Key: str,
        ExtraArgs: dict[str, Any] | None = None, **_: Any,
    ) -> None:
        extra = ExtraArgs or {}
        src = (CopySource["Bucket"], CopySource["Key"])
        with self.lock:
            if src not in self.objects:
                raise _client_error("NoSuchKey", "CopyObject", 404)
            metadata = (
                extra.get("Metadata") if extra.get("MetadataDirective") == "REPLACE"
                else self._meta(*src)["metadata"]
            )
            self.copy_calls += 1
            self._record(Bucket, Key, self.objects[src], metadata)

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.objects.pop((Bucket, Key), None)
        self.meta.pop((Bucket, Key), None)
        return {}

    def list_objects_v2(
        self, *, Bucket: str, Prefix: str = "", ContinuationToken: str | None = None, **_: Any
    ) -> dict[str, Any]:
        self.list_calls += 1
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix))
        # Like S3, the token is key-based, so it stays valid while keys are deleted.
        if ContinuationToken:
            keys = [k for k in keys if k > ContinuationToken]
        page = keys[: self.page_size]
        resp: dict[str, Any] = {
            "Contents": [
                {
                    "Key": k, "Size": len(self.objects[(Bucket, k)]),
                    "LastModified": self._meta(Bucket, k)["modified"],
                }
                for k in page
            ],
            "KeyCount": len(page),
        }
        if len(keys) > self.page_size:
            resp["IsTruncated"] = True
            resp["NextContinuationToken"] = page[-1]
        else:
            resp["IsTruncated"] = False
        return resp

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        objs = Delete["Objects"]
        assert len(objs) <= 1000, "S3 allows at most 1000 keys per delete_objects"
        self.delete_batches.append(len(objs))
        for o in objs:
            self.objects.pop((Bucket, o["Key"]), None)
            self.meta.pop((Bucket, o["Key"]), None)
        return {"Deleted": [{"Key": o["Key"]} for o in objs]}
