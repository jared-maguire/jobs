"""Layered, frozen configuration: defaults < toml file < SK8S_* env < kwargs.

The toml file is ``~/.config/sk8s/config.toml`` unless ``SK8S_CONFIG`` names another path.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from sk8s.budget import Budget

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sk8s" / "config.toml"
# ``SK8S_CONFIG=<path>`` relocates the file (both reading and ``sk8s config`` writing); it changes
# WHICH file is the file layer, not the layering.
CONFIG_PATH_ENV = "SK8S_CONFIG"
# The dedicated namespace sk8s uses when nothing names one. Never "default".
DEFAULT_NAMESPACE = "sk8s"


@dataclass(frozen=True)
class Config:
    namespace: str | None = None        # None -> kube-context namespace if it sets one, else "sk8s"
    image: str | None = None            # None -> official image for this version
    image_prefix: str = ""
    pull_policy: str = "IfNotPresent"
    service_account: str = "sk8s"
    store: str = "k8s"                  # "k8s" | "local:<dir>" | "s3://..." | "gs://..."
    store_endpoint: str | None = None   # S3-compatible endpoint URL; env SK8S_S3_ENDPOINT wins
    # StorageClass for ReadWriteMany volumes; written by ``sk8s config`` from the cluster profile.
    # None -> RWX volumes need an explicit ``storage_class=`` (create_volume says so).
    rwx_storage_class: str | None = None
    ttl_seconds_after_finished: int = 3600
    # How long a read-only mount waits for a running rw job to finish before it can be served
    # (a clone must never be taken under a live writer); VolumeWaitTimeout when it runs out.
    volume_wait_seconds: float = 300.0
    # How long a pod may sit Pending behind a LIVE holder of its ReadWriteOnce volume before the
    # job fails with VolumeAttachBlocked (and is deleted). The trade-off: a second rw job queued
    # behind a long writer legitimately waits for it (hand-over takes ~15s after the holder ends),
    # so this must exceed the queueing you want to allow; but the wait has no timeout of its own in
    # Kubernetes, so it must be finite (a forgotten fan-out would hang forever). 0 fails at once.
    volume_attach_grace_seconds: float = 300.0
    # How long sk8s's own read-only clone may stay Pending with ProvisioningFailed events before the
    # job fails with VolumeProvisioningFailed. The provisioner retries on its own (quota freed, a
    # rate-limited source disk); this is how long that is given.
    volume_provision_grace_seconds: float = 300.0
    retention_days: int = 7
    keep_results: bool = False
    # Memoize every run/submit/map: look up (function, args, input artifacts, image) in the store
    # before submitting and return the recorded result on a hit. Opt-in (SPEC decision A1).
    memo: bool = False
    # Extra env vars exported to worker pods (e.g. AWS_* creds for an S3-compatible store).
    # Config file / kwargs only: values are never read from SK8S_* env vars.
    store_env: dict[str, str] = field(default_factory=dict)
    # Client-side guardrails (see sk8s.budget): None = unlimited. A mapping is coerced to Budget.
    # Travels to detached drivers as SK8S_BUDGET (JSON), so nested submissions enforce it too.
    budget: Budget | None = None

    def __post_init__(self) -> None:
        if self.budget is not None and not isinstance(self.budget, Budget):
            object.__setattr__(self, "budget", Budget.coerce(self.budget))


_FIELDS = {f.name: f.type for f in dataclasses.fields(Config)}


def _coerce(name: str, raw: str) -> Any:
    if name in ("ttl_seconds_after_finished", "retention_days"):
        return int(raw)
    if name in ("volume_wait_seconds", "volume_attach_grace_seconds", "volume_provision_grace_seconds"):
        return float(raw)
    if name == "budget":
        return Budget.coerce(raw)
    if name in ("keep_results", "memo"):
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """The config file location: ``SK8S_CONFIG`` if set and non-empty, else the default path."""
    env = os.environ if env is None else env
    raw = env.get(CONFIG_PATH_ENV, "")
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Config:
    env = os.environ if env is None else env
    path = config_path(env) if path is None else path

    values: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        values.update({k: v for k, v in data.items() if k in _FIELDS})
    for name in _FIELDS:
        if name == "store_env":
            continue
        env_key = f"SK8S_{name.upper()}"
        if env_key in env:
            try:
                values[name] = _coerce(name, env[env_key])
            except ValueError as e:
                raise ValueError(f"{env_key}: {e}") from None
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Config(**values)
