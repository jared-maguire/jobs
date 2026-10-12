"""Sandboxes: a throwaway namespace + an isolated config + a default budget, in one command.

``sk8s sandbox create`` is the isolation pattern every agent in the v2 build maintained by hand:
its own namespace (``sk8s-sbx-<name>``, RBAC applied by the normal profile machinery), its own
store prefix, its own config file (point ``SK8S_CONFIG`` at it), an optional budget, and a TTL so
it cleans itself up. Everything needed to find and delete the sandbox later lives on the namespace
itself (label ``sk8s/kind=sandbox`` plus annotations), so ``list`` / ``delete`` / ``reap`` need no
local state and no config file.

Whoever runs ``delete`` / ``clean`` / ``reap`` needs credentials for the sandbox's BASE store
(bucket access for s3/gs): removing the store prefix is part of deleting a sandbox, and a sandbox
whose prefix cannot be removed is kept, not half-deleted. The isolated config file is written to
the creator's working directory and its location is not recorded, so ``delete`` from that
directory removes it but a reap (or delete from elsewhere) leaves it behind: harmless, stale.

Reaping follows the same rules as workflow retention (``sk8s.cleaning``): an expired sandbox is
deleted only if it is older than a minimum age, has no active jobs, and has no running services;
anything that cannot be proven idle is kept.
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import random
import re
import string
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from kubernetes import client
from kubernetes.client import ApiException

from sk8s import profile as profile_mod
from sk8s.budget import Budget
from sk8s.cluster import job_phase
from sk8s.config import Config
from sk8s.errors import Sk8sError

log = logging.getLogger("sk8s.sandbox")

LABEL_KEY = "sk8s/kind"
LABEL_VALUE = "sandbox"
SELECTOR = f"{LABEL_KEY}={LABEL_VALUE}"
ANN_CREATED = "sk8s/created-at"
ANN_EXPIRES = "sk8s/expires-at"
ANN_STORE = "sk8s/store"                  # the sandbox's own store URI (what its config uses)
ANN_STORE_BASE = "sk8s/store-base"        # the store it was carved out of
ANN_STORE_PREFIX = "sk8s/store-prefix"    # the key prefix inside the base store
ANN_STORE_ENDPOINT = "sk8s/store-endpoint"
ANN_BUDGET = "sk8s/budget"

NAMESPACE_PREFIX = "sk8s-sbx-"
STORE_PREFIX = "sbx-"
DEFAULT_TTL_SECONDS = 24 * 3600.0
# Same floor as workflow anchors (sk8s.cleaning): never reap what was created moments ago.
MIN_AGE_SECONDS = 300.0
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_MAX_NAME = 63 - len(NAMESPACE_PREFIX)  # a namespace is a DNS label
_TTL = re.compile(r"(\d+(?:\.\d+)?)([smhd])")
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
MAX_TTL_SECONDS = 3650 * 86400.0  # ten years: past that a datetime would overflow, and it is no ttl


# --- names, ttl, store ---------------------------------------------------------------------------------


def validate_name(name: str) -> str:
    if not name or len(name) > _MAX_NAME or _NAME.fullmatch(name) is None:
        raise Sk8sError(
            f"invalid sandbox name {name!r}: lowercase letters, digits and '-' only, starting and "
            f"ending with a letter or digit, at most {_MAX_NAME} characters"
        )
    return name


def new_name(name: str | None) -> str:
    """``name`` validated, or a random 6-character one."""
    if name is not None:
        return validate_name(name)
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def namespace_for(name: str) -> str:
    return NAMESPACE_PREFIX + name


def name_of(name_or_namespace: str) -> str:
    """The sandbox name from either the bare name or its ``sk8s-sbx-`` namespace."""
    return name_or_namespace.removeprefix(NAMESPACE_PREFIX)


def parse_ttl(text: str) -> float:
    """'90s' / '30m' / '24h' / '7d' (a number may be fractional) -> seconds. A unit is required."""
    m = _TTL.fullmatch(text.strip().lower())
    seconds = float(m.group(1)) * _UNITS[m.group(2)] if m else 0.0
    if m is None or seconds <= 0:
        raise Sk8sError(
            f"invalid ttl {text!r}: a positive number and a unit, e.g. 90m, 24h, 7d (s/m/h/d)"
        )
    if seconds > MAX_TTL_SECONDS:
        raise Sk8sError(f"ttl {text!r} is too long: at most 3650d")
    return seconds


def sandbox_store(base: str, name: str) -> tuple[str, str]:
    """(the sandbox's store URI, its key prefix) carved out of the configured store ``base``.

    ``k8s`` is already namespace-scoped (blobs are ConfigMaps in the sandbox namespace), so it is
    kept as is; directory and bucket stores get a ``sbx-<name>`` prefix.
    """
    prefix = STORE_PREFIX + name
    if base == "k8s":
        return base, prefix
    if base.startswith(("local:", "s3://", "gs://")):
        return f"{base.rstrip('/')}/{prefix}", prefix
    raise Sk8sError(f"cannot derive a sandbox store from {base!r}: expected k8s, local:, s3:// or gs://")


# --- the record ----------------------------------------------------------------------------------------


def _when(text: str | None) -> datetime.datetime | None:
    if not text:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.timezone.utc)


@dataclass(frozen=True)
class Sandbox:
    """A sandbox as recorded on its namespace. ``created_at`` / ``expires_at`` are None when the
    annotation is missing or malformed (such a sandbox is never reaped)."""

    name: str
    namespace: str
    created_at: datetime.datetime | None
    expires_at: datetime.datetime | None
    store: str
    store_base: str
    store_prefix: str
    store_endpoint: str | None = None
    budget: Budget | None = None

    def expired(self, now: datetime.datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "namespace": self.namespace,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "store": self.store, "budget": self.budget.to_dict() if self.budget else None,
        }


def _is_sandbox(ns: Any) -> bool:
    return (ns.metadata.labels or {}).get(LABEL_KEY) == LABEL_VALUE


def _from_namespace(ns: Any) -> Sandbox:
    ann = ns.metadata.annotations or {}
    try:
        budget = Budget.coerce(ann.get(ANN_BUDGET))
    except (ValueError, TypeError):
        budget = None
    return Sandbox(
        name=name_of(ns.metadata.name), namespace=ns.metadata.name,
        created_at=_when(ann.get(ANN_CREATED)), expires_at=_when(ann.get(ANN_EXPIRES)),
        store=ann.get(ANN_STORE, ""), store_base=ann.get(ANN_STORE_BASE, ""),
        store_prefix=ann.get(ANN_STORE_PREFIX, ""), store_endpoint=ann.get(ANN_STORE_ENDPOINT) or None,
        budget=budget,
    )


def _read(core_api: Any, namespace: str) -> Any | None:
    try:
        return core_api.read_namespace(name=namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def list_sandboxes(core_api: Any | None = None) -> list[Sandbox]:
    """Every sandbox in the cluster (label ``sk8s/kind=sandbox`` on the namespace), by name."""
    if core_api is None:
        from sk8s import k8s

        core_api = k8s.core_api()
    items = core_api.list_namespace(label_selector=SELECTOR).items
    return sorted((_from_namespace(ns) for ns in items if _is_sandbox(ns)), key=lambda s: s.name)


# --- create --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Created:
    sandbox: Sandbox
    config_path: Path
    config: dict[str, Any]
    lines: list[str] = field(default_factory=list)

    @property
    def export_line(self) -> str:
        return f"export SK8S_CONFIG={self.config_path}"


def _config_data(cfg: Config, prof: profile_mod.ClusterProfile, budget: Budget | None) -> dict[str, Any]:
    """The sandbox's config file: this client's settings, repointed at the sandbox."""
    data: dict[str, Any] = {"namespace": prof.namespace, "store": prof.store}
    if cfg.store_endpoint:
        data["store_endpoint"] = cfg.store_endpoint
    if cfg.image:
        data["image"] = cfg.image
    if prof.image_prefix:
        data["image_prefix"] = prof.image_prefix
    if cfg.pull_policy != Config().pull_policy:
        data["pull_policy"] = cfg.pull_policy
    if prof.service_account != "sk8s":
        data["service_account"] = prof.service_account
    if prof.rwx_storage_class:
        data["rwx_storage_class"] = prof.rwx_storage_class
    if budget is not None:
        data["budget"] = budget.to_dict()
    if cfg.store_env:
        data["store_env"] = dict(cfg.store_env)
    return data


def write_config_file(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` as TOML, atomically, readable by the owner only (it may carry credentials)."""
    from sk8s.cli import render_toml  # lazy: the CLI imports this module

    text = render_toml(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sandbox-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create(
    name: str | None = None,
    *,
    cfg: Config,
    provider: str,
    provider_args: Mapping[str, Any] | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    budget_usd: float | None = None,
    assume_cpu: float | None = None,
    config_dir: Path | None = None,
    core_api: Any | None = None,
    rbac_api: Any | None = None,
    storage_api: Any | None = None,
    now: datetime.datetime | None = None,
) -> Created:
    """Create sandbox ``name`` (random if None): labelled namespace, profile apply, config file.

    ``cfg`` is the client's current configuration; the store (carved out under ``sbx-<name>``),
    endpoint, image settings and credentials are carried into the sandbox's own config file,
    written to ``<config_dir or cwd>/.sk8s-sandbox-<name>.toml``. ``provider`` (+ ``provider_args``
    for gke) goes through the same ``profile_for`` ``sk8s config`` uses. Refuses an existing
    namespace; if applying the profile fails the namespace is deleted again.
    """
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise Sk8sError(f"ttl must be positive and at most 3650d, got {ttl_seconds}s")
    if assume_cpu is not None and budget_usd is None:
        raise Sk8sError("--assume-cpu only means something with --budget-usd")
    budget = (
        Budget(max_estimated_usd=budget_usd, assumed_cpu_when_unset=assume_cpu)
        if budget_usd is not None else None
    )
    name = new_name(name)
    namespace = namespace_for(name)
    stamp = now or datetime.datetime.now(datetime.timezone.utc)
    store, prefix = sandbox_store(cfg.store, name)
    extra: dict[str, Any] = dict(provider_args or {})
    if provider != "gke":
        extra.setdefault("image_prefix", cfg.image_prefix)
        extra.setdefault("rwx_storage_class", cfg.rwx_storage_class if provider == "generic" else None)
    try:
        prof = profile_mod.profile_for(provider, namespace=namespace, store=store, **extra)
    except ValueError as e:
        raise Sk8sError(str(e)) from e
    if provider == "gke" and cfg.image_prefix:  # what `sk8s config` persisted wins
        prof = dataclasses.replace(prof, image_prefix=cfg.image_prefix)
    if core_api is None or rbac_api is None:
        from sk8s import k8s

        core_api = core_api if core_api is not None else k8s.core_api()
        rbac_api = rbac_api if rbac_api is not None else k8s.rbac_api()

    if _read(core_api, namespace) is not None:
        raise Sk8sError(
            f"namespace {namespace} already exists; pick another --name, or "
            f"'sk8s sandbox delete {name}' first"
        )
    annotations = {
        ANN_CREATED: stamp.isoformat(),
        ANN_EXPIRES: (stamp + datetime.timedelta(seconds=ttl_seconds)).isoformat(),
        ANN_STORE: store, ANN_STORE_BASE: cfg.store, ANN_STORE_PREFIX: prefix,
    }
    if cfg.store_endpoint:
        annotations[ANN_STORE_ENDPOINT] = cfg.store_endpoint
    if budget is not None:
        annotations[ANN_BUDGET] = budget.to_json()
    labels = {**profile_mod.MANAGED_BY, LABEL_KEY: LABEL_VALUE}
    # Born labelled: a crash between creating and labelling must not leave an anonymous namespace.
    try:
        core_api.create_namespace(body=client.V1Namespace(metadata=client.V1ObjectMeta(
            name=namespace, labels=labels, annotations=annotations)))
    except ApiException as e:
        if e.status == 409:
            raise Sk8sError(f"namespace {namespace} already exists; pick another --name") from e
        raise
    try:
        lines = profile_mod.apply(prof, core_api=core_api, rbac_api=rbac_api, storage_api=storage_api)
        data = _config_data(cfg, prof, budget)
        path = (config_dir or Path.cwd()) / f".sk8s-sandbox-{name}.toml"
        write_config_file(path, data)
    except BaseException:
        try:
            core_api.delete_namespace(name=namespace)
        except Exception:  # noqa: BLE001 — the original failure is what matters
            log.exception("could not roll back namespace %s", namespace)
        raise
    ns = _read(core_api, namespace)
    assert ns is not None
    return Created(_from_namespace(ns), path, data, lines)


# --- delete / reap -------------------------------------------------------------------------------------


def _clean_store(box: Sandbox, core_api: Any) -> str | None:
    """Delete the sandbox's key prefix from its base store; the line to report, or None if the
    store needs nothing (k8s: the namespace owns the blobs). Raises Sk8sError if it cannot."""
    if box.store_base == "k8s":
        return None
    from sk8s.store import get_store

    try:
        store = get_store(
            box.store_base, namespace=box.namespace, core_api=core_api, endpoint=box.store_endpoint
        )
        store.delete_prefix(box.store_prefix)
    except Exception as e:  # noqa: BLE001 — any backend failure: keep the namespace so this is retryable
        raise Sk8sError(
            f"could not delete the store prefix {box.store_prefix!r} of {box.store_base!r} "
            f"({type(e).__name__}: {e}); the sandbox namespace was kept so you can retry"
        ) from e
    return f"deleted store prefix {box.store_prefix} under {box.store_base}"


def _validate_record(box: Sandbox) -> None:
    """The store prefix is deleted from whatever the namespace's annotations say, so trust none of
    it blindly: a sandbox's prefix is exactly ``sbx-<name>`` and it must name a base store. A
    mismatch (hand-edited or damaged annotations) deletes nothing."""
    if box.store_prefix != STORE_PREFIX + box.name or not box.store_base:
        raise Sk8sError(
            f"sandbox {box.name!r} has an inconsistent store record (prefix {box.store_prefix!r}, "
            f"base {box.store_base!r}; expected prefix {STORE_PREFIX + box.name!r} and a base "
            "store); refusing to delete anything from it on that basis. Fix the namespace "
            f"annotations {ANN_STORE_PREFIX} / {ANN_STORE_BASE}, or delete the namespace by hand"
        )


def _remove(box: Sandbox, core_api: Any) -> list[str]:
    _validate_record(box)
    lines = []
    line = _clean_store(box, core_api)
    if line:
        lines.append(line)
    try:
        core_api.delete_namespace(name=box.namespace)
    except ApiException as e:
        if e.status != 404:
            raise
    lines.append(f"deleted namespace {box.namespace}")
    return lines


def delete(name: str, *, core_api: Any | None = None) -> list[str]:
    """Delete sandbox ``name``: its store prefix, then its namespace (which cascades everything in
    it). Unconditional, like ``sk8s kill``: live jobs die with it. Never deletes a namespace that
    is not labelled as a sandbox. If the store prefix cannot be deleted the namespace is KEPT."""
    if core_api is None:
        from sk8s import k8s

        core_api = k8s.core_api()
    name = name_of(name)
    ns = _read(core_api, namespace_for(name)) if _NAME.fullmatch(name) else None
    if ns is None:
        raise Sk8sError(f"no such sandbox {name!r} (see 'sk8s sandbox list')")
    if not _is_sandbox(ns):
        raise Sk8sError(
            f"namespace {ns.metadata.name} is not an sk8s sandbox (it lacks the {SELECTOR} label); "
            "refusing to delete it"
        )
    return _remove(_from_namespace(ns), core_api)


@dataclass
class ReapReport:
    deleted: list[str] = field(default_factory=list)
    kept: list[dict[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.deleted or self.kept)

    def as_dict(self) -> dict[str, Any]:
        return {"deleted": list(self.deleted), "kept": [dict(k) for k in self.kept]}


def reap(
    *,
    core_api: Any | None = None,
    batch_api: Any | None = None,
    apps_api: Any | None = None,
    now: datetime.datetime | None = None,
) -> ReapReport:
    """Delete expired sandboxes that are provably idle; report what was kept and why.

    Kept: not yet expired; younger than the minimum age; malformed or missing expiry stamps (never
    deleted on a guess); jobs or services that could not be listed; any active job; any running
    service (Deployment); a store prefix that could not be deleted.
    """
    from sk8s import k8s

    core_api = core_api if core_api is not None else k8s.core_api()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    report = ReapReport()

    def keep(box: Sandbox, reason: str) -> None:
        report.kept.append({"sandbox": box.name, "reason": reason})

    for box in list_sandboxes(core_api):
        if box.expires_at is None or box.created_at is None:
            keep(box, "its expiry or creation stamp is missing or malformed (never deleted on a guess)")
            continue
        if not box.expired(now):
            keep(box, f"not expired: expires {box.expires_at.isoformat()}")
            continue
        if (now - box.created_at).total_seconds() < MIN_AGE_SECONDS:
            keep(box, f"younger than the {int(MIN_AGE_SECONDS)}s minimum age no sweep goes below")
            continue
        try:
            batch_api = batch_api if batch_api is not None else k8s.batch_api()
            apps_api = apps_api if apps_api is not None else k8s.apps_api()
            jobs = batch_api.list_namespaced_job(namespace=box.namespace).items
            services = apps_api.list_namespaced_deployment(namespace=box.namespace).items
        except Exception:  # noqa: BLE001 — cannot prove it idle: keep it
            log.warning("keeping sandbox %s: could not list its jobs/services", box.name, exc_info=True)
            keep(box, "expired, but its jobs or services could not be listed to prove it idle")
            continue
        if any(job_phase(j) == "active" for j in jobs):
            keep(box, "expired, but it still has active jobs")
            continue
        if services:
            keep(box, "expired, but it still has running services (Deployments)")
            continue
        try:
            _remove(box, core_api)
        except Exception as e:  # noqa: BLE001
            log.warning("keeping sandbox %s: %s", box.name, e)
            keep(box, f"expired and idle, but could not be deleted: {e}")
            continue
        log.info("reaped expired sandbox %s", box.name)
        report.deleted.append(box.name)
    return report
