"""Kubernetes client bootstrap: in-pod detection, a cached config load, and per-call API clients."""
from __future__ import annotations

from pathlib import Path

from kubernetes import client, config

from sk8s.config import DEFAULT_NAMESPACE

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


def context_namespace() -> str | None:
    """The namespace sk8s is running in or pointed at, or None if nothing explicit names one.

    In a pod: the serviceaccount namespace. Outside: the active kube context's namespace,
    only when that context sets one (an unset context namespace is None, not "default").
    """
    if in_pod():
        return (_SA_DIR / "namespace").read_text().strip()
    load()
    _, active = config.list_kube_config_contexts()  # type: ignore[no-untyped-call]
    return (active.get("context") or {}).get("namespace") or None


def current_namespace() -> str:
    """Resolve a namespace from nothing: pod / kube-context namespace if set, else "sk8s".

    Callers with an explicit namespace (config file, SK8S_NAMESPACE, kwargs) use that
    instead and never reach this. The fallback is never "default".
    """
    return context_namespace() or DEFAULT_NAMESPACE


def rbac_api() -> client.RbacAuthorizationV1Api:
    load()
    return client.RbacAuthorizationV1Api()


def storage_api() -> client.StorageV1Api:
    load()
    return client.StorageV1Api()
