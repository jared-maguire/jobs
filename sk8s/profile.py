"""Declarative cluster profile: what a namespace needs so sk8s can run there, plus plan/apply.

Provider adapters supply PARAMETERS only (``profile_for``); ``plan`` and ``apply`` are shared.
Adding a cloud provider later means registering one more adapter in ``_ADAPTERS`` (and removing
it from ``TABLED_PROVIDERS``); nothing in plan/apply changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from kubernetes import client, config
from kubernetes.client import ApiException

from sk8s import k8s

VERBS: tuple[str, ...] = ("get", "list", "watch", "create", "delete", "patch")
LOCAL_CONTEXTS: frozenset[str] = frozenset({"minikube", "docker-desktop"})
TABLED_PROVIDERS: tuple[str, ...] = ("gke", "eks")
MANAGED_BY = {"app.kubernetes.io/managed-by": "sk8s"}


@dataclass(frozen=True)
class PolicyRule:
    api_groups: tuple[str, ...]
    resources: tuple[str, ...]
    verbs: tuple[str, ...]


# The exact rule table: verbs get/list/watch/create/delete/patch on these resources.
ROLE_RULES: tuple[PolicyRule, ...] = (
    PolicyRule(
        ("",),
        ("pods", "pods/log", "configmaps", "persistentvolumeclaims", "services"),
        VERBS,
    ),
    PolicyRule(("batch",), ("jobs",), VERBS),
    PolicyRule(("apps",), ("deployments",), VERBS),
)


@dataclass(frozen=True)
class ClusterProfile:
    namespace: str
    store: str
    service_account: str = "sk8s"
    rules: tuple[PolicyRule, ...] = ROLE_RULES
    rwx_storage_class: str | None = None  # None for local
    image_prefix: str = ""

    @property
    def role_name(self) -> str:
        """Role and RoleBinding share the service account's name."""
        return self.service_account


# --- provider adapters -------------------------------------------------------------------------

Adapter = Callable[[str, str, str, str | None], ClusterProfile]


def _local(
    namespace: str, store: str, image_prefix: str, rwx_storage_class: str | None
) -> ClusterProfile:
    if rwx_storage_class is not None:
        raise ValueError("provider 'local' has no RWX storage class; use provider 'generic'")
    return ClusterProfile(namespace=namespace, store=store, image_prefix=image_prefix)


def _generic(
    namespace: str, store: str, image_prefix: str, rwx_storage_class: str | None
) -> ClusterProfile:
    return ClusterProfile(
        namespace=namespace,
        store=store,
        image_prefix=image_prefix,
        rwx_storage_class=rwx_storage_class,
    )


_ADAPTERS: dict[str, Adapter] = {"local": _local, "generic": _generic}


def profile_for(
    provider: str,
    *,
    namespace: str,
    store: str,
    image_prefix: str = "",
    rwx_storage_class: str | None = None,
) -> ClusterProfile:
    try:
        adapter = _ADAPTERS[provider]
    except KeyError:
        raise ValueError(
            f"unknown provider {provider!r}; supported: {', '.join(_ADAPTERS)} "
            f"({', '.join(TABLED_PROVIDERS)} are tabled)"
        ) from None
    return adapter(namespace, store, image_prefix, rwx_storage_class)


def detect_provider(context: str | None = None) -> str:
    """'local' for minikube/docker-desktop kube contexts, else 'generic'.

    With no argument, inspects the active kube context (any failure to read it means 'generic').
    """
    if context is None:
        try:
            _, active = config.list_kube_config_contexts()  # type: ignore[no-untyped-call]
            context = active.get("name")
        except Exception:
            return "generic"
    return "local" if context in LOCAL_CONTEXTS else "generic"


# --- plan --------------------------------------------------------------------------------------


def plan(profile: ClusterProfile) -> list[str]:
    """Human-readable actions apply() would converge on. Pure: touches no cluster."""
    ns, sa = profile.namespace, profile.service_account
    lines = [
        f"ensure Namespace {ns}",
        f"create-or-replace ServiceAccount {sa} in {ns}",
    ]
    for r in profile.rules:
        groups = ",".join(g or "core" for g in r.api_groups)
        lines.append(
            f"create-or-replace Role {profile.role_name} in {ns}: "
            f"{'/'.join(r.verbs)} on {','.join(r.resources)} [{groups}]"
        )
    lines.append(
        f"create-or-replace RoleBinding {profile.role_name} in {ns}: "
        f"Role {profile.role_name} -> ServiceAccount {sa}"
    )
    return lines


# --- apply -------------------------------------------------------------------------------------


def _status(exc: ApiException) -> int | None:
    status = exc.status
    return status if isinstance(status, int) else None


def _meta(name: str, namespace: str | None = None) -> client.V1ObjectMeta:
    return client.V1ObjectMeta(name=name, namespace=namespace, labels=dict(MANAGED_BY))


RuleKey = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]


def normalized_rules(role: client.V1Role) -> frozenset[RuleKey]:
    """Order-insensitive form of a Role's rules: (api_groups, resources, verbs, resource_names).

    Each field is sorted, so reordering rules/resources/verbs is not drift, while a changed
    verb, an extra rule, or a name-narrowed rule (``resource_names``) is. Shared by apply
    (drift detection) and doctor (permission checks).
    """
    return frozenset(
        (
            tuple(sorted(r.api_groups or ())),
            tuple(sorted(r.resources or ())),
            tuple(sorted(r.verbs or ())),
            tuple(sorted(r.resource_names or ())),
        )
        for r in (role.rules or [])
    )


def missing_permissions(want: client.V1Role, have: client.V1Role) -> list[tuple[str, str, str]]:
    """(api_group, resource, verb) triples ``want`` requires that ``have`` does not grant.

    Subset semantics: extra grants in ``have`` are fine, differently-grouped rules are fine, and
    ``*`` wildcards in ``have`` count. A rule narrowed by ``resource_names`` grants nothing for
    an unrestricted requirement, so it is ignored here.
    """

    def expand(role: client.V1Role) -> set[tuple[str, str, str]]:
        return {
            (g, res, v)
            for groups, resources, verbs, names in normalized_rules(role)
            if not names
            for g in groups
            for res in resources
            for v in verbs
        }

    granted = expand(have)

    def covered(g: str, res: str, v: str) -> bool:
        return any(
            (gg, rr, vv) in granted
            for gg in (g, "*")
            for rr in (res, "*")
            for vv in (v, "*")
        )

    return sorted(t for t in expand(want) if not covered(*t))


def desired_role(profile: ClusterProfile) -> client.V1Role:
    return client.V1Role(
        metadata=_meta(profile.role_name, profile.namespace),
        rules=[
            client.V1PolicyRule(
                api_groups=list(r.api_groups), resources=list(r.resources), verbs=list(r.verbs)
            )
            for r in profile.rules
        ],
    )


def _desired_binding(profile: ClusterProfile) -> client.V1RoleBinding:
    return client.V1RoleBinding(
        metadata=_meta(profile.role_name, profile.namespace),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io", kind="Role", name=profile.role_name
        ),
        subjects=[
            client.RbacV1Subject(
                kind="ServiceAccount", name=profile.service_account, namespace=profile.namespace
            )
        ],
    )


def _binding_subjects(b: client.V1RoleBinding) -> set[tuple[str, str, str]]:
    return {(s.kind, s.name, s.namespace or "") for s in (b.subjects or [])}


def _same_role_ref(a: client.V1RoleBinding, b: client.V1RoleBinding) -> bool:
    ra, rb = a.role_ref, b.role_ref
    return (ra.kind, ra.name, ra.api_group) == (rb.kind, rb.name, rb.api_group)


def apply(
    profile: ClusterProfile,
    *,
    core_api: client.CoreV1Api | None = None,
    rbac_api: client.RbacAuthorizationV1Api | None = None,
) -> list[str]:
    """Converge the cluster on ``profile``; returns one line per object saying what happened.

    Each object is read first: missing -> create; present but drifted -> replace; present and
    equal -> untouched. So a second apply performs no writes at all.
    """
    core = core_api if core_api is not None else k8s.core_api()
    rbac = rbac_api if rbac_api is not None else k8s.rbac_api()
    ns, sa, role = profile.namespace, profile.service_account, profile.role_name
    out: list[str] = []

    # Namespace: ensure only (never replaced; it may carry user labels/quotas).
    try:
        core.read_namespace(name=ns)
        out.append(f"Namespace {ns}: exists")
    except ApiException as e:
        if _status(e) != 404:
            raise
        try:
            core.create_namespace(body=client.V1Namespace(metadata=_meta(ns)))
            out.append(f"Namespace {ns}: created")
        except ApiException as e2:
            if _status(e2) != 409:
                raise
            out.append(f"Namespace {ns}: exists")

    # ServiceAccount: nothing declarative beyond its existence (replacing would drop its secrets).
    try:
        core.read_namespaced_service_account(name=sa, namespace=ns)
        out.append(f"ServiceAccount {sa}: unchanged")
    except ApiException as e:
        if _status(e) != 404:
            raise
        try:
            core.create_namespaced_service_account(
                namespace=ns,
                body=client.V1ServiceAccount(metadata=_meta(sa, ns)),
            )
            out.append(f"ServiceAccount {sa}: created")
        except ApiException as e2:
            if _status(e2) != 409:
                raise
            out.append(f"ServiceAccount {sa}: unchanged")

    # Role: replace when the rule set has drifted (e.g. a verb was removed).
    want_role = desired_role(profile)
    try:
        have_role = rbac.read_namespaced_role(name=role, namespace=ns)
    except ApiException as e:
        if _status(e) != 404:
            raise
        have_role = None
    if have_role is None:
        try:
            rbac.create_namespaced_role(namespace=ns, body=want_role)
            out.append(f"Role {role}: created")
        except ApiException as e:
            if _status(e) != 409:
                raise
            rbac.replace_namespaced_role(name=role, namespace=ns, body=want_role)
            out.append(f"Role {role}: replaced")
    elif normalized_rules(have_role) != normalized_rules(want_role):
        rbac.replace_namespaced_role(name=role, namespace=ns, body=want_role)
        out.append(f"Role {role}: replaced")
    else:
        out.append(f"Role {role}: unchanged")

    # RoleBinding: subjects are replaceable; roleRef is immutable, so a changed one is recreated.
    want_b = _desired_binding(profile)
    try:
        have_b = rbac.read_namespaced_role_binding(name=role, namespace=ns)
    except ApiException as e:
        if _status(e) != 404:
            raise
        have_b = None
    if have_b is None:
        try:
            rbac.create_namespaced_role_binding(namespace=ns, body=want_b)
            out.append(f"RoleBinding {role}: created")
        except ApiException as e:
            if _status(e) != 409:
                raise
            rbac.replace_namespaced_role_binding(name=role, namespace=ns, body=want_b)
            out.append(f"RoleBinding {role}: replaced")
    elif not _same_role_ref(have_b, want_b):
        rbac.delete_namespaced_role_binding(name=role, namespace=ns)
        rbac.create_namespaced_role_binding(namespace=ns, body=want_b)
        out.append(f"RoleBinding {role}: recreated (roleRef changed)")
    elif _binding_subjects(have_b) != _binding_subjects(want_b):
        rbac.replace_namespaced_role_binding(name=role, namespace=ns, body=want_b)
        out.append(f"RoleBinding {role}: replaced")
    else:
        out.append(f"RoleBinding {role}: unchanged")
    return out
