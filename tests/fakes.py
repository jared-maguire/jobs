"""In-memory fakes for the kubernetes client surface sk8s uses."""
from __future__ import annotations

from typing import Any

from kubernetes.client import ApiException


class FakeCoreV1:
    def __init__(self) -> None:
        self.configmaps: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1ConfigMap

    def create_namespaced_config_map(self, namespace: str, body: Any) -> Any:
        self.configmaps[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_config_map(self, name: str, namespace: str) -> Any:
        try:
            return self.configmaps[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def delete_namespaced_config_map(self, name: str, namespace: str) -> None:
        if (namespace, name) not in self.configmaps:
            raise ApiException(status=404, reason="Not Found")
        del self.configmaps[(namespace, name)]

    def list_namespaced_config_map(self, namespace: str, label_selector: str = "") -> Any:
        class R:
            items = [v for (ns, _), v in self.configmaps.items() if ns == namespace]
        return R()
