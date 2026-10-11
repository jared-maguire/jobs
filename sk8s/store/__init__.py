from __future__ import annotations

from typing import Any

from sk8s.store.base import BlobStore
from sk8s.store.local import LocalStore


def get_store(
    store: str,
    *,
    namespace: str | None = None,
    core_api: Any | None = None,
    endpoint: str | None = None,
) -> BlobStore:
    if store.startswith("local:"):
        return LocalStore(store[len("local:"):])
    if store == "k8s":
        from sk8s.store.configmap import ConfigMapStore

        if namespace is None or core_api is None:
            from sk8s import k8s  # lazy: only needed to fill in what wasn't injected

            if namespace is None:
                namespace = k8s.current_namespace()
            if core_api is None:
                core_api = k8s.core_api()
        return ConfigMapStore(namespace=namespace, core_api=core_api)
    if store.startswith("s3://"):
        from sk8s.store.s3 import S3Store

        return S3Store.from_url(store, endpoint=endpoint)
    if store.startswith("gs://"):
        from sk8s.store.gcs import GCSStore

        return GCSStore.from_url(store)
    raise ValueError(f"unknown store {store!r}")


__all__ = ["BlobStore", "LocalStore", "get_store"]
