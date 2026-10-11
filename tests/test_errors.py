import pytest

from sk8s.errors import (
    BlobNotFound, BlobTooLarge, ImagePullError, JobTimeout, MapFailed,
    PodFailed, RemoteException, RemoteTraceback, Sk8sError, StoreError,
    VersionMismatch,
)


def test_hierarchy() -> None:
    assert issubclass(StoreError, Sk8sError)
    assert issubclass(BlobNotFound, StoreError)
    assert issubclass(BlobTooLarge, StoreError)
    assert issubclass(VersionMismatch, Sk8sError)
    assert issubclass(PodFailed, Sk8sError)
    assert issubclass(ImagePullError, PodFailed)


def test_job_timeout_message() -> None:
    e = JobTimeout("job-abc123", 60.0)
    assert e.job_name == "job-abc123"
    assert "60" in str(e)


def test_pod_failed_fields() -> None:
    e = PodFailed("job-x", reason="OOMKilled", logs="...killed...")
    assert e.reason == "OOMKilled"
    assert "OOMKilled" in str(e)


def test_remote_exception_carries_traceback() -> None:
    e = RemoteException("mylib.CustomError", "boom", "Traceback (most recent...)")
    assert "mylib.CustomError" in str(e) and "boom" in str(e)
    assert e.tb_text.startswith("Traceback")


def test_remote_exception_survives_pickle_roundtrip() -> None:
    import pickle

    e = RemoteException("mylib.CustomError", "boom", "Traceback (most recent...)")
    back = pickle.loads(pickle.dumps(e))
    assert isinstance(back, RemoteException)
    assert (back.type_name, back.message, back.tb_text) == (e.type_name, e.message, e.tb_text)
    assert str(back) == str(e)


def test_map_failed_summary() -> None:
    failures = [(3, ValueError("bad")), (7, KeyError("k"))]
    e = MapFailed(failures, total=10)
    assert e.failures == failures
    assert "2/10" in str(e) and "ValueError" in str(e)
