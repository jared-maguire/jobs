from typing import Any

import pytest
from kubernetes.client import ApiException

import sk8s
from sk8s import volumes
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import Sk8sError
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1

NS = "ns1"


class Harness:
    def __init__(self, tmp_path, rwx_storage_class: str | None = None) -> None:  # type: ignore[no-untyped-def]
        self.core = FakeCoreV1()
        cfg = Config(
            namespace=NS, image="sk8s:dev", store=f"local:{tmp_path}",
            rwx_storage_class=rwx_storage_class,
        )
        self.cluster = Cluster(
            cfg, batch_api=FakeBatchV1(), core_api=self.core,
            store=LocalStore(tmp_path), watcher_events=lambda: iter(()),
        )

    def pvc(self, name: str) -> Any:
        return self.core.pvcs[(NS, name)]


@pytest.fixture
def h(tmp_path):  # type: ignore[no-untyped-def]
    harness = Harness(tmp_path)
    yield harness
    harness.cluster.close()


def test_create_returns_name_and_builds_pvc(h: Harness) -> None:
    name = sk8s.create_volume("5Gi", name="data", cluster=h.cluster)
    assert name == "data"
    pvc = h.pvc("data")
    assert pvc.metadata.namespace == NS
    assert pvc.spec.resources.requests == {"storage": "5Gi"}
    assert pvc.spec.access_modes == ["ReadWriteOnce"]
    assert pvc.spec.storage_class_name is None


def test_size_and_access_modes_pass_through_verbatim(h: Harness) -> None:
    name = sk8s.create_volume(
        "1500M", access_modes=["ReadWriteMany", "ReadOnlyMany"], storage_class="x",
        cluster=h.cluster,
    )
    pvc = h.pvc(name)
    assert pvc.spec.resources.requests["storage"] == "1500M"
    assert pvc.spec.access_modes == ["ReadWriteMany", "ReadOnlyMany"]


def test_storage_class_only_when_given(h: Harness) -> None:
    name = sk8s.create_volume("1Gi", storage_class="fast", cluster=h.cluster)
    assert h.pvc(name).spec.storage_class_name == "fast"


# --- storage-class resolution (the v1 pattern) ---------------------------------------------------


@pytest.fixture
def mapped(tmp_path):  # type: ignore[no-untyped-def]
    harness = Harness(tmp_path, rwx_storage_class="standard-rwx")
    yield harness
    harness.cluster.close()


def test_rwx_resolves_to_the_configured_class(mapped: Harness) -> None:
    for modes in (["ReadWriteMany"], ["ReadWriteOnce", "ReadWriteMany"], "ReadWriteMany"):
        name = sk8s.create_volume("1Ti", access_modes=modes, cluster=mapped.cluster)
        assert mapped.pvc(name).spec.storage_class_name == "standard-rwx"


def test_rwx_without_a_mapped_class_is_a_typed_error_with_a_config_hint(h: Harness) -> None:
    with pytest.raises(Sk8sError) as e:
        sk8s.create_volume("1Ti", access_modes=["ReadWriteMany"], cluster=h.cluster)
    assert "sk8s config" in str(e.value) and "storage_class=" in str(e.value)
    assert not h.core.pvcs and not h.core.create_calls  # nothing half-made, no Pending PVC


def test_an_explicit_storage_class_always_wins(h: Harness, mapped: Harness) -> None:
    for harness in (h, mapped):  # unmapped (would otherwise raise) and mapped
        name = sk8s.create_volume(
            "1Ti", access_modes=["ReadWriteMany"], storage_class="mine", cluster=harness.cluster
        )
        assert harness.pvc(name).spec.storage_class_name == "mine"


@pytest.mark.parametrize(
    "modes",
    [["ReadWriteOnce"], ["ReadOnlyMany"], ["ReadWriteOncePod"], ["ReadWriteOnce", "ReadOnlyMany"]],
)
def test_non_rwx_modes_leave_the_class_to_the_cluster_default(
    mapped: Harness, modes: list[str]
) -> None:
    name = sk8s.create_volume("1Gi", access_modes=modes, cluster=mapped.cluster)
    assert mapped.pvc(name).spec.storage_class_name is None


def test_plain_rwo_volume_is_byte_identical_to_before_class_resolution(
    h: Harness, mapped: Harness
) -> None:
    """RF2: the PVC built for a default volume is the pre-branch body, mapped class or not."""
    from kubernetes import client

    for harness in (h, mapped):
        name = sk8s.create_volume("5Gi", name="data", keep=True, cluster=harness.cluster)
        body = client.ApiClient().sanitize_for_serialization(harness.pvc(name))
        body["metadata"].pop("uid", None)  # assigned by the fake API server, not by sk8s
        assert body == {
            "metadata": {
                "name": "data",
                "namespace": NS,
                "labels": {"app.kubernetes.io/managed-by": "sk8s", "sk8s/kind": "volume-keep"},
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "5Gi"}},
            },
        }


def test_generated_name_is_dns1123(h: Harness) -> None:
    a = sk8s.create_volume("1Gi", cluster=h.cluster)
    b = sk8s.create_volume("1Gi", cluster=h.cluster)
    assert a != b
    for n in (a, b):
        assert volumes._NAME_RE.match(n) and n.startswith("vol-") and len(n) == len("vol-") + 6


@pytest.mark.parametrize("bad", ["", "Upper", "-lead", "trail-", "has_underscore", "a" * 64, "dot.ted"])
def test_invalid_name_rejected_before_any_call(h: Harness, bad: str) -> None:
    with pytest.raises(ValueError):
        sk8s.create_volume("1Gi", name=bad, cluster=h.cluster)
    assert not h.core.pvcs and not h.core.create_calls


def test_empty_size_rejected(h: Harness) -> None:
    with pytest.raises(ValueError):
        sk8s.create_volume("", cluster=h.cluster)


def test_existing_name_raises_sk8serror(h: Harness) -> None:
    sk8s.create_volume("1Gi", name="data", cluster=h.cluster)
    with pytest.raises(Sk8sError, match="already exists"):
        sk8s.create_volume("1Gi", name="data", cluster=h.cluster)


def test_owned_volume_has_anchor_owner_and_workflow_label(h: Harness) -> None:
    name = sk8s.create_volume("1Gi", cluster=h.cluster)
    pvc = h.pvc(name)
    anchor = h.cluster._anchor
    assert anchor is not None  # created from birth, as submit would
    assert (NS, anchor.metadata.name) in h.core.configmaps
    refs = pvc.metadata.owner_references
    assert len(refs) == 1
    assert refs[0].kind == "ConfigMap" and refs[0].name == anchor.metadata.name
    assert refs[0].uid == anchor.metadata.uid
    labels = pvc.metadata.labels
    assert labels["app.kubernetes.io/managed-by"] == "sk8s"
    assert labels["sk8s/workflow-id"] == h.cluster.workflow_id
    assert labels["sk8s/kind"] == "volume"


def test_kept_volume_has_no_owner_and_no_workflow_label(h: Harness) -> None:
    name = sk8s.create_volume("1Gi", keep=True, cluster=h.cluster)
    pvc = h.pvc(name)
    assert not pvc.metadata.owner_references
    labels = pvc.metadata.labels
    assert "sk8s/workflow-id" not in labels
    assert labels["app.kubernetes.io/managed-by"] == "sk8s"
    assert labels["sk8s/kind"] == "volume-keep"
    assert h.cluster._anchor is None  # keep=True does not even create an anchor


def test_delete_volume_removes_and_is_idempotent(h: Harness) -> None:
    name = sk8s.create_volume("1Gi", cluster=h.cluster)
    sk8s.delete_volume(name, cluster=h.cluster)
    assert (NS, name) not in h.core.pvcs
    sk8s.delete_volume(name, cluster=h.cluster)  # already gone: no error
    sk8s.delete_volume("never-existed", cluster=h.cluster)


def test_delete_uses_default_propagation(h: Harness) -> None:
    name = sk8s.create_volume("1Gi", cluster=h.cluster)
    sk8s.delete_volume(name, cluster=h.cluster)
    assert h.core.pvc_delete_policies[name] is None


def test_delete_works_on_kept_volume(h: Harness) -> None:
    name = sk8s.create_volume("1Gi", keep=True, cluster=h.cluster)
    sk8s.delete_volume(name, cluster=h.cluster)
    assert (NS, name) not in h.core.pvcs


def test_delete_refuses_pvc_not_managed_by_sk8s(h: Harness) -> None:
    from kubernetes import client

    h.core.pvcs[(NS, "theirs")] = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name="theirs", namespace=NS, labels={"app": "x"})
    )
    with pytest.raises(Sk8sError, match="not managed by sk8s"):
        sk8s.delete_volume("theirs", cluster=h.cluster)
    assert (NS, "theirs") in h.core.pvcs


def test_delete_propagates_non_404_errors(h: Harness) -> None:
    class Boom(FakeCoreV1):
        def read_namespaced_persistent_volume_claim(self, name: str, namespace: str) -> Any:
            raise ApiException(status=500, reason="boom")

    h.cluster._core = Boom()
    with pytest.raises(ApiException):
        sk8s.delete_volume("x", cluster=h.cluster)


def test_scratch_volume_creates_yields_and_deletes(h: Harness) -> None:
    with sk8s.scratch_volume("2Gi", cluster=h.cluster) as name:
        assert h.pvc(name).spec.resources.requests["storage"] == "2Gi"
        assert h.pvc(name).metadata.owner_references
    assert (NS, name) not in h.core.pvcs


def test_scratch_volume_deletes_on_exception(h: Harness) -> None:
    seen: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        with sk8s.scratch_volume("2Gi", cluster=h.cluster) as name:
            seen.append(name)
            raise RuntimeError("boom")
    assert (NS, seen[0]) not in h.core.pvcs


def test_scratch_volume_deletes_on_keyboard_interrupt(h: Harness) -> None:
    seen: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        with sk8s.scratch_volume("2Gi", cluster=h.cluster) as name:
            seen.append(name)
            raise KeyboardInterrupt
    assert (NS, seen[0]) not in h.core.pvcs


def test_scratch_volume_body_already_deleted_is_fine(h: Harness) -> None:
    with sk8s.scratch_volume("1Gi", cluster=h.cluster) as name:
        sk8s.delete_volume(name, cluster=h.cluster)
    assert not h.core.pvcs


def test_created_name_mounts_read_only_at_mnt_name_by_default(h: Harness) -> None:
    name = sk8s.create_volume("1Gi", cluster=h.cluster)
    assert volumes.parse_mounts(name) == [volumes.Mount(name, "ro", f"/mnt/{name}")]


def test_default_cluster_used_when_none_given(h: Harness) -> None:
    sk8s.configure(cluster=h.cluster)
    try:
        name = sk8s.create_volume("1Gi")
        assert (NS, name) in h.core.pvcs
        sk8s.delete_volume(name)
        assert not h.core.pvcs
    finally:
        sk8s.configure()
