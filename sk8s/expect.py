"""``expect=`` evaluation: turn an expectation's verdict into ``ExpectationFailed``.

An expectation is a callable that receives the job's result value. It passes by returning
``None`` or ``True``; it fails by returning a string (the explanation) or ``False``, or by
raising any ``Exception`` (``AssertionError`` messages are used verbatim). Anything else it
returns is a failure too, because "passes silently" is the wrong default for a QC gate (a
returned list of problems would otherwise count as green).

This module runs inside the worker (and inside batch jobs, per item); it must stay free of
Kubernetes imports.
"""
from __future__ import annotations

import traceback
from typing import Any, Callable, NamedTuple

from sk8s.errors import ExpectationFailed

_REPR_LIMIT = 500


class Verdict(NamedTuple):
    """A failed expectation: the typed error plus the traceback text to chain on the client."""

    error: ExpectationFailed
    tb: str


def require_callable(expect: Any) -> None:
    if not callable(expect):
        raise TypeError(
            f"expect= must be a callable taking the job's result, got {type(expect).__name__}"
        )


def _name(expect: Callable[..., Any]) -> str:
    return str(getattr(expect, "__name__", None) or type(expect).__name__)


def _truncated_repr(value: Any, limit: int = _REPR_LIMIT) -> str:
    try:
        text = repr(value)
    except Exception as exc:  # noqa: BLE001 — a hostile __repr__ must not mask the verdict
        return f"<unrepresentable {type(value).__name__}: {type(exc).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_numpy_bool(value: Any) -> bool:
    return getattr(getattr(value, "dtype", None), "kind", None) == "b" and hasattr(value, "item")


def check(expect: Callable[..., Any], value: Any) -> Verdict | None:
    """Run ``expect(value)``; ``None`` if it passes, else a ``Verdict``.

    ``BaseException``s that are not ``Exception`` (``KeyboardInterrupt``, ``SystemExit``)
    propagate: they are not verdicts.
    """
    name = _name(expect)
    shown = _truncated_repr(value)

    def fail(message: str, tb: str = "") -> Verdict:
        return Verdict(
            ExpectationFailed(message, shown),
            tb or f"expectation {name!r} rejected the result: {message}\n",
        )

    try:
        verdict = expect(value)
    except AssertionError as exc:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return fail(str(exc) or f"expectation {name!r} failed an assertion", tb)
    except Exception as exc:  # noqa: BLE001
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return fail(f"expectation {name!r} raised {type(exc).__name__}: {exc}", tb)
    if verdict is None or verdict is True:
        return None
    if isinstance(verdict, str):
        return fail(verdict)
    if verdict is False:
        return fail(f"expectation {name!r} returned False for this result")
    if _is_numpy_bool(verdict):
        return None if verdict.item() else fail(f"expectation {name!r} returned False for this result")
    return fail(
        f"expectation {name!r} returned {type(verdict).__name__} {_truncated_repr(verdict, 80)}; "
        f"an expectation must return None, True, False or a str"
    )
