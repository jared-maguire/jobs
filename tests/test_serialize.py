import decimal
import json
import sys

import pytest

from sk8s import serialize
from sk8s.errors import RemoteException, RemoteTraceback, ResultDecodeError, Sk8sError, VersionMismatch


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


def test_undecodable_ok_result_raises_decode_error() -> None:
    # A successful job whose result references a module the client lacks
    # (e.g. numpy.float64 without numpy): corrupt the module name in the pickle.
    blob = serialize.encode_result(decimal.Decimal("1.5"))
    head, _, body = blob.partition(b"\n")
    assert b"decimal" in body
    body = body.replace(b"decimal", b"decimaX")
    with pytest.raises(ResultDecodeError) as exc_info:
        serialize.decode_result(head + b"\n" + body)
    assert isinstance(exc_info.value, Sk8sError)
    assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)
    msg = str(exc_info.value)
    assert "job succeeded" in msg
    assert "decimaX" in msg
    assert "install the missing module" in msg


def test_ok_result_with_builtin_types_unaffected() -> None:
    v = {"a": [1, 2.5, (3, None)], 4: "x"}
    assert serialize.decode_result(serialize.encode_result(v)) == v


# --- expect= envelope --------------------------------------------------------

def test_payload_without_expect_is_the_legacy_three_tuple_body() -> None:
    import pickle

    data = serialize.encode_payload(len, ((),), {})
    head, _, body = data.partition(b"\n")
    assert "expect" not in json.loads(head)
    assert len(pickle.loads(body)) == 3
    assert serialize.decode_payload_full(data).expect is None


def test_payload_with_expect_roundtrips_and_flags_the_header() -> None:
    data = serialize.encode_payload(len, ((),), {}, expect=lambda r: r == 0 or "nonzero")
    head, _, _ = data.partition(b"\n")
    h = json.loads(head)
    assert h["expect"] is True and h["envelope"] == serialize.ENVELOPE_VERSION
    payload = serialize.decode_payload_full(data)
    assert payload.func is len and payload.expect is not None
    assert payload.expect(0) is False or payload.expect(0) is True
    # the three-tuple decoder still works (services use it)
    f, args, kwargs = serialize.decode_payload(data)
    assert f is len


def test_payload_from_a_newer_envelope_is_refused_clearly() -> None:
    data = serialize.encode_payload(len, ((),), {})
    head, _, body = data.partition(b"\n")
    h = json.loads(head)
    h["envelope"] = serialize.ENVELOPE_VERSION + 1
    with pytest.raises(VersionMismatch, match="sk8s image build"):
        serialize.decode_payload_full(json.dumps(h).encode() + b"\n" + body)


def test_expect_failed_result_reraises_typed_with_remote_traceback() -> None:
    from sk8s.errors import ExpectationFailed

    blob = serialize.encode_expect_failed(
        {"mapped": 0.61}, ExpectationFailed("61% < 90%", "{'mapped': 0.61}"), "tb text"
    )
    with pytest.raises(ExpectationFailed) as exc_info:
        serialize.decode_result(blob)
    assert exc_info.value.message == "61% < 90%"
    assert exc_info.value.result_repr == "{'mapped': 0.61}"
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)
    assert "tb text" in str(exc_info.value.__cause__)


def test_result_value_is_recoverable_from_a_failed_expectation_blob() -> None:
    from sk8s.errors import ExpectationFailed

    blob = serialize.encode_expect_failed(
        {"mapped": 0.61}, ExpectationFailed("m", "r"), ""
    )
    assert serialize.load_result_value(blob) == {"mapped": 0.61}
    assert serialize.load_result_value(serialize.encode_result(5)) == 5
    with pytest.raises(ValueError, match="no result value"):
        serialize.load_result_value(serialize.encode_error(KeyError("k")))


def test_result_headers_carry_the_envelope_version() -> None:
    for blob in (serialize.encode_result(1), serialize.encode_error(KeyError("k"))):
        assert json.loads(blob.partition(b"\n")[0])["envelope"] == serialize.ENVELOPE_VERSION
