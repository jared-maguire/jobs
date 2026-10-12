"""Kubernetes resource quantities ("500m", "1Gi") as plain numbers."""
from __future__ import annotations

from typing import Any, Mapping

from kubernetes.utils import parse_quantity


def cpu_cores(quantity: Any) -> float | None:
    """CPU cores for a quantity like ``"500m"`` or ``2``; None if absent or unparseable."""
    if quantity is None or quantity == "":
        return None
    try:
        return float(parse_quantity(str(quantity)))  # type: ignore[no-untyped-call]
    except (ValueError, ArithmeticError):
        return None


def memory_bytes(quantity: Any) -> int | None:
    """Bytes for a quantity like ``"1Gi"`` or ``"512M"``; None if absent or unparseable."""
    if quantity is None or quantity == "":
        return None
    try:
        return int(parse_quantity(str(quantity)))  # type: ignore[no-untyped-call]
    except (ValueError, ArithmeticError):
        return None


def effective_request(
    requests: Mapping[str, Any] | None, limits: Mapping[str, Any] | None, resource: str
) -> Any:
    """The request for ``resource``; Kubernetes defaults an unset request to the limit."""
    if requests and requests.get(resource) not in (None, ""):
        return requests[resource]
    if limits and limits.get(resource) not in (None, ""):
        return limits[resource]
    return None
