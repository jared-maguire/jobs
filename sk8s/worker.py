"""In-pod entrypoint: fetch payload, run the function, store result or error.

Exit code 0 = user function succeeded; 1 = it (or result storage) failed, so
the Job's backoffLimit governs retries. A result blob is always written unless
the store itself is unreachable.

Service mode (SK8S_SERVICE=1): the function must block forever serving requests.
No result blob is ever written; returning or raising both exit 1 so the
Deployment restarts the pod. SIGTERM (pod deletion, scale-down, rollout) is a
clean shutdown: the process is PID 1 in the container, where an unhandled
SIGTERM is ignored, so without a handler every delete waits out the full
termination grace period before SIGKILL.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import Any, Mapping

from kubernetes import client

from sk8s import expect as expectations
from sk8s import serialize, staging
from sk8s.cas import ArtifactStore
from sk8s.store import get_store

log = logging.getLogger("sk8s.worker")


def _core_api() -> Any:
    from sk8s import k8s

    return k8s.core_api()


def _batch_api() -> Any:
    from sk8s import k8s

    return k8s.batch_api()


def _sweep(environ: Mapping[str, str]) -> None:
    """Best-effort retention sweep of the pod's namespace; never raises.

    Skipped when SK8S_SWEEP=0 or when this is not a job-mode pod (no SK8S_RESULT_KEY).
    Each anchor's blobs are removed from the store it recorded. The pod's own workflow
    is excluded so a sweep can never cascade-delete the job that is running it.
    """
    if environ.get("SK8S_SWEEP") == "0" or not environ.get("SK8S_RESULT_KEY"):
        return
    try:
        from sk8s.cleaning import clean

        own = environ.get("SK8S_WORKFLOW_ID")
        clean(
            environ.get("SK8S_NAMESPACE"), core_api=_core_api(), batch_api=_batch_api(),
            exclude=(own,) if own else (), artifacts=False,
            sandboxes=False,  # cluster-wide: a pod's namespaced Role can neither list nor delete them
        )
    except Exception:  # noqa: BLE001 — a sweep must never fail the job
        log.warning("retention sweep failed", exc_info=True)



class _Terminated(BaseException):
    """Raised from the SIGTERM handler to unwind the service function."""


def _on_sigterm(signum: int, frame: object) -> None:
    raise _Terminated()


def _run_service(environ: Mapping[str, str]) -> int:
    store = get_store(
        environ["SK8S_STORE"], namespace=environ.get("SK8S_NAMESPACE")
    )
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        func, args, kwargs = serialize.decode_payload(store.get(environ["SK8S_PAYLOAD_URI"]))
        func(*args, **kwargs)
    except _Terminated:
        log.info("SIGTERM received; shutting down")
        return 0
    except BaseException:  # noqa: BLE001 — any exit from a service function is a failure
        log.exception("service function raised")
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    log.error("service function returned; service functions must block")
    return 1


def main(environ: Mapping[str, str]) -> int:
    if environ.get("SK8S_SERVICE") == "1":
        return _run_service(environ)
    store = get_store(
        environ["SK8S_STORE"],
        namespace=environ.get("SK8S_NAMESPACE"),
        endpoint=environ.get("SK8S_S3_ENDPOINT"),
    )
    payload = store.get(environ["SK8S_PAYLOAD_URI"])
    result_key = environ["SK8S_RESULT_KEY"]
    _sweep(environ)
    owner = None
    if environ.get("SK8S_ANCHOR_NAME") and environ.get("SK8S_ANCHOR_UID"):
        # Not a controller reference: the anchor already controls the Job itself.
        owner = client.V1OwnerReference(
            api_version="v1", kind="ConfigMap", name=environ["SK8S_ANCHOR_NAME"],
            uid=environ["SK8S_ANCHOR_UID"], controller=False,
        )
    try:
        func, args, kwargs, expect = serialize.decode_payload_full(payload)
        # Artifact arguments are staged to scratch around the call; a call with neither
        # artifacts nor an OutDir parameter is made directly and touches no artifact code.
        with staging.artifact_run(
            func, args, kwargs, environ=environ,
            astore=lambda: ArtifactStore(store, environ["SK8S_STORE"]),
        ) as run:
            value = run.call()
            serialize.encode_result(value)  # a result that cannot be pickled fails here, pre-gate
            if expect is not None:
                # The gate runs on the value AS RETURNED (OutDir paths are still readable files),
                # BEFORE anything is uploaded: a gated-out result leaves no CAS object, no
                # reference-set entry and (client-side) no memo entry.
                verdict = expectations.check(expect, value)
                if verdict is not None:
                    # The rejected value rides along in the blob, so it stays inspectable.
                    store.put(
                        result_key,
                        serialize.encode_expect_failed(value, verdict.error, verdict.tb),
                        owner=owner,
                    )
                    return 1
            value, produced = run.finish(value)  # hash + upload declared outputs
            blob = serialize.encode_result(value, [a.to_dict() for a in produced])
        store.put(result_key, blob, owner=owner)
        return 0
    except BaseException as exc:  # noqa: BLE001 — everything becomes an error result
        store.put(result_key, serialize.encode_error(exc), owner=owner)
        return 1


if __name__ == "__main__":
    sys.exit(main(os.environ))
