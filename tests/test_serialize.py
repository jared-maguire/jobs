import json
import sys

import pytest

from sk8s import serialize
from sk8s.errors import RemoteException, RemoteTraceback, VersionMismatch


def test_payload_roundtrip() -> None:
    f, args, kwargs = serialize.decode_payload(
        serialize.encode_payload(lambda x, y=1: x + y, (2,), {"y": 3})
    )
    assert f(*args, **kwargs) == 5


def test_payload_python_mismatch() -> None:
    data = serialize.encode_payload(len, ((),), {})
    head, _, body = data.partition(b"\n")
    h = json.loads(head)
    h["python"] = "2.7"
    with pytest.raises(VersionMismatch, match="sk8s image build"):
        serialize.decode_payload(json.dumps(h).encode() + b"\n" + body)


def test_result_roundtrip_none() -> None:
    assert serialize.decode_result(serialize.encode_result(None)) is None


def test_result_roundtrip_object() -> None:
    v = {"a": [1, 2, (3, 4)], "b": b"bytes"}
    assert serialize.decode_result(serialize.encode_result(v)) == v


def test_error_reraises_original_type() -> None:
    try:
        raise ValueError("remote boom")
    except ValueError as e:
        blob = serialize.encode_error(e)
    with pytest.raises(ValueError, match="remote boom") as exc_info:
        serialize.decode_result(blob)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)
    assert "remote boom" in exc_info.value.__cause__.tb_text


def test_unimportable_exception_falls_back() -> None:
    # Simulate an exception whose class only exists worker-side: encode a real
    # error, then corrupt the pickled class reference so unpickling fails.
    try:
        raise ValueError("boom")
    except ValueError as e:
        blob = serialize.encode_error(e)
    head, _, body = blob.partition(b"\n")
    body = body.replace(b"ValueError", b"ValueXrror")
    with pytest.raises(RemoteException) as exc_info:
        serialize.decode_result(head + b"\n" + body)
    assert "boom" in str(exc_info.value)
    assert "Traceback" in exc_info.value.tb_text
