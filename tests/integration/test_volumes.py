"""Volumes integration suite: create/delete, anchor cascade, keep=True, scratch_volume,
per-mount access modes (read-only by default) and the hidden ReadOnlyMany clone.

Needs the worker image built from THIS branch, tagged ``sk8s-dev-volumes:latest``::

    minikube image build -t sk8s-dev-volumes:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_volumes.py -v

Override the tag with ``SK8S_VOLUMES_TEST_IMAGE``. Needs the default StorageClass
(minikube's ``standard``) and ``persistentvolumeclaims`` in the sk8s Role. Jobs run
one after another, so ReadWriteOnce re-use is fine on a single-node cluster.
"""
import errno
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from kubernetes.client import ApiException

from sk8s import create_volume, delete_volume, scratch_volume
from sk8s import clones
from sk8s.cluster import Cluster
from sk8s.config import load_config
from tests.integration.conftest import requires_cluster, suite_namespace, suite_pull_policy

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

IMAGE = os.environ.get("SK8S_VOLUMES_TEST_IMAGE", "sk8s-dev-volumes:latest")
TTL = 10  # ttl_seconds_after_finished for the suite's clusters
# A PVC delete blocks on the pvc-protection finalizer until every pod that mounts it is
# *deleted*; Succeeded pods count. Job pods vanish only when the TTL controller reaps
# the job (TTL seconds after it finishes, plus controller lag), so the wait must
# comfortably exceed TTL: 3x leaves generous margin for that lag.
GONE_TIMEOUT = 3 * TTL


def _new_cluster() -> Cluster:
    # Legacy default-namespace RBAC (dev-setup.md); pinned so we don't follow the "sk8s" default.
    return Cluster(
        load_config(namespace=suite_namespace(), image=IMAGE, pull_policy=suite_pull_policy(), ttl_seconds_after_finished=TTL)
    )


@pytest.fixture
def vol_cluster() -> Iterator[Cluster]:
    c = _new_cluster()
    yield c
    c.close(delete_workflow=True)


def _pvc_exists(c: Cluster, name: str) -> bool:
    try:
        c.core.read_namespaced_persistent_volume_claim(name=name, namespace=c.config.namespace)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    return True


def _pvc_alive(c: Cluster, name: str) -> bool:
    """True only if the PVC exists, is NOT Terminating, and has no owner (kept volume)."""
    try:
        pvc = c.core.read_namespaced_persistent_volume_claim(name=name, namespace=c.config.namespace)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    return pvc.metadata.deletion_timestamp is None and not pvc.metadata.owner_references


def _poll(cond: Callable[[], Any], timeout: float, what: str, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = cond()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last={last!r})")


def _diagnose(c: Cluster, name: str) -> str:
    ns = c.config.namespace
    try:
        pvc = c.core.read_namespaced_persistent_volume_claim(name=name, namespace=ns)
        state = f"deletionTimestamp={pvc.metadata.deletion_timestamp} finalizers={pvc.metadata.finalizers}"
    except ApiException as e:
        state = f"pvc read -> {e.status}"
    users = [
        f"{p.metadata.name}:{p.status.phase}"
        for p in c.core.list_namespaced_pod(ns).items
        if any(
            v.persistent_volume_claim and v.persistent_volume_claim.claim_name == name
            for v in (p.spec.volumes or [])
        )
    ]
    return f"{state} pods_mounting={users}"


def _wait_pvc_gone(c: Cluster, name: str) -> None:
    timeline: list[str] = []

    def gone() -> bool:
        if not _pvc_exists(c, name):
            return True
        state = _diagnose(c, name)
        if not timeline or timeline[-1].split(" ", 1)[1] != state:
            timeline.append(f"t+{time.monotonic() - start:.1f}s {state}")
        return False

    start = time.monotonic()
    try:
        _poll(gone, GONE_TIMEOUT, f"PVC {name} to be deleted")
    except AssertionError as e:
        raise AssertionError(f"{e}; timeline: {timeline}") from None


def _rw(vol: str) -> dict[str, dict[str, str]]:
    """Mounts are read-only by default; a job that writes must say so."""
    return {vol: {"access_mode": "rw"}}


def _write(path: str, text: str) -> Callable[[], str]:
    # Built from a nested function so cloudpickle ships it by value (the worker
    # image does not contain the ``tests`` package).
    def write() -> str:
        import pathlib

        pathlib.Path(path).write_text(text)
        return "written"

    return write


def _read(path: str) -> Callable[[], str]:
    def read() -> str:
        import pathlib

        return pathlib.Path(path).read_text()

    return read


def test_roundtrip(vol_cluster: Cluster) -> None:
    vol = create_volume("100Mi", cluster=vol_cluster)
    assert _pvc_exists(vol_cluster, vol)
    try:
        path = f"/mnt/{vol}/hello.txt"
        assert vol_cluster.run(_write(path, "hello volume"), volumes=_rw(vol), timeout=240) == "written"
        assert vol_cluster.run(_read(path), volumes=vol, timeout=240) == "hello volume"
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _wait_pvc_gone(vol_cluster, vol)


def test_cascade_with_workflow(vol_cluster: Cluster) -> None:
    vol = create_volume("100Mi", cluster=vol_cluster)  # creates the anchor
    assert _pvc_exists(vol_cluster, vol)
    vol_cluster.close(delete_workflow=True)
    _wait_pvc_gone(vol_cluster, vol)


def test_kept_volume_survives_cascade(vol_cluster: Cluster) -> None:
    vol = create_volume("100Mi", keep=True, cluster=vol_cluster)
    try:
        # An anchor exists for this workflow once a job has run.
        path = f"/mnt/{vol}/keep.txt"
        assert vol_cluster.run(_write(path, "kept"), volumes=_rw(vol), timeout=240) == "written"
        vol_cluster.close(delete_workflow=True)  # anchor cascade

        # Survives a stable 5s window (a cascade would have removed it by now).
        end = time.monotonic() + 5
        while time.monotonic() < end:
            assert _pvc_alive(vol_cluster, vol), "kept volume was removed (or is Terminating/owned) after the anchor cascade"
            time.sleep(0.5)

        # ...and its data is still readable from a different workflow.
        other = _new_cluster()
        try:
            assert other.run(_read(path), volumes=vol, timeout=240) == "kept"
        finally:
            other.close(delete_workflow=True)
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _wait_pvc_gone(vol_cluster, vol)


def test_scratch_volume(vol_cluster: Cluster) -> None:
    with scratch_volume("100Mi", cluster=vol_cluster) as vol:
        assert _pvc_exists(vol_cluster, vol)
        path = f"/mnt/{vol}/scratch.txt"
        assert vol_cluster.run(_write(path, "scratch"), volumes=_rw(vol), timeout=240) == "written"
        assert vol_cluster.run(_read(path), volumes=vol, timeout=240) == "scratch"
    _wait_pvc_gone(vol_cluster, vol)


# --- RWX (GKE Filestore), opt-in: it is the one expensive thing the suites can create -------------
#
# SK8S_GKE_RWX=1 runs it. Basic-tier Filestore has a 1 TiB minimum (~$0.2-0.3/hour while the
# instance exists), so the volume is created late and deleted immediately, and the test waits
# for the PVC (and so the instance) to be gone. The storage class defaults to GKE's
# ``standard-rwx``; override with SK8S_GKE_RWX_CLASS.

_RWX = os.environ.get("SK8S_GKE_RWX") == "1"


@pytest.mark.skipif(not _RWX, reason="set SK8S_GKE_RWX=1 (creates a Filestore instance, 1 TiB minimum)")
@pytest.mark.timeout(2400)
def test_rwx_volume_shared_between_jobs(vol_cluster: Cluster) -> None:
    sc = os.environ.get("SK8S_GKE_RWX_CLASS", "standard-rwx")
    vol = create_volume("1Ti", access_modes=("ReadWriteMany",), storage_class=sc, cluster=vol_cluster)
    try:
        pvc = vol_cluster.core.read_namespaced_persistent_volume_claim(
            name=vol, namespace=vol_cluster.config.namespace
        )
        assert pvc.spec.access_modes == ["ReadWriteMany"]
        # Two jobs at once on the same volume: ReadWriteMany is what lets them share it.
        w1 = vol_cluster.submit(_write(f"/mnt/{vol}/one.txt", "one"), volumes=_rw(vol))
        w2 = vol_cluster.submit(_write(f"/mnt/{vol}/two.txt", "two"), volumes=_rw(vol))
        assert w1.result(timeout=1500) == "written"  # first pod pays Filestore provisioning
        assert w2.result(timeout=300) == "written"
        assert vol_cluster.run(_read(f"/mnt/{vol}/one.txt"), volumes=vol, timeout=300) == "one"
        assert vol_cluster.run(_read(f"/mnt/{vol}/two.txt"), volumes=vol, timeout=300) == "two"
    finally:
        delete_volume(vol, cluster=vol_cluster)
    # PVC deletion waits for the TTL reaper to drop the job pods, then the CSI driver deletes
    # the Filestore instance (minutes); verify with `gcloud filestore instances list`.
    _poll(lambda: not _pvc_exists(vol_cluster, vol), 600, f"PVC {vol} to be deleted", interval=5)


# --- per-mount access modes: read-only by default -----------------------------------------------------


def _try_write(path: str) -> Callable[[], str]:
    def attempt() -> str:
        import pathlib

        pathlib.Path(path).write_text("nope")  # raises OSError(EROFS) on a read-only mount
        return "wrote"

    return attempt


def test_default_mount_is_read_only_and_a_write_fails_with_erofs(vol_cluster: Cluster) -> None:
    vol = create_volume("100Mi", cluster=vol_cluster)
    try:
        path = f"/mnt/{vol}/seed.txt"
        assert vol_cluster.run(_write(path, "seed"), volumes=_rw(vol), timeout=240) == "written"
        # the default mount reads fine...
        assert vol_cluster.run(_read(path), volumes=vol, timeout=240) == "seed"
        # ...and a write is refused by the kernel, surfaced as the job's own exception
        with pytest.raises(OSError) as e:
            vol_cluster.run(_try_write(f"/mnt/{vol}/other.txt"), volumes=vol, timeout=240)
        assert e.value.errno == errno.EROFS, e.value
        # an explicit "ro" is the same thing, at a custom mountpoint
        ro = {vol: {"access_mode": "ro", "mountpoint": "/data"}}
        assert vol_cluster.run(_read("/data/seed.txt"), volumes=ro, timeout=240) == "seed"
        # and the rw job still works afterwards: nothing about the ro jobs changed the volume
        assert vol_cluster.run(_write(path, "again"), volumes=_rw(vol), timeout=240) == "written"
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _wait_pvc_gone(vol_cluster, vol)


def test_ro_mount_sets_read_only_on_both_pod_spec_fields(vol_cluster: Cluster) -> None:
    vol = create_volume("100Mi", cluster=vol_cluster)
    try:
        assert vol_cluster.run(_write(f"/mnt/{vol}/x", "x"), volumes=_rw(vol), timeout=240) == "written"
        fut = vol_cluster.submit(_read(f"/mnt/{vol}/x"), volumes=vol)
        job = vol_cluster.batch.read_namespaced_job(name=fut.job_name, namespace=vol_cluster.config.namespace)
        pod = job.spec.template.spec
        assert pod.volumes[0].persistent_volume_claim.read_only is True
        assert pod.containers[0].volume_mounts[0].read_only is True
        assert fut.result(timeout=240) == "x"
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _wait_pvc_gone(vol_cluster, vol)


def test_rw_mount_job_has_no_read_only_fields(vol_cluster: Cluster) -> None:
    vol = create_volume("100Mi", cluster=vol_cluster)
    try:
        fut = vol_cluster.submit(_write(f"/mnt/{vol}/x", "x"), volumes=_rw(vol))
        job = vol_cluster.batch.read_namespaced_job(name=fut.job_name, namespace=vol_cluster.config.namespace)
        pod = job.spec.template.spec
        assert not pod.volumes[0].persistent_volume_claim.read_only
        assert not pod.containers[0].volume_mounts[0].read_only
        assert fut.result(timeout=240) == "written"
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _wait_pvc_gone(vol_cluster, vol)


def test_missing_volume_is_a_typed_error_not_a_pending_pod(vol_cluster: Cluster) -> None:
    from sk8s import VolumeNotFound

    with pytest.raises(VolumeNotFound):
        vol_cluster.submit(_read("/mnt/ghost/x"), volumes="ghost")


# --- logical ReadOnlyMany volumes: generation tracking runs everywhere, cloning needs a cloning provisioner ---

RWO_ROX = ("ReadWriteOnce", "ReadOnlyMany")


def _annotations(c: Cluster, name: str) -> dict[str, str]:
    pvc = c.core.read_namespaced_persistent_volume_claim(name=name, namespace=c.config.namespace)
    return dict(pvc.metadata.annotations or {})


def _clone_pvcs(c: Cluster, vol: str) -> list[Any]:
    return list(
        c.core.list_namespaced_persistent_volume_claim(
            c.config.namespace, label_selector=f"{clones.CLONE_OF_LABEL}={vol}"
        ).items
    )


def test_logical_volume_tracks_writer_generation_on_the_real_api(vol_cluster: Cluster) -> None:
    """Marker keys, merge-patch deletes and the generation token against a real API server."""
    vol = create_volume("100Mi", access_modes=RWO_ROX, cluster=vol_cluster)
    try:
        pvc = vol_cluster.core.read_namespaced_persistent_volume_claim(
            name=vol, namespace=vol_cluster.config.namespace
        )
        assert pvc.spec.access_modes == ["ReadWriteOnce"]  # the PVC itself is plain RWO
        assert _annotations(vol_cluster, vol)[clones.ACCESS_MODES_ANNOTATION] == "ReadWriteOnce,ReadOnlyMany"

        fut = vol_cluster.submit(_write(f"/mnt/{vol}/g1", "g1"), volumes=_rw(vol))
        marker = clones.WRITER_MARKER_PREFIX + fut.job_name
        assert marker in _annotations(vol_cluster, vol)  # visible while the writer is in flight
        assert fut.result(timeout=240) == "written"
        ann = _annotations(vol_cluster, vol)
        assert marker not in ann and ann[clones.WRITER_GENERATION_ANNOTATION] == fut.job_name
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _wait_pvc_gone(vol_cluster, vol)


def test_read_only_mount_before_any_writer_is_a_typed_error(vol_cluster: Cluster) -> None:
    from sk8s import VolumeNotPopulated

    vol = create_volume("100Mi", access_modes=RWO_ROX, cluster=vol_cluster)
    try:
        with pytest.raises(VolumeNotPopulated):
            vol_cluster.submit(_read(f"/mnt/{vol}/x"), volumes=vol)
        assert _clone_pvcs(vol_cluster, vol) == []
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _wait_pvc_gone(vol_cluster, vol)


def test_read_only_mount_of_a_logical_volume_gets_a_read_only_clone_claim(vol_cluster: Cluster) -> None:
    """The clone PVC is accepted by a real API server and mounted read-only. Whether it holds
    the data depends on the provisioner (see the lifecycle test), so this does not read it."""
    vol = create_volume("100Mi", access_modes=RWO_ROX, cluster=vol_cluster)
    try:
        assert vol_cluster.run(_write(f"/mnt/{vol}/x", "x"), volumes=_rw(vol), timeout=240) == "written"
        with pytest.raises(OSError) as e:
            vol_cluster.run(_try_write(f"/mnt/{vol}/y"), volumes=vol, timeout=240)
        assert e.value.errno == errno.EROFS
        (clone,) = _clone_pvcs(vol_cluster, vol)
        assert clone.spec.access_modes == ["ReadOnlyMany"]
        assert clone.spec.data_source.kind == "PersistentVolumeClaim" and clone.spec.data_source.name == vol
        assert clone.metadata.labels["sk8s/kind"] == clones.KIND_CLONE
        assert [r.kind for r in clone.metadata.owner_references] == ["ConfigMap"]  # the anchor
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _poll(lambda: not _clone_pvcs(vol_cluster, vol) and not _pvc_exists(vol_cluster, vol),
          GONE_TIMEOUT, f"volume {vol} and its clone to be deleted")


# PVC dataSource clones need a provisioner that implements volume cloning. minikube's default
# (k8s.io/minikube-hostpath) silently ignores dataSource and hands out an EMPTY volume, so the
# data-carrying lifecycle test only runs where cloning is real: GKE's PD CSI (Task 4 runs it there)
# or minikube's csi-hostpath-driver addon. It never runs in the minikube CI job.
_CLONING_PROVISIONERS = {"pd.csi.storage.gke.io", "hostpath.csi.k8s.io"}


def _default_provisioner_clones() -> bool:
    from kubernetes import client as k8s_client

    from sk8s import k8s

    k8s.load()
    for sc in k8s_client.StorageV1Api().list_storage_class().items:
        annotations = sc.metadata.annotations or {}
        if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
            return sc.provisioner in _CLONING_PROVISIONERS
    return False


@pytest.mark.timeout(900)
def test_clone_lifecycle_point_in_time_and_supersede(vol_cluster: Cluster) -> None:
    if not _default_provisioner_clones():
        pytest.skip("the default StorageClass's provisioner does not implement PVC cloning")
    vol = create_volume("1Gi", access_modes=RWO_ROX, cluster=vol_cluster)
    try:
        path = f"/mnt/{vol}/data.txt"
        assert vol_cluster.run(_write(path, "v1"), volumes=_rw(vol), timeout=300) == "written"
        assert vol_cluster.run(_read(path), volumes=vol, timeout=300) == "v1"
        (first,) = _clone_pvcs(vol_cluster, vol)

        # a later writer: readers keep seeing v1 until it COMPLETES, then a new clone shows v2
        assert vol_cluster.run(_write(path, "v2"), volumes=_rw(vol), timeout=300) == "written"
        assert vol_cluster.run(_read(path), volumes=vol, timeout=300) == "v2"
        # The library's own sweeps must remove the superseded clone: it was too young to sweep at
        # the reader's end (CLONE_MIN_AGE), so the next read-only mount on the volume does it once
        # the floor has passed. This test never sweeps by hand.
        time.sleep(clones.CLONE_MIN_AGE + 5)
        assert vol_cluster.run(_read(path), volumes=vol, timeout=300) == "v2"
        _poll(
            lambda: [c.metadata.name for c in _clone_pvcs(vol_cluster, vol)] != [first.metadata.name]
            and len(_clone_pvcs(vol_cluster, vol)) == 1,
            GONE_TIMEOUT, "the library to delete the superseded clone",
        )

        # concurrent readers share the one clone
        futs = [vol_cluster.submit(_read(path), volumes=vol) for _ in range(3)]
        assert [f.result(timeout=300) for f in futs] == ["v2"] * 3
        assert len(_clone_pvcs(vol_cluster, vol)) == 1
    finally:
        delete_volume(vol, cluster=vol_cluster)
    _poll(lambda: not _clone_pvcs(vol_cluster, vol) and not _pvc_exists(vol_cluster, vol),
          GONE_TIMEOUT, f"volume {vol} and its clones to be deleted")
