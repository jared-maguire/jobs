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


class VersionChanged(StoreError):
    """An object's pinned version (GCS generation / S3 ETag / ConfigMap resourceVersion) no longer
    matches: it was overwritten or deleted since the pin was taken."""


class RetryExhausted(StoreError):
    """A conditional-write loop kept losing its race and gave up after its bounded retries."""


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


class ImageNotConfigured(Sk8sError):
    """No worker image is configured, and sk8s publishes none (you build your own).

    Raised before anything is created, when ``image`` is unset in the job options, ``Config`` /
    ``SK8S_IMAGE``, and no ``image_prefix`` is set to compose a name from.
    """

    def __init__(self) -> None:
        super().__init__(
            "no worker image is configured, and sk8s publishes none: build your own and name it.\n"
            "  docker build -f docker/Dockerfile.worker --build-arg PYTHON_VERSION=<your client's "
            "minor> -t REGISTRY/sk8s-worker:TAG .\n"
            "  docker push REGISTRY/sk8s-worker:TAG\n"
            "then pass image='REGISTRY/sk8s-worker:TAG' per job, or set it once: export "
            "SK8S_IMAGE=... (or `image = \"...\"` in config.toml; `image_prefix` composes the "
            "name instead).\n"
            "See the README quickstart, \"Build the worker image and push it\"."
        )


class VolumeError(Sk8sError):
    """Something is wrong with a volume the job asked to mount (base of the volume errors).

    Note: ``VolumeAttachBlocked`` and ``VolumeProvisioningFailed`` are NOT subclasses of
    this — they surface through the pod-failure machinery and subclass ``PodFailed``.
    Catch ``Sk8sError`` to cover both families.
    """


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


class VolumeAttachBlocked(PodFailed):
    """A job's pod is stuck waiting for a ReadWriteOnce volume that another node's pod holds.

    Kubernetes never times this out: the pod sits in ``ContainerCreating`` with a
    ``FailedAttachVolume`` event ("Waiting for detach ... Volume is already used by pod(s) X").
    ``holders`` names the pods that still hold the disk; ``message`` is the event verbatim.
    """

    def __init__(
        self, job_name: str, volume: str, claim: str, holders: list[str], message: str
    ) -> None:
        self.volume = volume
        self.claim = claim
        self.holders = holders
        self.message = message
        who = ", ".join(holders) or "another pod"
        super().__init__(
            job_name,
            reason=(
                f"cannot start: its volume {volume!r} (PVC {claim!r}) is still held by {who} "
                f"on another node, and a ReadWriteOnce disk attaches to one node at a time. "
                f"Kubernetes reports FailedAttachVolume: {message}\n"
                f"This wait has no timeout of its own. Either wait for {who} to finish and "
                f"resubmit, or run these jobs one at a time. To fan out many concurrent "
                f"readers, create the volume with access_modes=[\"ReadWriteOnce\", "
                f"\"ReadOnlyMany\"] and mount it read-only (the default): readers are then "
                f"served from a read-only clone that any number of nodes can attach."
            ),
        )


class VolumeProvisioningFailed(PodFailed):
    """A job's volume claim can never be provisioned, so its pod would stay ``Pending`` forever.

    The cause appears only on the PVC (``ProvisioningFailed``); the pod just says it "didn't
    find available persistent volumes to bind". The one sk8s detects: a ReadOnlyMany claim
    created without a content source, which GKE's PD CSI driver rejects.
    """

    def __init__(
        self, job_name: str, volume: str, claim: str, message: str, *, clone_grace: float = 0.0
    ) -> None:
        self.volume = volume
        self.claim = claim
        self.message = message
        if clone_grace:
            super().__init__(
                job_name,
                reason=(
                    f"the read-only clone {claim!r} of its volume {volume!r} was still not "
                    f"provisioned after {clone_grace:g}s. Kubernetes reports ProvisioningFailed: "
                    f"{message}\n"
                    f"The provisioner keeps retrying, so this was not a transient hiccup. Typical "
                    f"causes are storage quota or the cloud rate-limiting clones of one source "
                    f"disk: free quota or resubmit later (raise "
                    f"Config.volume_provision_grace_seconds to wait longer)."
                ),
            )
            return
        super().__init__(
            job_name,
            reason=(
                f"its volume {volume!r} (PVC {claim!r}) can never be provisioned. Kubernetes "
                f"reports ProvisioningFailed: {message}\n"
                f"A ReadOnlyMany claim needs a content source (a clone of a populated volume) "
                f"and cannot be created on its own. Create the volume with "
                f"access_modes=[\"ReadWriteOnce\", \"ReadOnlyMany\"], populate it with a "
                f"read-write mount, and mount it read-only: sk8s then manages the clone itself."
            ),
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


class ExpectationFailed(Sk8sError):
    """The job's function returned, but its ``expect=`` check rejected the result.

    A semantic failure (the answer is wrong), as opposed to an operational one (the
    function raised or the pod died). ``message`` is the expectation's own explanation;
    ``result_repr`` is the (truncated) ``repr`` of the offending result. The result itself
    is stored before the check runs, so it is still inspectable with ``keep_results``.
    """

    def __init__(self, message: str, result_repr: str) -> None:
        self.message = message
        self.result_repr = result_repr
        super().__init__(f"expectation failed: {message} (result: {result_repr})")

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.message, self.result_repr))


def _failure_census(failures: list[tuple[int, BaseException]]) -> str:
    """\"<n> failed their expectation, <m> errored\" when any failure was semantic, else \"\"."""
    semantic = sum(isinstance(e, ExpectationFailed) for _, e in failures)
    if not semantic:
        return ""
    return f"{semantic} failed their expectation, {len(failures) - semantic} errored"


class MapFailed(Sk8sError):
    """``map``/``starmap`` finished with at least one failed item.

    ``failures`` is ``[(index, exception), ...]``; ``results`` maps the index of every item that
    completed successfully to its value (so the successes are not lost); ``total`` is the item
    count. ``len(results) + len(failures) == total``.
    """

    def __init__(
        self,
        failures: list[tuple[int, BaseException]],
        total: int,
        results: dict[int, Any] | None = None,
    ) -> None:
        self.failures = failures
        self.total = total
        self.results: dict[int, Any] = {} if results is None else results
        first_i, first_e = failures[0]
        census = _failure_census(failures)
        split = f" ({census})" if census else ""
        super().__init__(
            f"{len(failures)}/{total} map items failed{split}; "
            f"first failure at index {first_i}: {type(first_e).__name__}: {first_e}"
        )


class PilotFailed(Sk8sError):
    """A ``pilot=`` run failed, so the remaining items were never submitted.

    ``failures`` is ``[(index, exception), ...]`` over the pilot items (``index`` is the
    position in the input); ``results`` maps the index of every pilot item that succeeded to
    its value (as on ``MapFailed``); ``pilot`` is how many items the pilot ran and ``total``
    how many the call was given.
    """

    def __init__(
        self,
        failures: list[tuple[int, BaseException]],
        pilot: int,
        total: int,
        results: dict[int, Any] | None = None,
    ) -> None:
        self.failures = failures
        self.results: dict[int, Any] = {} if results is None else results
        self.pilot = pilot
        self.total = total
        first_i, first_e = failures[0]
        census = _failure_census(failures)
        split = f" ({census})" if census else ""
        super().__init__(
            f"{len(failures)}/{pilot} pilot items failed{split}; the other "
            f"{total - pilot} items were not submitted; "
            f"first failure at index {first_i}: {type(first_e).__name__}: {first_e}"
        )


class ArtifactError(Sk8sError):
    """Base class for artifact-layer failures."""


class ArtifactNotFound(ArtifactError):
    """No known location of the artifact holds its bytes any more."""


class ArtifactChanged(ArtifactError):
    """An input artifact is not what was pinned: overwritten in place (new generation / ETag) or
    its bytes hash to something else. A changed input is a different artifact, so the run must
    not silently use it."""


class PublishConflict(ArtifactError):
    """The publish destination already holds different content; publish never overwrites."""
