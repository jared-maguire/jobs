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


class ResultDecodeError(Sk8sError):
    """The job succeeded but its pickled result cannot be loaded on this client."""


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


class JobRejected(Sk8sError):
    """The Kubernetes API refused to create the Job (admission denial, forbidden, invalid spec).

    Typical cause: a policy the cluster enforces, e.g. GKE Autopilot's "privileged containers
    are not allowed". ``message`` is the API server's own explanation, verbatim.
    """

    def __init__(
        self, job_name: str, status: int | None, message: str, hint: str | None = None
    ) -> None:
        self.job_name = job_name
        self.status = status
        self.message = message
        self.hint = hint
        tail = f"\n{hint}" if hint else ""
        super().__init__(
            f"the cluster rejected job {job_name} (HTTP {status}): {message}{tail}"
        )


class ImagePullError(PodFailed):
    def __init__(self, job_name: str, image: str) -> None:
        super().__init__(job_name, reason=f"cannot pull image {image!r}")
        self.image = image


class VolumeError(Sk8sError):
    """Something is wrong with a volume the job asked to mount (base of the volume errors)."""


class VolumeNotFound(VolumeError):
    """``volumes=`` names a PersistentVolumeClaim that does not exist in the namespace."""

    def __init__(self, volume: str, namespace: str) -> None:
        self.volume = volume
        self.namespace = namespace
        super().__init__(
            f"volume {volume!r} does not exist in namespace {namespace!r}; create it first "
            f"(create_volume({volume!r}, ...)) or check the name"
        )


class VolumeNotPopulated(VolumeError):
    """A read-only mount of a ReadOnlyMany-capable volume that no rw job has completed on yet.

    A read-only mount is served from a hidden clone taken after a writer completes; before
    that there is nothing (not even a filesystem) to clone.
    """

    def __init__(self, volume: str) -> None:
        self.volume = volume
        super().__init__(
            f"volume {volume!r} has not been written by any completed job yet, so there is "
            f"nothing to mount read-only. Populate it first with a read-write mount "
            f"(volumes={{{volume!r}: {{\"access_mode\": \"rw\"}}}}) and wait for that job to "
            f"finish; read-only mounts see the volume as of the last completed writer."
        )


class VolumeWaitTimeout(VolumeError, TimeoutError):
    """A bounded wait on a volume ran out (a writer that is still running, or a clone being made)."""

    def __init__(self, volume: str, waiting_on: str, timeout: float, hint: str) -> None:
        self.volume = volume
        self.waiting_on = waiting_on
        self.timeout = timeout
        self.hint = hint
        super().__init__(
            f"volume {volume!r}: still waiting on {waiting_on} after {timeout:g}s. {hint}"
        )


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
