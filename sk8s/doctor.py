"""``sk8s doctor``: independent, read-mostly health checks for a ClusterProfile.

Every check runs regardless of the others' outcome. When the cluster itself is unreachable the
checks that need it report not-ok with a "cluster unreachable" hint instead of crashing.
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from kubernetes import client
from kubernetes.client import ApiException

from sk8s import k8s
from sk8s.errors import (
    BlobNotFound, ImageNotConfigured, ImagePullError, JobTimeout, PodFailed, RemoteTraceback,
)
from sk8s.profile import ClusterProfile, desired_role, missing_permissions
from sk8s.store.base import BlobStore

PROBE_PREFIX = "doctor/"
_PROBE_PAYLOAD = b"sk8s doctor probe"
_UNREACHABLE = (
    "cluster unreachable: fix 'cluster reachable' first "
    "(check your kubeconfig/current context and that the cluster is running)"
)

WORKER_PROBE_NAME = "worker identity (in-cluster store probe)"
WORKER_PROBE_TIMEOUT = 600.0  # generous: an autoscaling cluster may add a node before the pod runs

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


def probe_store_in_pod(store_url: str) -> str:
    """The function the worker-identity check runs INSIDE a pod: a store round-trip using the
    pod's own credentials (for ``gs://``, its Workload Identity). Returns ``"ok"`` or raises."""
    from sk8s.store import get_store  # lazy: runs in the pod, where only the store is needed

    _probe_store(get_store(store_url))
    return "ok"


# The submission seam: run ``fn(store_url)`` in a pod of the cluster and return its result (or
# raise what the job raised). Unit tests stub it; the default goes through a real Cluster.
ProbeSubmit = Callable[[Callable[[str], str], ClusterProfile], str]


def submit_probe_job(fn: Callable[[str], str], profile: ClusterProfile) -> str:
    from sk8s.cluster import Cluster  # lazy: building a Cluster needs a kube config
    from sk8s.config import load_config

    config = dataclasses.replace(
        load_config(),
        namespace=profile.namespace,
        store=profile.store,
        service_account=profile.service_account,
        image_prefix=profile.image_prefix,
    )
    cluster = Cluster(config)
    try:
        return str(cluster.run(fn, profile.store, timeout=WORKER_PROBE_TIMEOUT, name_prefix="doctor"))
    finally:
        cluster.close()


def _worker_probe_hint(profile: ClusterProfile, exc: BaseException) -> str:
    ns, sa = profile.namespace, profile.service_account
    detail = f"probe error: {_describe(exc)}"
    if isinstance(exc, ImageNotConfigured):
        return (
            "no worker image is configured, so the probe pod was never created: build one and "
            "set SK8S_IMAGE (or `image` in your config); `sk8s config` writes an image_prefix "
            f"only on GKE ({detail})"
        )
    if isinstance(exc, ImagePullError):
        return (
            f"the probe pod could not pull its image ({detail}); push the worker image to "
            "your registry (sk8s image build/push) and check that nodes may read it"
        )
    if isinstance(exc, JobTimeout):
        return (
            f"the probe pod did not finish in {exc.timeout:.0f}s ({detail}); on an autoscaling "
            f"cluster the first pod can take minutes, so re-run; if it persists, inspect the pod "
            f"in namespace {ns} (kubectl get pods -n {ns})"
        )
    pod_side = isinstance(exc, PodFailed) or isinstance(exc.__cause__, RemoteTraceback)
    if not pod_side:
        # Pod-side errors come back re-raised with a RemoteTraceback cause; anything else never
        # got a verdict from the pod (building the client, writing the payload, creating the job).
        return (
            "the probe failed before reaching the pod (client-side store/cluster error: fix the "
            f"'store writable' check and cluster access first, then re-run); {detail}"
        )
    return (
        f"a pod using ServiceAccount {sa!r} cannot use store {profile.store!r}: this is "
        "usually Workload Identity misconfiguration. Check (1) the ServiceAccount carries "
        "annotation iam.gke.io/gcp-service-account=<gsa> (sk8s config --provider gke --gsa ...); "
        f"(2) that GSA has roles/iam.workloadIdentityUser for member "
        f"serviceAccount:<project>.svc.id.goog[{ns}/{sa}]; (3) the GSA has "
        "roles/storage.objectAdmin on the bucket; (4) Workload Identity is enabled on the "
        "cluster and node pool. 'sk8s config --provider gke ... --apply-cloud' sets up 1-3. "
        f"({detail})"
    )


def doctor(
    profile: ClusterProfile,
    *,
    core_api: client.CoreV1Api | None = None,
    rbac_api: client.RbacAuthorizationV1Api | None = None,
    batch_api: client.BatchV1Api | None = None,
    store: BlobStore | None = None,
    storage_api: client.StorageV1Api | None = None,
    store_endpoint: str | None = None,
    submit_probe: ProbeSubmit | None = None,
) -> list[Check]:
    """Run every check for ``profile``; never raises, one failure never hides the rest.

    ``batch_api`` is accepted for symmetry with the other injected clients; no current check needs
    it. ``store_endpoint`` is the S3-compatible endpoint used when the store has to be built here
    (an injected ``store`` is used as-is). ``storage_api`` backs the storage-class check, which only runs when the profile names one.
    ``submit_probe`` is the seam for the worker-identity check (see ``ProbeSubmit``); that check
    exists only when the cluster is reachable AND the store is ``gs://``, where the client's own
    credentials say nothing about the pods'.
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
        if not reachable and profile.store == "k8s":
            return _fail("store writable", _UNREACHABLE)  # would only time out through put/get/delete
        try:
            s = store
            if s is None:
                from sk8s.store import get_store

                s = get_store(
                    profile.store, namespace=ns, core_api=core, endpoint=store_endpoint
                )
            _probe_store(s)
            return _ok("store writable")
        except StoreProbeError as e:
            hint = f"store {profile.store!r}: {e}; check its credentials/permissions or run: sk8s config --store"
        except Exception as e:
            hint = f"store {profile.store!r} is unusable ({_describe(e)}); check the store setting"
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

    # 7. worker identity: the pods' credentials, not the client's, must reach a gs:// store.
    if reachable and profile.store.startswith("gs://"):
        prerequisites = [c for c in checks if c.name in ("namespace exists", "service account exists")]

        def worker_check() -> Check:
            if not all(c.ok for c in prerequisites):
                return _fail(
                    WORKER_PROBE_NAME,
                    f"not attempted: namespace {ns!r} / ServiceAccount {sa!r} are not ready; "
                    "fix those checks first (run: sk8s config)",
                )
            submit = submit_probe if submit_probe is not None else submit_probe_job
            try:
                result = submit(probe_store_in_pod, profile)
            except Exception as e:
                return _fail(WORKER_PROBE_NAME, _worker_probe_hint(profile, e))
            if result != "ok":  # the pod answered, just not with a pass: a pod-side verdict
                bad = RuntimeError(f"probe returned {result!r}")
                bad.__cause__ = RemoteTraceback("(no traceback: the probe returned normally)")
                return _fail(WORKER_PROBE_NAME, _worker_probe_hint(profile, bad))
            return _ok(WORKER_PROBE_NAME)

        checks.append(guarded(WORKER_PROBE_NAME, worker_check))
    return checks
