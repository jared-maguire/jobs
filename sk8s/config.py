"""Layered, frozen configuration: defaults < toml file < SK8S_* env < kwargs."""
from __future__ import annotations

import dataclasses
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sk8s" / "config.toml"


@dataclass(frozen=True)
class Config:
    namespace: str | None = None        # None -> current kube-context namespace
    image: str | None = None            # None -> official image for this version
    image_prefix: str = ""
    pull_policy: str = "IfNotPresent"
    service_account: str = "sk8s"
    store: str = "k8s"                  # "k8s" | "local:<dir>" | "s3://..." | "gs://..."
    store_endpoint: str | None = None   # S3-compatible endpoint URL; env SK8S_S3_ENDPOINT wins
    ttl_seconds_after_finished: int = 3600
    retention_days: int = 7
    keep_results: bool = False


_FIELDS = {f.name: f.type for f in dataclasses.fields(Config)}


def _coerce(name: str, raw: str) -> Any:
    if name in ("ttl_seconds_after_finished", "retention_days"):
        return int(raw)
    if name == "keep_results":
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Config:
    path = DEFAULT_CONFIG_PATH if path is None else path
    env = os.environ if env is None else env

    values: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text())
        values.update({k: v for k, v in data.items() if k in _FIELDS})
    for name in _FIELDS:
        env_key = f"SK8S_{name.upper()}"
        if env_key in env:
            values[name] = _coerce(name, env[env_key])
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Config(**values)
