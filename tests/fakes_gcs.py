"""Dict-backed fake of the google-cloud-storage client surface that GCSStore uses."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from google.api_core.exceptions import NotFound


class FakeBlob:
    def __init__(self, client: FakeGCSClient, bucket: str, name: str) -> None:
        self._client = client
        self._bucket = bucket
        self.name = name

    def upload_from_string(self, data: bytes | str, **_: Any) -> None:
        raw = data.encode() if isinstance(data, str) else bytes(data)
        self._client.objects[(self._bucket, self.name)] = raw

    def download_as_bytes(self, **_: Any) -> bytes:
        try:
            return self._client.objects[(self._bucket, self.name)]
        except KeyError:
            raise NotFound(f"No such object: {self._bucket}/{self.name}") from None

    def delete(self, **_: Any) -> None:
        # Like the real API, deleting a missing object is a 404.
        try:
            del self._client.objects[(self._bucket, self.name)]
        except KeyError:
            raise NotFound(f"No such object: {self._bucket}/{self.name}") from None
        self._client.delete_calls += 1


class FakeBucket:
    def __init__(self, client: FakeGCSClient, name: str) -> None:
        self._client = client
        self.name = name

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self._client, self.name, name)


class FakeGCSClient:
    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.page_size = page_size
        self.list_calls = 0  # server pages fetched
        self.delete_calls = 0  # successful deletes

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
                yield FakeBlob(self, bucket, k)
            if len(keys) <= self.page_size:
                return
            token = page[-1]
