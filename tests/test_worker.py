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


def run_service_worker(tmp_path: Path, func, args=(), kwargs={}):  # type: ignore[no-untyped-def]
    store = LocalStore(tmp_path)
    payload_uri = store.put("svc/api/payload", serialize.encode_payload(func, args, kwargs))
    env = {
        "SK8S_STORE": f"local:{tmp_path}",
        "SK8S_PAYLOAD_URI": payload_uri,
        "SK8S_SERVICE": "1",
    }
    return worker.main(env), store


def test_service_mode_return_is_error_and_writes_no_blob(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    code, _ = run_service_worker(tmp_path, lambda: "done")
    assert code == 1
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["payload"]


def test_service_mode_calls_function_with_args(tmp_path: Path) -> None:
    # cloudpickle serializes closures by value, so observe via a file instead
    marker = tmp_path / "marker.txt"

    def serve2(a: int, b: int = 0) -> None:
        marker.write_text(f"{a},{b}")
        raise RuntimeError("stop")

    code, _ = run_service_worker(tmp_path, serve2, (1,), {"b": 2})
    assert code == 1
    assert marker.read_text() == "1,2"


def test_service_mode_raise_logs_exception_and_writes_no_blob(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def boom() -> None:
        raise KeyError("missing")

    code, _ = run_service_worker(tmp_path, boom)
    assert code == 1
    assert any(r.exc_info for r in caplog.records)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["payload"]


def test_service_mode_does_not_need_result_key(tmp_path: Path) -> None:
    code, _ = run_service_worker(tmp_path, lambda: None)  # env has no SK8S_RESULT_KEY
    assert code == 1


def test_service_mode_sigterm_is_clean_shutdown(tmp_path: Path) -> None:
    import signal

    def serve_until_term() -> None:
        import os
        import signal
        import time

        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)  # interrupted by the handler's exception

    before = signal.getsignal(signal.SIGTERM)
    code, _ = run_service_worker(tmp_path, serve_until_term)
    assert code == 0
    assert signal.getsignal(signal.SIGTERM) is before  # handler restored


def test_service_flag_other_than_1_is_job_mode(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    payload_uri = store.put("wf/j/payload", serialize.encode_payload(lambda: 5, (), {}))
    env = {
        "SK8S_STORE": f"local:{tmp_path}",
        "SK8S_PAYLOAD_URI": payload_uri,
        "SK8S_RESULT_KEY": "wf/j/result",
        "SK8S_SERVICE": "0",
    }
    assert worker.main(env) == 0
    assert serialize.decode_result(store.get(store.uri_for("wf/j/result"))) == 5


def test_worker_threads_s3_endpoint_to_store(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}  # type: ignore[type-arg]

    def fake_get_store(store, **kw):  # type: ignore[no-untyped-def]
        seen.update(store=store, **kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(worker, "get_store", fake_get_store)
    with pytest.raises(RuntimeError):
        worker.main({"SK8S_STORE": "s3://b/p/", "SK8S_S3_ENDPOINT": "http://minio:9000"})
    assert seen["endpoint"] == "http://minio:9000"


# --- expect= ------------------------------------------------------------------

def run_worker_expect(tmp_path: Path, func, expect, args=()):  # type: ignore[no-untyped-def]
    store = LocalStore(tmp_path)
    payload_uri = store.put(
        "wf/j/payload", serialize.encode_payload(func, args, {}, expect=expect)
    )
    env = {
        "SK8S_STORE": f"local:{tmp_path}",
        "SK8S_PAYLOAD_URI": payload_uri,
        "SK8S_RESULT_KEY": "wf/j/result",
    }
    return worker.main(env), store.get(store.uri_for("wf/j/result"))


def test_expectation_passes(tmp_path: Path) -> None:
    code, blob = run_worker_expect(tmp_path, lambda x: x * 2, lambda r: r == 42 or "wrong", (21,))
    assert code == 0
    assert serialize.decode_result(blob) == 42


def test_expectation_failure_is_typed_exit_1_and_result_kept(tmp_path: Path) -> None:
    from sk8s.errors import ExpectationFailed

    code, blob = run_worker_expect(
        tmp_path, lambda: {"mapped": 0.61}, lambda r: f"mapped {r['mapped']:.0%} < 90%"
    )
    assert code == 1
    with pytest.raises(ExpectationFailed) as exc_info:
        serialize.decode_result(blob)
    assert exc_info.value.message == "mapped 61% < 90%"
    assert "0.61" in exc_info.value.result_repr
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)
    assert serialize.load_result_value(blob) == {"mapped": 0.61}  # result still inspectable


def test_function_exception_is_not_an_expectation_failure(tmp_path: Path) -> None:
    called = []

    def boom() -> None:
        raise KeyError("missing")

    code, blob = run_worker_expect(tmp_path, boom, lambda r: called.append(r))
    assert code == 1
    with pytest.raises(KeyError):
        serialize.decode_result(blob)
    assert called == []  # the expectation never ran


def test_expectation_that_raises_fails_semantically(tmp_path: Path) -> None:
    from sk8s.errors import ExpectationFailed

    def gate(r: int) -> None:
        assert r > 100, "too small"

    code, blob = run_worker_expect(tmp_path, lambda: 1, gate)
    assert code == 1
    with pytest.raises(ExpectationFailed, match="too small"):
        serialize.decode_result(blob)


def test_legacy_payload_without_expect_still_runs(tmp_path: Path) -> None:
    code, blob = run_worker(tmp_path, lambda: 7)
    assert code == 0 and serialize.decode_result(blob) == 7
