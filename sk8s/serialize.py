"""Payload/result wire format: one JSON header line + cloudpickle body.

Payload header: {"kind": "payload", "envelope": 2, "sk8s": ..., "cloudpickle": ..., "python": "3.12"}
                plus "expect": true when the body carries an expectation (see below).
Result header:  {"kind": "ok", "envelope": 2} or
                {"kind": "err", "envelope": 2, "type": ..., "message": ..., "tb": "..."} or
                {"kind": "expect_failed", "envelope": 2, "message": ..., "result_repr": ..., "tb": ...}
An "err" body is the cloudpickled exception, or b"" when it wasn't picklable. An
"expect_failed" body is the cloudpickled result VALUE the expectation rejected, so the
result stays inspectable (``load_result_value``).

Envelope versions: 1 (v2.0 and earlier) had no ``envelope`` field and a 3-tuple body
``(func, args, kwargs)``. 2 adds ``expect=``: a payload with an expectation has a 4-tuple
body ``(func, args, kwargs, expect)``, so a worker that predates it fails the decode loudly
(ValueError, unpack) before running anything instead of silently skipping the check. A
worker refuses payloads from an envelope newer than it knows (VersionMismatch).
"""
from __future__ import annotations

import json
import pickle
import sys
import traceback
from typing import Any, Callable, NamedTuple

import cloudpickle  # type: ignore[import-untyped]  # no py.typed marker upstream

from sk8s import __version__
from sk8s.errors import (
    ExpectationFailed, RemoteException, RemoteTraceback, ResultDecodeError, VersionMismatch,
)

ENVELOPE_VERSION = 2


def _python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


class Payload(NamedTuple):
    func: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    expect: Callable[..., Any] | None


def encode_payload(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expect: Callable[..., Any] | None = None,
) -> bytes:
    header: dict[str, Any] = {
        "kind": "payload",
        "envelope": ENVELOPE_VERSION,
        "sk8s": __version__,
        "cloudpickle": cloudpickle.__version__,
        "python": _python_minor(),
    }
    packed: tuple[Any, ...] = (func, args, kwargs)
    if expect is not None:
        header["expect"] = True
        packed = (func, args, kwargs, expect)
    body: bytes = cloudpickle.dumps(packed)
    return json.dumps(header).encode() + b"\n" + body


def decode_payload_full(data: bytes) -> Payload:
    head, _, body = data.partition(b"\n")
    header = json.loads(head)
    if header["python"] != _python_minor():
        raise VersionMismatch(
            f"payload was built on Python {header['python']} but this worker runs "
            f"{_python_minor()} — use a matching image (rebuild with `sk8s image build`)"
        )
    if header.get("envelope", 1) > ENVELOPE_VERSION:
        raise VersionMismatch(
            f"payload uses wire envelope {header['envelope']} but this worker only understands "
            f"up to {ENVELOPE_VERSION} (sk8s {__version__}) — the image is older than the client; "
            f"rebuild it with `sk8s image build`"
        )
    packed = pickle.loads(body)
    if header.get("expect"):
        func, args, kwargs, expect = packed
        return Payload(func, args, kwargs, expect)
    func, args, kwargs = packed
    return Payload(func, args, kwargs, None)


def decode_payload(data: bytes) -> tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]:
    """The (func, args, kwargs) of a payload; any expectation it carries is not returned
    (job workers use ``decode_payload_full``)."""
    func, args, kwargs, _ = decode_payload_full(data)
    return func, args, kwargs


def encode_result(value: Any) -> bytes:
    body: bytes = cloudpickle.dumps(value)
    return json.dumps({"kind": "ok", "envelope": ENVELOPE_VERSION}).encode() + b"\n" + body


def encode_expect_failed(value: Any, error: ExpectationFailed, tb: str) -> bytes:
    """Result blob for a function that returned ``value`` which its expectation rejected."""
    header = {
        "kind": "expect_failed",
        "envelope": ENVELOPE_VERSION,
        "message": error.message,
        "result_repr": error.result_repr,
        "tb": tb,
    }
    body: bytes = cloudpickle.dumps(value)
    return json.dumps(header).encode() + b"\n" + body


def load_result_value(data: bytes) -> Any:
    """The function's return value from an ``ok`` or ``expect_failed`` blob (the latter keeps
    the rejected value for inspection). Raises ValueError for an ``err`` blob."""
    head, _, body = data.partition(b"\n")
    kind = json.loads(head)["kind"]
    if kind not in ("ok", "expect_failed"):
        raise ValueError(f"a {kind!r} result blob holds no result value")
    return pickle.loads(body)


def encode_error(exc: BaseException) -> bytes:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    header = {
        "kind": "err",
        "envelope": ENVELOPE_VERSION,
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc),
        "tb": tb,
    }
    body: bytes
    try:
        body = cloudpickle.dumps(exc)
    except Exception:
        body = b""
    return json.dumps(header).encode() + b"\n" + body


def decode_result(data: bytes) -> Any:
    head, _, body = data.partition(b"\n")
    header = json.loads(head)
    if header["kind"] == "ok":
        try:
            return pickle.loads(body)
        except Exception as decode_exc:
            raise ResultDecodeError(
                "the job succeeded but its result could not be deserialized on this "
                f"client: {type(decode_exc).__name__}: {decode_exc} \u2014 install the missing module "
                "locally (results are pickled; the client needs the modules of the types "
                "a job returns) or return builtin types from the function"
            ) from decode_exc
    if header["kind"] == "expect_failed":
        raise ExpectationFailed(header["message"], header["result_repr"]) from RemoteTraceback(
            header["tb"]
        )
    # kind == "err"
    if body:
        try:
            exc: BaseException = pickle.loads(body)
        except Exception:
            exc = RemoteException(header["type"], header["message"], header["tb"])
    else:
        exc = RemoteException(header["type"], header["message"], header["tb"])
    raise exc from RemoteTraceback(header["tb"])
