"""Volumes integration suite: create/delete, anchor cascade, keep=True, scratch_volume.

Needs the worker image built from THIS branch, tagged ``sk8s-dev-volumes:latest``::

    minikube image build -t sk8s-dev-volumes:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_volumes.py -v

Override the tag with ``SK8S_VOLUMES_TEST_IMAGE``. Needs the default StorageClass
(minikube's ``standard``) and ``persistentvolumeclaims`` in the sk8s Role. Jobs run
one after another, so ReadWriteOnce re-use is fine on a single-node cluster.
"""
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from kubernetes.client import ApiException

from sk8s import create_volume, delete_volume, scratch_volume
from sk8s.cluster import Cluster
from sk8s.config import load_config
from tests.integration.conftest import requires_cluster

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

IMAGE = os.environ.get("SK8S_VOLUMES_TEST_IMAGE", "sk8s-dev-volumes:latest")
# A PVC delete blocks on the pvc-protection finalizer until every pod that mounts it is
# *deleted*; Succeeded pods count. Job pods vanish only when the TTL controller reaps
# the job (ttl_seconds_after_finished=30 here, plus controller lag), so 30s is too tight.
GONE_TIMEOUT = 75


def _new_cluster() -> Cluster:
    return Cluster(load_config(image=IMAGE, pull_policy="Never", ttl_seconds_after_finished=30))


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
        assert vol_cluster.run(_write(path, "hello volume"), volumes=vol, timeout=240) == "written"
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
        assert vol_cluster.run(_write(path, "kept"), volumes=vol, timeout=240) == "written"
        vol_cluster.close(delete_workflow=True)  # anchor cascade

        # Survives a stable 5s window (a cascade would have removed it by now).
        end = time.monotonic() + 5
        while time.monotonic() < end:
            assert _pvc_exists(vol_cluster, vol), "kept volume was removed by the anchor cascade"
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
        assert vol_cluster.run(_write(path, "scratch"), volumes=vol, timeout=240) == "written"
        assert vol_cluster.run(_read(path), volumes=vol, timeout=240) == "scratch"
    _wait_pvc_gone(vol_cluster, vol)
