"""Typed exceptions. Infra failures get specific classes with actionable text."""
from __future__ import annotations

from typing import Any


class Sk8sError(Exception):
    pass


class StoreError(Sk8sError):
    pass


class BlobNotFound(StoreError):
    pass


class BlobTooLarge(StoreError):
    pass


class VersionMismatch(Sk8sError):
    pass


class JobTimeout(Sk8sError, TimeoutError):
    def __init__(self, job_name: str, timeout: float) -> None:
        self.job_name = job_name
        self.timeout = timeout
        super().__init__(
            f"job {job_name} did not finish within {timeout}s "
            f"(it is still running; cancel with future.cancel())"
        )


class PodFailed(Sk8sError):
    def __init__(self, job_name: str, reason: str, logs: str | None = None) -> None:
        self.job_name = job_name
        self.reason = reason
        self.logs = logs
        tail = f"\n--- pod logs ---\n{logs}" if logs else ""
        super().__init__(f"job {job_name} failed: {reason}{tail}")


class ImagePullError(PodFailed):
    def __init__(self, job_name: str, image: str) -> None:
        super().__init__(job_name, reason=f"cannot pull image {image!r}")
        self.image = image


class RemoteTraceback(Exception):
    """Chained as __cause__ of re-raised remote exceptions; str() is the remote tb."""

    def __init__(self, tb_text: str) -> None:
        self.tb_text = tb_text
        super().__init__(f"\n\n--- remote traceback ---\n{tb_text}")


class RemoteException(Sk8sError):
    """Raised when the remote exception's class can't be reconstructed locally."""

    def __init__(self, type_name: str, message: str, tb_text: str) -> None:
        self.type_name = type_name
        self.message = message
        self.tb_text = tb_text
        super().__init__(f"{type_name}: {message}")

    def __reduce__(self) -> tuple[Any, ...]:
        # Exception's default reduce replays only ``args`` (the formatted message),
        # which does not match this three-argument __init__.
        return (type(self), (self.type_name, self.message, self.tb_text))


class MapFailed(Sk8sError):
    def __init__(self, failures: list[tuple[int, BaseException]], total: int) -> None:
        self.failures = failures
        self.total = total
        first_i, first_e = failures[0]
        super().__init__(
            f"{len(failures)}/{total} map items failed; "
            f"first failure at index {first_i}: {type(first_e).__name__}: {first_e}"
        )
