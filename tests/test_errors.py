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


def test_map_failed_plain_message_unchanged_without_expectations() -> None:
    e = MapFailed([(1, ValueError("bad"))], total=4)
    assert str(e) == "1/4 map items failed; first failure at index 1: ValueError: bad"


def test_map_failed_distinguishes_expectation_failures_from_crashes() -> None:
    from sk8s.errors import ExpectationFailed

    failures = [
        (2, ValueError("crash")),
        (5, ExpectationFailed("mapped 61% < 90%", "{'mapped': 0.61}")),
        (9, ExpectationFailed("mapped 40% < 90%", "{'mapped': 0.4}")),
    ]
    text = str(MapFailed(failures, total=12))
    assert "3/12 map items failed" in text
    assert "2 failed their expectation" in text
    assert "1 errored" in text
    assert "index 2" in text  # first failure is still named


def test_expectation_failed_carries_message_and_truncated_repr() -> None:
    from sk8s.errors import ExpectationFailed

    e = ExpectationFailed("mapped 61% < 90%", "{'mapped': 0.61}")
    assert e.message == "mapped 61% < 90%"
    assert e.result_repr == "{'mapped': 0.61}"
    assert "mapped 61% < 90%" in str(e) and "{'mapped': 0.61}" in str(e)
    assert isinstance(e, Sk8sError)


def test_expectation_failed_pickles() -> None:
    import pickle

    from sk8s.errors import ExpectationFailed

    e = ExpectationFailed("m", "r")
    back = pickle.loads(pickle.dumps(e))
    assert isinstance(back, ExpectationFailed)
    assert (back.message, back.result_repr) == ("m", "r")
    assert str(back) == str(e)


def test_pilot_failed_carries_failures_and_says_nothing_further_submitted() -> None:
    from sk8s.errors import ExpectationFailed, PilotFailed

    failures = [(1, ExpectationFailed("low", "0.5")), (2, ValueError("boom"))]
    e = PilotFailed(failures, pilot=3, total=48)
    assert e.failures == failures and e.pilot == 3 and e.total == 48
    text = str(e)
    assert "2/3 pilot items failed" in text
    assert "45" in text and "not submitted" in text
    assert "1 failed their expectation" in text and "1 errored" in text
    assert isinstance(e, Sk8sError)


def test_pilot_failed_carries_the_pilot_successes() -> None:
    from sk8s.errors import PilotFailed

    e = PilotFailed([(1, ValueError("x"))], pilot=3, total=9, results={0: "a", 2: "c"})
    assert e.results == {0: "a", 2: "c"}
    assert PilotFailed([(0, ValueError("x"))], pilot=1, total=2).results == {}
