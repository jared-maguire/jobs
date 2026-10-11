"""Kubernetes client bootstrap: in-pod detection, a cached config load, and per-call API clients."""
from __future__ import annotations

from pathlib import Path

from kubernetes import client, config

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_loaded = False


def in_pod() -> bool:
    return (_SA_DIR / "token").exists()


def load() -> None:
    global _loaded
    if _loaded:
        return
    if in_pod():
        config.load_incluster_config()  # type: ignore[no-untyped-call]
    else:
        config.load_kube_config()  # type: ignore[no-untyped-call]
    _loaded = True


def core_api() -> client.CoreV1Api:
    load()
    return client.CoreV1Api()


def batch_api() -> client.BatchV1Api:
    load()
    return client.BatchV1Api()


def apps_api() -> client.AppsV1Api:
    load()
    return client.AppsV1Api()


def current_namespace() -> str:
    if in_pod():
        return (_SA_DIR / "namespace").read_text().strip()
    load()
    _, active = config.list_kube_config_contexts()  # type: ignore[no-untyped-call]
    ns = (active.get("context") or {}).get("namespace")
    return ns or "default"


def rbac_api() -> client.RbacAuthorizationV1Api:
    load()
    return client.RbacAuthorizationV1Api()
