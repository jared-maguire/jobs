from pathlib import Path

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
