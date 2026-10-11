"""In-pod entrypoint: fetch payload, run the function, store result or error.

Exit code 0 = user function succeeded; 1 = it (or result storage) failed, so
the Job's backoffLimit governs retries. A result blob is always written unless
the store itself is unreachable.

Service mode (SK8S_SERVICE=1): the function must block forever serving requests.
No result blob is ever written; returning or raising both exit 1 so the
Deployment restarts the pod.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Mapping

from sk8s import serialize
from sk8s.store import get_store

log = logging.getLogger("sk8s.worker")


def _run_service(environ: Mapping[str, str]) -> int:
    store = get_store(
        environ["SK8S_STORE"], namespace=environ.get("SK8S_NAMESPACE")
    )
    try:
        func, args, kwargs = serialize.decode_payload(store.get(environ["SK8S_PAYLOAD_URI"]))
        func(*args, **kwargs)
    except BaseException:  # noqa: BLE001 — any exit from a service function is a failure
        log.exception("service function raised")
        return 1
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
