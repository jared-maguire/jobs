"""Dict-backed fake of the boto3 S3 client surface that S3Store uses."""
from __future__ import annotations

import io
from typing import Any

from botocore.exceptions import ClientError


def _client_error(code: str, op: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


class FakeS3Client:
    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.page_size = page_size
        self.list_calls = 0
        self.delete_batches: list[int] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: Any) -> dict[str, Any]:
        self.objects[(Bucket, Key)] = bytes(Body)
        return {"ETag": '"x"'}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        try:
            return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
        except KeyError:
            raise _client_error("NoSuchKey", "GetObject", 404) from None

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.objects.pop((Bucket, Key), None)
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
        resp: dict[str, Any] = {"Contents": [{"Key": k} for k in page], "KeyCount": len(page)}
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
        return {"Deleted": [{"Key": o["Key"]} for o in objs]}
