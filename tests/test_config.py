from pathlib import Path

import pytest

from sk8s import config as config_mod
from sk8s.config import DEFAULT_NAMESPACE, Config, config_path, load_config


def test_defaults() -> None:
    cfg = load_config(path=Path("/nonexistent"), env={})
    assert cfg.store == "k8s"
    assert cfg.service_account == "sk8s"
    assert cfg.ttl_seconds_after_finished == 3600
    assert cfg.retention_days == 7
    assert cfg.pull_policy == "IfNotPresent"
    assert cfg.namespace is None and cfg.image is None


def test_file_layer(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('store = "s3://bkt/sk8s/"\nnamespace = "prod"\n')
    cfg = load_config(path=f, env={})
    assert cfg.store == "s3://bkt/sk8s/"
    assert cfg.namespace == "prod"


def test_env_overrides_file(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('namespace = "prod"\nttl_seconds_after_finished = 10\n')
    cfg = load_config(path=f, env={"SK8S_NAMESPACE": "dev", "SK8S_KEEP_RESULTS": "true"})
    assert cfg.namespace == "dev"
    assert cfg.ttl_seconds_after_finished == 10
    assert cfg.keep_results is True


def test_kwargs_override_everything(tmp_path: Path) -> None:
    cfg = load_config(path=Path("/nonexistent"), env={"SK8S_NAMESPACE": "dev"}, namespace="x")
    assert cfg.namespace == "x"


def test_frozen() -> None:
    cfg = load_config(path=Path("/nonexistent"), env={})
    try:
        cfg.store = "other"  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised


def test_store_endpoint_layers(tmp_path: Path) -> None:
    assert load_config(path=Path("/nonexistent"), env={}).store_endpoint is None
    f = tmp_path / "config.toml"
    f.write_text('store_endpoint = "http://file:9000"\n')
    assert load_config(path=f, env={}).store_endpoint == "http://file:9000"
    cfg = load_config(path=f, env={"SK8S_STORE_ENDPOINT": "http://env:9000"})
    assert cfg.store_endpoint == "http://env:9000"


def test_rwx_storage_class_layers(tmp_path: Path) -> None:
    assert load_config(path=Path("/nonexistent"), env={}).rwx_storage_class is None
    f = tmp_path / "config.toml"
    f.write_text('rwx_storage_class = "file-rwx"\n')
    assert load_config(path=f, env={}).rwx_storage_class == "file-rwx"
    cfg = load_config(path=f, env={"SK8S_RWX_STORAGE_CLASS": "env-rwx"})
    assert cfg.rwx_storage_class == "env-rwx"
    assert load_config(path=f, env={}, rwx_storage_class="kw-rwx").rwx_storage_class == "kw-rwx"


def test_store_env_default_is_independent_and_file_settable(tmp_path: Path) -> None:
    a, b = Config(), Config()
    assert a.store_env == {} and a.store_env is not b.store_env
    f = tmp_path / "config.toml"
    f.write_text('[store_env]\nAWS_ACCESS_KEY_ID = "k"\n')
    assert load_config(path=f, env={}).store_env == {"AWS_ACCESS_KEY_ID": "k"}


def test_store_env_not_read_from_environment() -> None:
    cfg = load_config(path=Path("/nonexistent"), env={"SK8S_STORE_ENV": "junk"})
    assert cfg.store_env == {}


def test_default_namespace_constant_is_dedicated_not_default() -> None:
    assert DEFAULT_NAMESPACE == "sk8s"
    # The config itself stays unresolved (None) so a kube-context namespace can still apply.
    assert load_config(path=Path("/nonexistent"), env={}).namespace is None


def test_config_file_is_read_as_utf8(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "config.toml"
    f.write_bytes('image_prefix = "registry.example/caf\u00e9-\u00fcber/"\n'.encode("utf-8"))
    # Make an implicit-encoding read_text() decode as ascii, as on a C/POSIX locale.
    real_read_text = Path.read_text

    def strict_default(self, encoding=None, errors=None):  # type: ignore[no-untyped-def]
        return real_read_text(self, encoding=encoding or "ascii", errors=errors)

    monkeypatch.setattr(Path, "read_text", strict_default)
    assert load_config(path=f, env={}).image_prefix == "registry.example/café-über/"


def test_config_path_default_and_empty_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    default = tmp_path / "default.toml"
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", default)
    assert config_path({}) == default
    assert config_path({"SK8S_CONFIG": ""}) == default


def test_config_path_env_override(tmp_path: Path) -> None:
    assert config_path({"SK8S_CONFIG": str(tmp_path / "x.toml")}) == tmp_path / "x.toml"


def test_config_path_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_path({"SK8S_CONFIG": "~/c.toml"}) == tmp_path / "c.toml"


def test_load_config_reads_sk8s_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "never.toml")
    (tmp_path / "never.toml").write_text('namespace = "from-default"\n')
    f = tmp_path / "alt.toml"
    f.write_text('namespace = "from-alt"\nstore = "s3://b/"\n')
    cfg = load_config(env={"SK8S_CONFIG": str(f)})
    assert cfg.namespace == "from-alt" and cfg.store == "s3://b/"


def test_sk8s_config_changes_the_file_not_the_layering(tmp_path: Path) -> None:
    f = tmp_path / "alt.toml"
    f.write_text('namespace = "file"\nstore = "s3://b/"\n')
    env = {"SK8S_CONFIG": str(f), "SK8S_NAMESPACE": "env"}
    assert load_config(env=env).namespace == "env"  # env > file
    assert load_config(env=env, namespace="kw").namespace == "kw"  # kwargs > env
    assert load_config(env=env).store == "s3://b/"  # file still supplies the rest


def test_explicit_path_beats_sk8s_config(tmp_path: Path) -> None:
    a, b = tmp_path / "a.toml", tmp_path / "b.toml"
    a.write_text('namespace = "a"\n')
    b.write_text('namespace = "b"\n')
    assert load_config(path=a, env={"SK8S_CONFIG": str(b)}).namespace == "a"
