import pytest

from sk8s.errors import (
    BlobNotFound, BlobTooLarge, ImagePullError, JobTimeout, MapFailed,
    PodFailed, RemoteException, RemoteTraceback, Sk8sError, StoreError,
    VersionMismatch, ImageNotConfigured,
)


def test_hierarchy() -> None:
    assert issubclass(StoreError, Sk8sError)
    assert issubclass(BlobNotFound, StoreError)
    assert issubclass(BlobTooLarge, StoreError)
    assert issubclass(VersionMismatch, Sk8sError)
    assert issubclass(PodFailed, Sk8sError)
    assert issubclass(ImagePullError, PodFailed)
    assert issubclass(JobTimeout, TimeoutError)
    assert issubclass(JobTimeout, Sk8sError)


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


def test_map_failed_carries_results() -> None:
    e = MapFailed([(1, ValueError("bad"))], total=3, results={0: "a", 2: "c"})
    assert e.results == {0: "a", 2: "c"}
    # the message is unchanged by the successes
    assert str(e) == "1/3 map items failed; first failure at index 1: ValueError: bad"


def test_map_failed_results_default_empty() -> None:
    assert MapFailed([(0, ValueError("x"))], total=1).results == {}


def test_image_not_configured_explains_the_doctrine_in_five_lines() -> None:
    e = ImageNotConfigured()
    assert isinstance(e, Sk8sError)
    text = str(e)
    assert len(text.splitlines()) <= 5
    assert "docker build -f docker/Dockerfile.worker" in text
    assert "SK8S_IMAGE" in text and "image=" in text
    assert "publish" in text  # why: nothing is published anywhere, build your own
