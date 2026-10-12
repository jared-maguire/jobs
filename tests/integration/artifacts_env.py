"""Shared environment of the artifact integration suites (k8s store, MinIO).

They run in a namespace of their own (``sk8s-it-art`` by default; ``SK8S_ARTIFACTS_TEST_NAMESPACE``
overrides), created with ``sk8s config``'s own machinery and deleted again at exit if the suite
created it - never in whatever ``SK8S_NAMESPACE`` the other suites share (CI uses ``default``,
whose hand-made RBAC must not be replaced). The worker image comes from
``SK8S_ARTIFACTS_TEST_IMAGE`` (CI points it at the image it built for every suite).
"""
from __future__ import annotations

import atexit
import os
import threading

from kubernetes import client
from kubernetes.client import ApiException

from sk8s import k8s
from sk8s.profile import apply, profile_for

NAMESPACE = os.environ.get("SK8S_ARTIFACTS_TEST_NAMESPACE", "sk8s-it-art")
IMAGE = os.environ.get("SK8S_ARTIFACTS_TEST_IMAGE", "sk8s-dev-artifacts:latest")

_lock = threading.Lock()
_ready = False


def ensure_namespace() -> str:
    """Create (once per process) the artifact suites' namespace + RBAC; returns its name."""
    global _ready
    assert NAMESPACE.startswith("sk8s-it-"), f"refusing to run artifact tests in namespace {NAMESPACE!r}"
    with _lock:
        if not _ready:
            lines = apply(profile_for("local", namespace=NAMESPACE, store="k8s"))
            if lines and lines[0].endswith("created"):
                atexit.register(_delete_namespace)
            _ready = True
    return NAMESPACE


def _delete_namespace() -> None:
    try:
        client.CoreV1Api(k8s.core_api().api_client).delete_namespace(name=NAMESPACE)
    except ApiException:
        pass
    except Exception:  # noqa: BLE001 — best-effort teardown at interpreter exit
        pass
