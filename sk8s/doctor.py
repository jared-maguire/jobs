"""``sk8s doctor``: independent, read-mostly health checks for a ClusterProfile.

Every check runs regardless of the others' outcome. When the cluster itself is unreachable the
checks that need it report not-ok with a "cluster unreachable" hint instead of crashing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from kubernetes import client
from kubernetes.client import ApiException

from sk8s import k8s
from sk8s.errors import BlobNotFound
from sk8s.profile import ClusterProfile, desired_role, missing_permissions
from sk8s.store.base import BlobStore

PROBE_PREFIX = "doctor/"
_PROBE_PAYLOAD = b"sk8s doctor probe"
_UNREACHABLE = (
    "cluster unreachable: fix 'cluster reachable' first "
    "(check your kubeconfig/current context and that the cluster is running)"
)

T = TypeVar("T")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    fix_hint: str = ""


def _describe(exc: BaseException) -> str:
    if isinstance(exc, ApiException):
        return f"HTTP {exc.status} {exc.reason}"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _status(exc: BaseException) -> int | None:
    status = exc.status if isinstance(exc, ApiException) else None
    return status if isinstance(status, int) else None


def _ok(name: str) -> Check:
    return Check(name, True)


def _fail(name: str, hint: str) -> Check:
    return Check(name, False, hint)


def _resolve(given: T | None, factory: Callable[[], T]) -> tuple[T | None, Exception | None]:
    """The injected client, else a freshly built one; a failure to build is returned, not raised."""
    if given is not None:
        return given, None
    try:
        return factory(), None
    except Exception as e:
        return None, e


class StoreProbeError(Exception):
    """One step of the store round-trip failed; the message names the step."""


def _probe_store(store: BlobStore) -> None:
    """put -> get -> compare -> delete -> confirm gone. The probe is removed even on failure."""
    key = f"{PROBE_PREFIX}probe-{uuid.uuid4().hex[:12]}"
    uri = store.uri_for(key)
    deleted = False
    try:
        try:
            uri = store.put(key, _PROBE_PAYLOAD)
        except Exception as e:
            raise StoreProbeError(f"write failed ({_describe(e)})") from e
        try:
            got = store.get(uri)
        except Exception as e:
            raise StoreProbeError(
                f"read-back failed after a write ({_describe(e)}); the store may be read-only"
            ) from e
        if got != _PROBE_PAYLOAD:
            raise StoreProbeError("read-back returned different bytes than were written")
        try:
            store.delete(uri)
            deleted = True
        except Exception as e:
            raise StoreProbeError(f"delete failed ({_describe(e)})") from e
        try:
            store.get(uri)
        except BlobNotFound:
            return
        except Exception as e:
            raise StoreProbeError(f"could not confirm delete ({_describe(e)})") from e
        raise StoreProbeError("delete did not remove the probe (it is still readable)")
    finally:
        if not deleted:
            try:  # best effort; never mask the original failure
                store.delete(uri)
            except Exception:
                pass


def doctor(
    profile: ClusterProfile,
    *,
    core_api: client.CoreV1Api | None = None,
    rbac_api: client.RbacAuthorizationV1Api | None = None,
    batch_api: client.BatchV1Api | None = None,
    store: BlobStore | None = None,
    storage_api: client.StorageV1Api | None = None,
) -> list[Check]:
    """Run every check for ``profile``; never raises, one failure never hides the rest.

    ``batch_api`` is accepted for symmetry with the other injected clients; no current check needs
    it. ``storage_api`` backs the storage-class check, which only runs when the profile names one.
    """
    ns, sa = profile.namespace, profile.service_account
    core, core_err = _resolve(core_api, k8s.core_api)
    rbac, rbac_err = _resolve(rbac_api, k8s.rbac_api)
    checks: list[Check] = []

    # 1. cluster reachable (everything else that touches the API depends on this).
    name = "cluster reachable"
    reachable = False
    if core is None:
        err = core_err or RuntimeError("no client")
        checks.append(
            _fail(name, f"could not build a Kubernetes client ({_describe(err)}); "
                  "check your kubeconfig and current context")
        )
    else:
        try:
            core.get_api_resources()
            reachable = True
            checks.append(_ok(name))
        except Exception as e:
            checks.append(
                _fail(name, f"cannot reach the cluster ({_describe(e)}); check your kubeconfig, "
                      "current context, and that the cluster is running")
            )

    def guarded(check_name: str, fn: Callable[[], Check]) -> Check:
        if not reachable:
            return _fail(check_name, _UNREACHABLE)
        try:
            return fn()
        except Exception as e:
            return _fail(check_name, f"check could not complete ({_describe(e)})")

    # 2. namespace exists
    def namespace_check() -> Check:
        assert core is not None
        try:
            core.read_namespace(name=ns)
            return _ok("namespace exists")
        except ApiException as e:
            if _status(e) == 404:
                return _fail("namespace exists", f"Namespace {ns!r} not found; run: sk8s config")
            raise

    checks.append(guarded("namespace exists", namespace_check))

    # 3. service account exists
    def sa_check() -> Check:
        assert core is not None
        try:
            core.read_namespaced_service_account(name=sa, namespace=ns)
            return _ok("service account exists")
        except ApiException as e:
            if _status(e) == 404:
                return _fail(
                    "service account exists",
                    f"ServiceAccount {sa!r} not found in {ns!r}; run: sk8s config",
                )
            raise

    checks.append(guarded("service account exists", sa_check))

    # 4. role permissions (subset: every required (group, resource, verb) must be granted)
    def role_check() -> Check:
        if rbac is None:
            raise rbac_err or RuntimeError("no RBAC client")
        try:
            have = rbac.read_namespaced_role(name=profile.role_name, namespace=ns)
        except ApiException as e:
            if _status(e) == 404:
                return _fail(
                    "role permissions",
                    f"Role {profile.role_name!r} not found in {ns!r}; run: sk8s config",
                )
            raise
        missing = missing_permissions(desired_role(profile), have)
        if not missing:
            return _ok("role permissions")
        by_verb: dict[str, list[str]] = {}
        for group, resource, verb in missing:
            by_verb.setdefault(verb, []).append(f"{resource}[{group or 'core'}]")
        detail = "; ".join(f"{v} on {', '.join(rs)}" for v, rs in sorted(by_verb.items()))
        return _fail(
            "role permissions",
            f"Role {profile.role_name!r} in {ns!r} lacks: {detail}. "
            "Run: sk8s config (re-applies the Role), or grant those verbs manually",
        )

    checks.append(guarded("role permissions", role_check))

    # 5. store writable. Only a cluster-backed store depends on the cluster being reachable.
    def store_check() -> Check:
        try:
            s = store
            if s is None:
                from sk8s.store import get_store

                s = get_store(profile.store, namespace=ns, core_api=core)
            _probe_store(s)
            return _ok("store writable")
        except StoreProbeError as e:
            hint = f"store {profile.store!r}: {e}; check its credentials/permissions or run: sk8s config --store"
        except Exception as e:
            hint = f"store {profile.store!r} is unusable ({_describe(e)}); check the store setting"
        if not reachable and profile.store == "k8s":
            hint = _UNREACHABLE
        return _fail("store writable", hint)

    checks.append(store_check())

    # 6. storage class exists (only when the profile names one)
    wanted = profile.rwx_storage_class
    if wanted is not None:

        def storage_check() -> Check:
            storage, storage_err = _resolve(storage_api, k8s.storage_api)
            if storage is None:
                raise storage_err or RuntimeError("no storage client")
            try:
                storage.read_storage_class(name=wanted)
                return _ok("storage class exists")
            except ApiException as e:
                if _status(e) == 404:
                    return _fail(
                        "storage class exists",
                        f"StorageClass {wanted!r} not found; install a provisioner that "
                        "provides it, or correct rwx_storage_class in the profile",
                    )
                raise

        checks.append(guarded("storage class exists", storage_check))
    return checks
