"""In-pod entrypoint: fetch payload, run the function, store result or error.

Exit code 0 = user function succeeded; 1 = it (or result storage) failed, so
the Job's backoffLimit governs retries. A result blob is always written unless
the store itself is unreachable.
"""
from __future__ import annotations

import os
import sys
from typing import Mapping

from sk8s import serialize
from sk8s.store import get_store


def main(environ: Mapping[str, str]) -> int:
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
