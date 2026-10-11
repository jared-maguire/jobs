import json
from pathlib import Path

import pytest
from kubernetes import config as kube_config_module

from sk8s import k8s


def test_in_pod_false_without_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(k8s, "_SA_DIR", Path("/nonexistent"))
    assert k8s.in_pod() is False


def test_in_pod_true_with_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "token").write_text("tok")
    (tmp_path / "namespace").write_text("myns")
    monkeypatch.setattr(k8s, "_SA_DIR", tmp_path)
    assert k8s.in_pod() is True
    assert k8s.current_namespace() == "myns"


def _use_kubeconfig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namespace: str | None
) -> None:
    """Point the real kubeconfig loader at a temp file, outside any pod."""
    context: dict[str, str] = {"cluster": "c", "user": "u"}
    if namespace is not None:
        context["namespace"] = namespace
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "ctx",
        "clusters": [{"name": "c", "cluster": {"server": "https://127.0.0.1:1"}}],
        "users": [{"name": "u", "user": {"token": "tok"}}],
        "contexts": [{"name": "ctx", "context": context}],
    }
    path = tmp_path / "kubeconfig"
    path.write_text(json.dumps(kubeconfig))
    # The loader captures KUBECONFIG at import time, so patch the module constant it reads per call.
    monkeypatch.setattr(kube_config_module.kube_config, "KUBE_CONFIG_DEFAULT_LOCATION", str(path))
    monkeypatch.setattr(k8s, "_SA_DIR", tmp_path / "no-service-account")
    monkeypatch.setattr(k8s, "_loaded", False)


def test_current_namespace_outside_pod_uses_context_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_kubeconfig(tmp_path, monkeypatch, namespace="somens")
    assert k8s.current_namespace() == "somens"


def test_current_namespace_outside_pod_defaults_when_context_has_no_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_kubeconfig(tmp_path, monkeypatch, namespace=None)
    assert k8s.current_namespace() == "default"


def test_load_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_kubeconfig(tmp_path, monkeypatch, namespace="somens")
    calls: list[int] = []
    monkeypatch.setattr(k8s.config, "load_kube_config", lambda: calls.append(1))
    k8s.load()
    k8s.load()
    assert calls == [1]
