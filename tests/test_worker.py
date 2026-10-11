import threading
from pathlib import Path

import pytest

from sk8s import serialize, worker
from sk8s.errors import BlobTooLarge, RemoteTraceback
from sk8s.store.local import LocalStore


def run_worker(tmp_path: Path, func, args=(), kwargs={}):  # type: ignore[no-untyped-def]
    store = LocalStore(tmp_path)
    payload_uri = store.put("wf/j/payload", serialize.encode_payload(func, args, kwargs))
    env = {
        "SK8S_STORE": f"local:{tmp_path}",
        "SK8S_PAYLOAD_URI": payload_uri,
        "SK8S_RESULT_KEY": "wf/j/result",
    }
    code = worker.main(env)
    return code, store.get(store.uri_for("wf/j/result"))


def test_success(tmp_path: Path) -> None:
    code, blob = run_worker(tmp_path, lambda x: x * 2, (21,))
    assert code == 0
    assert serialize.decode_result(blob) == 42


def test_none_result(tmp_path: Path) -> None:
    code, blob = run_worker(tmp_path, lambda: None)
    assert code == 0
    assert serialize.decode_result(blob) is None


def test_user_exception_stored_and_exit_1(tmp_path: Path) -> None:
    def boom() -> None:
        raise KeyError("missing")

    code, blob = run_worker(tmp_path, boom)
    assert code == 1
    with pytest.raises(KeyError) as exc_info:
        serialize.decode_result(blob)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)


def test_unpicklable_result_becomes_error(tmp_path: Path) -> None:
    code, blob = run_worker(tmp_path, lambda: threading.Lock())
    assert code == 1
    with pytest.raises(Exception):
        serialize.decode_result(blob)


def test_oversized_result_becomes_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Patch the local store's put to enforce a tiny cap, simulating ConfigMap limits.
    real_put = LocalStore.put

    def capped_put(self, key, data, owner=None):  # type: ignore[no-untyped-def]
        if key.endswith("/result") and len(data) > 100 and b'"kind": "err"' not in data.split(b"\n", 1)[0]:
            raise BlobTooLarge("too big; use s3:// or gs://")
        return real_put(self, key, data, owner)

    monkeypatch.setattr(LocalStore, "put", capped_put)
    code, blob = run_worker(tmp_path, lambda: "x" * 10_000)
    assert code == 1
    with pytest.raises(BlobTooLarge, match="s3:// or gs://"):
        serialize.decode_result(blob)


def test_worker_threads_s3_endpoint_to_store(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}  # type: ignore[type-arg]

    def fake_get_store(store, **kw):  # type: ignore[no-untyped-def]
        seen.update(store=store, **kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(worker, "get_store", fake_get_store)
    with pytest.raises(RuntimeError):
        worker.main({"SK8S_STORE": "s3://b/p/", "SK8S_S3_ENDPOINT": "http://minio:9000"})
    assert seen["endpoint"] == "http://minio:9000"
