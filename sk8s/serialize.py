"""Payload/result wire format: one JSON header line + cloudpickle body.

Payload header: {"kind": "payload", "sk8s": ..., "cloudpickle": ..., "python": "3.12"}
Result header:  {"kind": "ok"} or
                {"kind": "err", "type": ..., "message": ..., "tb": "..."}
An "err" body is the cloudpickled exception, or b"" when it wasn't picklable.
"""
from __future__ import annotations

import json
import pickle
import sys
import traceback
from typing import Any, Callable

import cloudpickle  # type: ignore[import-untyped]  # no py.typed marker upstream

from sk8s import __version__
from sk8s.errors import RemoteException, RemoteTraceback, VersionMismatch


def _python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def encode_payload(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> bytes:
    header = {
        "kind": "payload",
        "sk8s": __version__,
        "cloudpickle": cloudpickle.__version__,
        "python": _python_minor(),
    }
    body: bytes = cloudpickle.dumps((func, args, kwargs))
    return json.dumps(header).encode() + b"\n" + body


def decode_payload(data: bytes) -> tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]:
    head, _, body = data.partition(b"\n")
    header = json.loads(head)
    if header["python"] != _python_minor():
        raise VersionMismatch(
            f"payload was built on Python {header['python']} but this worker runs "
            f"{_python_minor()} — use a matching image (rebuild with `sk8s image build`)"
        )
    func, args, kwargs = pickle.loads(body)
    return func, args, kwargs


def encode_result(value: Any) -> bytes:
    body: bytes = cloudpickle.dumps(value)
    return json.dumps({"kind": "ok"}).encode() + b"\n" + body


def encode_error(exc: BaseException) -> bytes:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    header = {
        "kind": "err",
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
        return pickle.loads(body)
    # kind == "err"
    if body:
        try:
            exc: BaseException = pickle.loads(body)
        except Exception:
            exc = RemoteException(header["type"], header["message"], header["tb"])
    else:
        exc = RemoteException(header["type"], header["message"], header["tb"])
    raise exc from RemoteTraceback(header["tb"])
