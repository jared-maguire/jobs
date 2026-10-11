import dataclasses

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from sk8s import profile as prof
from sk8s.profile import ClusterProfile, apply, detect_provider, plan, profile_for
from tests.fakes import FakeCoreV1, FakeRbacV1

VERBS = ("get", "list", "watch", "create", "delete", "patch")


def _profile(**kw: object) -> ClusterProfile:
    return profile_for("local", namespace="team-a", store="k8s", **kw)  # type: ignore[arg-type]


def _apply(p: ClusterProfile, core: FakeCoreV1, rbac: FakeRbacV1) -> list[str]:
    return apply(p, core_api=core, rbac_api=rbac)  # type: ignore[arg-type]


# --- profile exactness -------------------------------------------------------------------------


def test_profile_is_frozen_with_defaults() -> None:
    p = _profile()
    assert p.namespace == "team-a" and p.store == "k8s"
    assert p.service_account == "sk8s"
    assert p.rwx_storage_class is None and p.image_prefix == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.namespace = "x"  # type: ignore[misc]


def test_role_rules_are_the_exact_table() -> None:
    flat = {
        (group, resource): set(rule.verbs)
        for rule in _profile().rules
        for group in rule.api_groups
        for resource in rule.resources
    }
    expected = {
        ("batch", "jobs"),
        ("", "pods"),
        ("", "pods/log"),
        ("", "configmaps"),
        ("", "persistentvolumeclaims"),
        ("apps", "deployments"),
        ("", "services"),
    }
    assert set(flat) == expected
    assert all(v == set(VERBS) for v in flat.values())
    assert prof.VERBS == VERBS


# --- adapters ----------------------------------------------------------------------------------


def test_profile_for_generic_takes_provider_parameters() -> None:
    p = profile_for(
        "generic", namespace="n", store="s3://b/p", image_prefix="reg.io/me", rwx_storage_class="nfs"
    )
    assert (p.image_prefix, p.rwx_storage_class, p.store) == ("reg.io/me", "nfs", "s3://b/p")
    assert p.rules == _profile().rules  # apply logic and rules are shared across providers


def test_profile_for_local_has_no_rwx_class() -> None:
    assert _profile().rwx_storage_class is None
    with pytest.raises(ValueError, match="RWX"):
        profile_for("local", namespace="n", store="k8s", rwx_storage_class="nfs")


@pytest.mark.parametrize("provider", ["eks", "nope"])
def test_unknown_provider_names_eks_as_tabled_but_not_gke(provider: str) -> None:
    with pytest.raises(ValueError) as ei:
        profile_for(provider, namespace="n", store="k8s")
    msg = str(ei.value)
    assert provider in msg and "eks" in msg and "tabled" in msg
    assert "gke" in msg  # listed as supported...
    assert "gke" not in msg.split("tabled")[0].split("(")[-1]  # ...and not as tabled


def test_gke_is_not_in_the_tabled_list() -> None:
    assert prof.TABLED_PROVIDERS == ("eks",)


def _gke(**kw: object) -> ClusterProfile:
    args: dict[str, object] = dict(project="proj", region="us-central1", namespace="n", store="k8s")
    args.update(kw)
    return profile_for("gke", **args)  # type: ignore[arg-type]


def test_gke_adapter_parameters_are_exact() -> None:
    p = _gke(store="gs://b/p", gsa="sk8s-worker@proj.iam.gserviceaccount.com")
    assert p.image_prefix == "us-central1-docker.pkg.dev/proj/sk8s/"
    assert p.rwx_storage_class == "standard-rwx"
    assert p.sa_annotations == {
        "iam.gke.io/gcp-service-account": "sk8s-worker@proj.iam.gserviceaccount.com"
    }
    assert (p.namespace, p.store) == ("n", "gs://b/p")
    assert p.rules == _profile().rules  # apply logic and rules are shared across providers


def test_gke_adapter_repo_override_and_no_gsa_means_no_annotation() -> None:
    p = _gke(repo="images")
    assert p.image_prefix == "us-central1-docker.pkg.dev/proj/images/"
    assert p.sa_annotations == {}


@pytest.mark.parametrize("missing", ["project", "region"])
def test_gke_requires_project_and_region(missing: str) -> None:
    with pytest.raises(ValueError, match="project and region"):
        _gke(**{missing: None})


def test_gke_rejects_overriding_what_it_derives() -> None:
    with pytest.raises(ValueError, match="derives"):
        _gke(image_prefix="reg/")
    with pytest.raises(ValueError, match="derives"):
        _gke(rwx_storage_class="nfs")


@pytest.mark.parametrize("provider", ["local", "generic"])
@pytest.mark.parametrize("kw", [{"project": "p"}, {"region": "r"}, {"gsa": "g"}, {"repo": "x"}])
def test_gke_parameters_are_rejected_for_other_providers(provider: str, kw: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="GKE parameters"):
        profile_for(provider, namespace="n", store="k8s", **kw)


def test_sa_annotations_default_is_per_instance_and_profile_stays_frozen() -> None:
    a, b = _profile(), _profile()
    assert a.sa_annotations == {} and a.sa_annotations is not b.sa_annotations


@pytest.mark.parametrize(
    "context,expected",
    [
        ("minikube", "local"),
        ("docker-desktop", "local"),
        ("gke_proj_us-central1_c", "gke"),
        ("gke_my-proj_us-central1-a_my_cluster", "gke"),
        ("gke_proj_us-central1", "generic"),
        ("gke_proj__c", "generic"),
        ("notgke_proj_us-central1_c", "generic"),
        ("arn:aws:eks:us-west-2:1:cluster/c", "generic"),
        ("kind-kind", "generic"),
        ("", "generic"),
    ],
)
def test_detect_provider_from_context_name(context: str, expected: str) -> None:
    assert detect_provider(context) == expected


def test_detect_provider_defaults_to_generic_without_kubeconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("no kubeconfig")

    monkeypatch.setattr(prof.config, "list_kube_config_contexts", boom)
    assert detect_provider() == "generic"


def test_detect_provider_reads_active_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prof.config, "list_kube_config_contexts", lambda: ([], {"name": "minikube"})
    )
    assert detect_provider() == "local"


def test_gke_context_parts_and_region() -> None:
    assert prof.gke_context_parts("gke_jared-genome-analysis_us-central1_autopilot-cluster-1") == (
        "jared-genome-analysis", "us-central1", "autopilot-cluster-1",
    )
    assert prof.gke_context_parts("minikube") is None
    assert prof.gke_region("us-central1") == "us-central1"
    assert prof.gke_region("us-central1-a") == "us-central1"


# --- plan --------------------------------------------------------------------------------------


def test_plan_lists_every_object_without_touching_a_cluster() -> None:
    lines = plan(_profile())
    text = "\n".join(lines)
    assert lines[0] == "ensure Namespace team-a"
    for kind in ("ServiceAccount sk8s", "Role sk8s", "RoleBinding sk8s"):
        assert kind in text
    assert "team-a" in text and "jobs" in text and "pods/log" in text and "patch" in text
    assert "ensure ServiceAccount sk8s in team-a" in lines  # ensure-only, never replaced
    assert "create-or-replace ServiceAccount" not in text


def test_plan_lists_sa_annotations_when_the_profile_has_them() -> None:
    lines = plan(_gke(gsa="g@proj.iam.gserviceaccount.com"))
    assert (
        "annotate ServiceAccount sk8s in n: "
        "iam.gke.io/gcp-service-account=g@proj.iam.gserviceaccount.com"
    ) in lines
    assert not any("annotate" in line for line in plan(_profile()))


# --- apply -------------------------------------------------------------------------------------


def test_apply_creates_all_four_kinds() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    out = _apply(_profile(), core, rbac)
    assert "team-a" in core.namespaces
    assert ("team-a", "sk8s") in core.service_accounts
    role = rbac.roles[("team-a", "sk8s")]
    assert {
        (g, r, v)
        for rule in role.rules
        for g in rule.api_groups
        for r in rule.resources
        for v in rule.verbs
    } >= {("batch", "jobs", "patch"), ("", "pods/log", "get"), ("apps", "deployments", "delete")}
    binding = rbac.role_bindings[("team-a", "sk8s")]
    assert binding.role_ref.kind == "Role" and binding.role_ref.name == "sk8s"
    assert [(s.kind, s.name, s.namespace) for s in binding.subjects] == [
        ("ServiceAccount", "sk8s", "team-a")
    ]
    assert len(out) == 4 and all("created" in line for line in out)


def test_second_apply_performs_no_writes() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)
    core_before, rbac_before = list(core.actions), list(rbac.actions)
    objects_before = (dict(core.service_accounts), dict(rbac.roles), dict(rbac.role_bindings))
    out = _apply(_profile(), core, rbac)
    assert core.actions == core_before and rbac.actions == rbac_before  # zero creates/replaces
    assert (dict(core.service_accounts), dict(rbac.roles), dict(rbac.role_bindings)) == (
        objects_before
    )
    assert not any("created" in line or "replaced" in line for line in out)


def test_apply_ensures_existing_namespace_without_creating() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    core.namespaces["team-a"] = client.V1Namespace(metadata=client.V1ObjectMeta(name="team-a"))
    _apply(_profile(), core, rbac)
    assert ("create", "Namespace", "team-a") not in core.actions


def test_apply_converges_a_role_missing_a_verb() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)
    stale = rbac.roles[("team-a", "sk8s")]
    for rule in stale.rules:
        rule.verbs = [v for v in rule.verbs if v != "patch"]
    out = _apply(_profile(), core, rbac)
    assert ("replace", "Role", "sk8s") in rbac.actions
    assert all("patch" in r.verbs for r in rbac.roles[("team-a", "sk8s")].rules)
    assert any(line.startswith("Role") and "replaced" in line for line in out)
    rbac.actions.clear()
    _apply(_profile(), core, rbac)
    assert rbac.actions == []  # and then it stays converged


def test_apply_converges_drifted_binding_subjects() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)
    rbac.role_bindings[("team-a", "sk8s")].subjects = []
    _apply(_profile(), core, rbac)
    assert ("replace", "RoleBinding", "sk8s") in rbac.actions
    assert len(rbac.role_bindings[("team-a", "sk8s")].subjects) == 1


def test_apply_recreates_binding_when_role_ref_changed() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)
    rbac.role_bindings[("team-a", "sk8s")].role_ref.name = "other"
    _apply(_profile(), core, rbac)
    assert ("delete", "RoleBinding", "sk8s") in rbac.actions
    assert rbac.role_bindings[("team-a", "sk8s")].role_ref.name == "sk8s"


def test_create_409_race_falls_back_to_replace() -> None:
    """Object appears between our read (404) and create (409): replace instead of failing."""

    class Racy(FakeRbacV1):
        def read_namespaced_role(self, name: str, namespace: str) -> object:
            if not self.actions:  # first read: not there yet; someone else creates it right after
                self.roles[(namespace, name)] = client.V1Role(rules=[])
                raise ApiException(status=404, reason="Not Found")
            return super().read_namespaced_role(name, namespace)

    core, rbac = FakeCoreV1(), Racy()
    _apply(_profile(), core, rbac)
    assert ("create", "Role", "sk8s") in rbac.actions
    assert ("replace", "Role", "sk8s") in rbac.actions
    assert rbac.roles[("team-a", "sk8s")].rules


def test_apply_propagates_non_404_read_errors() -> None:
    class Forbidden(FakeRbacV1):
        def read_namespaced_role(self, name: str, namespace: str) -> object:
            raise ApiException(status=403, reason="Forbidden")

    with pytest.raises(ApiException) as ei:
        _apply(_profile(), FakeCoreV1(), Forbidden())
    assert ei.value.status == 403


def test_apply_uses_custom_service_account_name() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    p = dataclasses.replace(_profile(), service_account="runner")
    _apply(p, core, rbac)
    assert ("team-a", "runner") in core.service_accounts
    assert rbac.role_bindings[("team-a", "runner")].subjects[0].name == "runner"


# --- rule normalization (drift detection) ------------------------------------------------------


def _shuffled_role(role: client.V1Role) -> client.V1Role:
    return client.V1Role(
        metadata=role.metadata,
        rules=[
            client.V1PolicyRule(
                api_groups=list(reversed(r.api_groups or [])),
                resources=list(reversed(r.resources or [])),
                verbs=list(reversed(r.verbs or [])),
            )
            for r in reversed(role.rules or [])
        ],
    )


def test_reordered_but_equal_role_is_not_drift() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)
    rbac.roles[("team-a", "sk8s")] = _shuffled_role(rbac.roles[("team-a", "sk8s")])
    rbac.actions.clear()
    out = _apply(_profile(), core, rbac)
    assert rbac.actions == []  # zero writes
    assert "Role sk8s: unchanged" in out


def _add_verb(role: client.V1Role) -> None:
    assert role.rules
    role.rules[0].verbs.append("escalate")


def _add_rule(role: client.V1Role) -> None:
    assert role.rules is not None
    role.rules.append(client.V1PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get"]))


def test_extra_verb_or_extra_rule_replaces_the_role() -> None:
    for mutate in (_add_verb, _add_rule):
        core, rbac = FakeCoreV1(), FakeRbacV1()
        _apply(_profile(), core, rbac)
        mutate(rbac.roles[("team-a", "sk8s")])
        rbac.actions.clear()
        _apply(_profile(), core, rbac)
        assert ("replace", "Role", "sk8s") in rbac.actions
        assert prof.normalized_rules(rbac.roles[("team-a", "sk8s")]) == prof.normalized_rules(
            prof.desired_role(_profile())
        )


def test_name_narrowed_rule_registers_as_drift() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)
    rbac.roles[("team-a", "sk8s")].rules[0].resource_names = ["only-this"]
    rbac.actions.clear()
    _apply(_profile(), core, rbac)
    assert ("replace", "Role", "sk8s") in rbac.actions
    assert not rbac.roles[("team-a", "sk8s")].rules[0].resource_names


def test_missing_permissions_is_subset_based() -> None:
    want = prof.desired_role(_profile())
    assert prof.missing_permissions(want, want) == []
    wild = client.V1Role(
        rules=[client.V1PolicyRule(api_groups=["*"], resources=["*"], verbs=["*"])]
    )
    assert prof.missing_permissions(want, wild) == []
    narrowed = client.V1Role(
        rules=[
            client.V1PolicyRule(
                api_groups=["batch"], resources=["jobs"], verbs=list(VERBS), resource_names=["x"]
            )
        ]
    )
    assert ("batch", "jobs", "get") in prof.missing_permissions(want, narrowed)


# --- ServiceAccount annotations (Workload Identity) ---------------------------------------------

GSA = "sk8s-worker@proj.iam.gserviceaccount.com"
WI = "iam.gke.io/gcp-service-account"


def _gke_apply(core: FakeCoreV1, rbac: FakeRbacV1, gsa: str | None = GSA) -> list[str]:
    return _apply(_gke(namespace="team-a", gsa=gsa), core, rbac)


def test_apply_creates_the_service_account_with_the_annotation() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    out = _gke_apply(core, rbac)
    assert core.service_accounts[("team-a", "sk8s")].metadata.annotations == {WI: GSA}
    assert "ServiceAccount sk8s: created" in out
    assert ("patch", "ServiceAccount", "sk8s") not in core.actions  # annotated at creation


def test_apply_without_annotations_creates_a_bare_service_account() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)
    assert not core.service_accounts[("team-a", "sk8s")].metadata.annotations


def test_apply_annotation_is_idempotent() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _gke_apply(core, rbac)
    before = list(core.actions)
    out = _gke_apply(core, rbac)
    assert core.actions == before
    assert "ServiceAccount sk8s: unchanged" in out


def test_apply_patches_a_missing_annotation_without_recreating_the_service_account() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _apply(_profile(), core, rbac)  # pre-existing bare SA (e.g. from an earlier plain config)
    sa = core.service_accounts[("team-a", "sk8s")]
    sa.secrets = [client.V1ObjectReference(name="tok")]
    core.actions.clear()
    out = _gke_apply(core, rbac)
    assert core.actions == [("patch", "ServiceAccount", "sk8s")]  # no create/replace/delete
    assert core.service_accounts[("team-a", "sk8s")] is sa and sa.secrets  # same object, secrets kept
    assert sa.metadata.annotations == {WI: GSA}
    assert "ServiceAccount sk8s: annotated" in out
    core.actions.clear()
    _gke_apply(core, rbac)
    assert core.actions == []  # converged


def test_apply_patches_a_drifted_annotation_and_keeps_foreign_ones() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _gke_apply(core, rbac, gsa="old@proj.iam.gserviceaccount.com")
    core.service_accounts[("team-a", "sk8s")].metadata.annotations["other/ann"] = "keep"
    core.actions.clear()
    _gke_apply(core, rbac)
    assert core.actions == [("patch", "ServiceAccount", "sk8s")]
    assert core.service_accounts[("team-a", "sk8s")].metadata.annotations == {
        WI: GSA, "other/ann": "keep",
    }


def test_apply_without_gsa_leaves_an_existing_annotation_alone() -> None:
    core, rbac = FakeCoreV1(), FakeRbacV1()
    _gke_apply(core, rbac)
    core.actions.clear()
    _gke_apply(core, rbac, gsa=None)
    assert core.actions == []
    assert core.service_accounts[("team-a", "sk8s")].metadata.annotations == {WI: GSA}


def test_service_account_create_409_race_still_converges_annotations() -> None:
    class Racy(FakeCoreV1):
        def read_namespaced_service_account(self, name: str, namespace: str) -> object:
            if ("create", "ServiceAccount", name) not in self.actions:
                # first read 404s; someone creates it bare right after
                self.service_accounts[(namespace, name)] = client.V1ServiceAccount(
                    metadata=client.V1ObjectMeta(name=name)
                )
                raise ApiException(status=404, reason="Not Found")
            return super().read_namespaced_service_account(name, namespace)

    core = Racy()
    _gke_apply(core, FakeRbacV1())
    assert ("create", "ServiceAccount", "sk8s") in core.actions
    assert ("patch", "ServiceAccount", "sk8s") in core.actions
    assert core.service_accounts[("team-a", "sk8s")].metadata.annotations == {WI: GSA}
