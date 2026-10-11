from pathlib import Path

from sk8s.config import DEFAULT_NAMESPACE, Config, load_config


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
