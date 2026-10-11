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
from typing import Mapping

from sk8s import serialize
from sk8s.store import get_store

log = logging.getLogger("sk8s.worker")


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
        environ["SK8S_STORE"], namespace=environ.get("SK8S_NAMESPACE")
    )
    payload = store.get(environ["SK8S_PAYLOAD_URI"])
    result_key = environ["SK8S_RESULT_KEY"]
    try:
        func, args, kwargs = serialize.decode_payload(payload)
        value = func(*args, **kwargs)
        blob = serialize.encode_result(value)
        store.put(result_key, blob)
        return 0
    except BaseException as exc:  # noqa: BLE001 — everything becomes an error result
        store.put(result_key, serialize.encode_error(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main(os.environ))
