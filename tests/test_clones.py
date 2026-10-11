"""The hidden ROX clone: creation, generations, writer gating, supersede/delete, ownership.

Everything runs against the in-memory fakes; the minikube provisioner cannot clone PVCs, so
this is where the clone lifecycle is pinned (the live proof is Task 4 on GKE).
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from kubernetes import client
from kubernetes.client import ApiException

import sk8s
from sk8s import clones
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import (
    Sk8sError, VolumeError, VolumeNotFound, VolumeNotPopulated, VolumeWaitTimeout,
)
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1

NS = "ns1"
RWO_ROX = ["ReadWriteOnce", "ReadOnlyMany"]
RW = {"access_mode": "rw"}
WAIT = 30.0


class H:
    """A Cluster over fakes plus a fake clock the test advances through ``sleep``."""

    def __init__(self, tmp_path: Any) -> None:
        self.root = tmp_path
        self.batch = FakeBatchV1()
        self.core = FakeCoreV1()
        self.now = 0.0
        self.on_sleep: Callable[[], None] | None = None
        self.cluster = Cluster(
            Config(namespace=NS, image="sk8s:dev", store=f"local:{tmp_path}", volume_wait_seconds=WAIT),
            batch_api=self.batch, core_api=self.core, store=LocalStore(tmp_path),
            watcher_events=lambda: iter(()),
        )
        self.cluster._clock = lambda: self.now
        self.cluster._sleep = self._sleep
        self.wall_offset = 0.0  # advanced by age(): the wall clock clones and markers are aged by
        self.cluster._wall_clock = lambda: (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self.wall_offset)
        )

    def age(self, seconds: float) -> None:
        self.wall_offset += seconds

    def _sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()

    # -- volumes
    def volume(self, name: str = "idx", *, keep: bool = False, modes: list[str] | None = None,
               size: str = "10Gi", storage_class: str | None = None) -> str:
        return sk8s.create_volume(
            size, name=name, access_modes=modes or RWO_ROX, keep=keep,
            storage_class=storage_class, cluster=self.cluster,
        )

    def pvc(self, name: str) -> Any:
        return self.core.pvcs[(NS, name)]

    def ann(self, name: str) -> dict[str, str]:
        return dict(self.pvc(name).metadata.annotations or {})

    def clone_names(self, of: str = "idx") -> list[str]:
        return sorted(
            n for (_, n), p in self.core.pvcs.items()
            if (p.metadata.labels or {}).get(clones.CLONE_OF_LABEL) == of
        )

    # -- jobs
    def pod(self, fut: Any) -> Any:
        return self.batch.jobs[(NS, fut.job_name)].spec.template.spec

    def set_status(self, job: str, **status: Any) -> None:
        self.batch.jobs[(NS, job)].status = client.V1JobStatus(**status)

    def end(self, fut: Any, phase: str = "succeeded") -> None:
        """The control plane reports ``fut``'s job over (the future resolves, here to PodFailed)."""
        self.set_status(fut.job_name, **({"succeeded": 1} if phase == "succeeded" else {"failed": 1}))
        self.cluster._on_terminal(fut.job_name, phase)

    def populate(self, volume: str = "idx") -> Any:
        """Run (and complete) an rw job on ``volume``; returns its future."""
        fut = self.cluster.submit(lambda: 1, volumes={volume: RW})
        self.end(fut)
        return fut

    def reader(self, volume: str = "idx") -> Any:
        return self.cluster.submit(lambda: 1, volumes=volume)


@pytest.fixture
def h(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0)  # jobs here end without a result blob
    harness = H(tmp_path)
    yield harness
    harness.cluster.close()


# --- create_volume: the logical volume -------------------------------------------------------------


def test_rwo_plus_rox_creates_an_rwo_primary_and_records_the_declared_modes(h: H) -> None:
    h.volume("idx")
    assert h.pvc("idx").spec.access_modes == ["ReadWriteOnce"]  # PD CSI refuses dual-mode PVCs
    assert h.ann("idx") == {clones.ACCESS_MODES_ANNOTATION: "ReadWriteOnce,ReadOnlyMany"}


@pytest.mark.parametrize(
    "modes",
    [
        ["ReadOnlyMany"],  # passed through exactly as before (the PVC diagnostic covers it)
        ["ReadWriteMany", "ReadOnlyMany"],  # RWX serves concurrent readers natively
        ["ReadWriteOnce"],
        ["ReadWriteOncePod", "ReadOnlyMany"],
    ],
)
def test_other_mode_sets_are_passed_through_verbatim_with_no_annotation(h: H, modes: list[str]) -> None:
    h.volume("v", modes=modes, storage_class="x")
    assert h.pvc("v").spec.access_modes == modes
    assert not h.ann("v")


# --- mounts that need no clone -----------------------------------------------------------------------


def test_ro_mount_of_a_volume_without_rox_mounts_the_primary_read_only(h: H) -> None:
    h.volume("plain", modes=["ReadWriteOnce"])
    pod = h.pod(h.cluster.submit(lambda: 1, volumes="plain"))
    assert pod.volumes[0].persistent_volume_claim.claim_name == "plain"
    assert pod.volumes[0].persistent_volume_claim.read_only is True
    assert pod.containers[0].volume_mounts[0].read_only is True
    assert not [n for (_, n) in h.core.pvcs if n != "plain"]  # no clone
    assert not h.ann("plain")  # and no bookkeeping on it


def test_rw_mount_of_a_plain_volume_leaves_the_pvc_untouched(h: H) -> None:
    h.volume("plain", modes=["ReadWriteOnce"])
    fut = h.cluster.submit(lambda: 1, volumes={"plain": RW})
    h.end(fut)
    assert not h.core.pvc_patches and not h.ann("plain")


def test_missing_volume_is_a_typed_error_in_both_modes_and_creates_nothing(h: H) -> None:
    for vols in ("ghost", {"ghost": RW}):
        with pytest.raises(VolumeNotFound, match="ghost"):
            h.cluster.submit(lambda: 1, volumes=vols)
    assert not h.batch.jobs


@pytest.mark.parametrize("volumes", ["plain", {"plain": RW}])
def test_unreadable_volume_fails_the_submit_in_both_modes(h: H, volumes: Any) -> None:
    """An rw job must never run unmarked because a read blipped; nothing is created."""
    h.volume("plain", modes=["ReadWriteOnce"])

    def boom(name: str, namespace: str) -> Any:
        raise ApiException(status=500, reason="Internal Server Error")

    h.core.read_namespaced_persistent_volume_claim = boom  # type: ignore[method-assign]
    with pytest.raises(VolumeError, match="cannot read volume 'plain'"):
        h.cluster.submit(lambda: 1, volumes=volumes)
    assert not h.batch.jobs and not h.core.pvc_patches


# --- writers: markers and the generation ----------------------------------------------------------------


def test_rw_job_is_marked_before_its_job_exists_and_unmarked_with_a_new_generation(h: H) -> None:
    h.volume()
    seen: dict[str, dict[str, str]] = {}
    create = h.batch.create_namespaced_job

    def create_spy(namespace: str, body: Any) -> Any:
        seen["at_create"] = h.ann("idx")
        return create(namespace, body)

    h.batch.create_namespaced_job = create_spy  # type: ignore[method-assign]
    fut = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    marker = clones.WRITER_MARKER_PREFIX + fut.job_name
    assert marker in seen["at_create"]  # visible to every client before the Job can start
    assert marker in h.ann("idx")
    h.end(fut)
    ann = h.ann("idx")
    assert marker not in ann
    assert ann[clones.WRITER_GENERATION_ANNOTATION] == fut.job_name


def test_failed_writer_clears_its_marker_but_is_not_a_new_generation(h: H) -> None:
    h.volume()
    ok = h.populate()
    bad = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    h.end(bad, "failed")
    ann = h.ann("idx")
    assert ann[clones.WRITER_GENERATION_ANNOTATION] == ok.job_name
    assert not [k for k in ann if k.startswith(clones.WRITER_MARKER_PREFIX)]


def test_writer_whose_job_was_reaped_counts_as_completed(h: H) -> None:
    """The watcher only saw DELETED ("failed"): the Job is gone, so it may well have succeeded."""
    h.volume()
    fut = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    del h.batch.jobs[(NS, fut.job_name)]
    h.cluster._on_terminal(fut.job_name, "failed")
    assert h.ann("idx")[clones.WRITER_GENERATION_ANNOTATION] == fut.job_name


def test_job_create_failure_unmarks_without_a_new_generation(h: H) -> None:
    h.volume()

    def refuse(namespace: str, body: Any) -> Any:
        raise ApiException(status=500, reason="boom")

    h.batch.create_namespaced_job = refuse  # type: ignore[method-assign]
    with pytest.raises(ApiException):
        h.cluster.submit(lambda: 1, volumes={"idx": RW})
    assert not h.ann("idx").keys() - {clones.ACCESS_MODES_ANNOTATION}


def test_writer_with_a_lingering_running_pod_is_not_done(h: H) -> None:
    h.volume()
    fut = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    h.set_status(fut.job_name, succeeded=1)
    h.core.pods[(NS, "p1")] = SimpleNamespace(
        metadata=SimpleNamespace(name="p1", labels={"job-name": fut.job_name}),
        status=SimpleNamespace(phase="Running"),
    )
    assert h.cluster._writer_state(fut.job_name) == clones.RUNNING
    h.core.pods[(NS, "p1")].status.phase = "Succeeded"
    assert h.cluster._writer_state(fut.job_name) == clones.SUCCEEDED


# --- the read-only mount: lazy clone ------------------------------------------------------------------


def test_ro_mount_before_any_completed_writer_is_a_typed_error_and_leaks_nothing(h: H) -> None:
    h.volume()
    with pytest.raises(VolumeNotPopulated, match="rw"):
        h.reader()
    assert not h.batch.jobs and h.clone_names() == []
    assert not list(h.root.rglob("*payload*"))  # the payload was never stored


def test_first_ro_mount_creates_the_clone_and_mounts_it_read_only(h: H) -> None:
    h.volume("idx", size="10Gi", storage_class="fast")
    writer = h.populate()
    fut = h.reader()
    (name,) = h.clone_names()
    assert name == clones.clone_name("idx", writer.job_name)
    clone = h.pvc(name)
    assert clone.spec.access_modes == ["ReadOnlyMany"]
    assert clone.spec.data_source.kind == "PersistentVolumeClaim"
    assert clone.spec.data_source.name == "idx"
    assert clone.spec.storage_class_name == "fast"
    assert clone.spec.resources.requests == {"storage": "10Gi"}
    assert clone.metadata.annotations == {clones.CLONE_GENERATION_ANNOTATION: writer.job_name}
    pod = h.pod(fut)
    vol = pod.volumes[0]
    assert vol.name == "idx" and vol.persistent_volume_claim.claim_name == name
    assert vol.persistent_volume_claim.read_only is True
    assert pod.containers[0].volume_mounts[0].name == "idx"
    assert pod.containers[0].volume_mounts[0].read_only is True


def test_clone_takes_the_primarys_live_storage_class_and_real_capacity(h: H) -> None:
    h.volume("idx", size="10Gi")
    pvc = h.pvc("idx")
    pvc.spec.storage_class_name = "standard-rwo"  # what the API server fills in for a defaulted class
    pvc.status = client.V1PersistentVolumeClaimStatus(phase="Bound", capacity={"storage": "12Gi"})
    h.populate()
    h.reader()
    (name,) = h.clone_names()
    assert h.pvc(name).spec.storage_class_name == "standard-rwo"
    assert h.pvc(name).spec.resources.requests == {"storage": "12Gi"}


def test_many_ro_jobs_share_one_clone_and_create_it_once(h: H) -> None:
    h.volume()
    h.populate()
    futs = [h.reader() for _ in range(5)]
    assert len(h.clone_names()) == 1
    claims = {h.pod(f).volumes[0].persistent_volume_claim.claim_name for f in futs}
    assert claims == set(h.clone_names())
    assert [n for n in h.core.pvc_deleted] == []


def test_owned_volume_clone_shares_the_anchor_and_workflow_label(h: H) -> None:
    h.volume()
    h.populate()
    h.reader()
    (name,) = h.clone_names()
    clone, primary = h.pvc(name), h.pvc("idx")
    assert [r.uid for r in clone.metadata.owner_references] == [
        r.uid for r in primary.metadata.owner_references
    ] == [h.cluster._anchor.metadata.uid]
    assert clone.metadata.labels == {
        "app.kubernetes.io/managed-by": "sk8s",
        "sk8s/kind": "volume-clone",
        "sk8s/clone-of": "idx",
        "sk8s/workflow-id": h.cluster.workflow_id,
    }


def test_kept_volume_clone_is_ownerless_and_unlabeled_by_workflow(h: H) -> None:
    h.volume(keep=True)
    h.populate()
    h.reader()
    (name,) = h.clone_names()
    clone = h.pvc(name)
    assert not clone.metadata.owner_references
    assert "sk8s/workflow-id" not in clone.metadata.labels
    assert clone.metadata.labels["sk8s/kind"] == "volume-clone"  # follows the source: not "volume"


def test_racing_creator_adopts_the_existing_clone(h: H) -> None:
    h.volume()
    writer = h.populate()
    name = clones.clone_name("idx", writer.job_name)
    create = h.core.create_namespaced_persistent_volume_claim
    state = {"first": True}

    def racing_create(namespace: str, body: Any) -> Any:
        if body.metadata.name == name and state["first"]:
            state["first"] = False
            create(namespace, body)  # the other client wins
            raise ApiException(status=409, reason="Conflict")
        return create(namespace, body)

    h.core.create_namespaced_persistent_volume_claim = racing_create  # type: ignore[method-assign]
    h.reader()
    assert h.clone_names() == [name]


def test_an_unrelated_pvc_squatting_on_the_clone_name_is_a_typed_error(h: H) -> None:
    h.volume()
    writer = h.populate()
    name = clones.clone_name("idx", writer.job_name)
    h.core.pvcs[(NS, name)] = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=name, namespace=NS, labels={"app": "theirs"})
    )
    with pytest.raises(VolumeError, match="not a clone"):
        h.reader()


# --- never clone under a running writer ------------------------------------------------------------------


def test_ro_mount_waits_for_a_running_writer_then_times_out_typed(h: H) -> None:
    h.volume()
    h.populate()
    writer = h.cluster.submit(lambda: 1, volumes={"idx": RW})  # still running
    with pytest.raises(VolumeWaitTimeout) as e:
        h.reader()
    assert writer.job_name in str(e.value) and "volume_wait_seconds" in str(e.value)
    assert isinstance(e.value, TimeoutError) and e.value.timeout == WAIT
    assert h.now >= WAIT  # it really waited, on the (fake) clock
    assert len(h.clone_names()) == 0
    assert len(h.batch.jobs) == 2  # only the two writers; the reader was never created


def test_ro_mount_proceeds_once_the_writer_completes_and_clones_that_generation(h: H) -> None:
    h.volume()
    h.populate()
    writer = h.cluster.submit(lambda: 1, volumes={"idx": RW})

    def finish_writer() -> None:
        h.on_sleep = None
        h.end(writer)

    h.on_sleep = finish_writer
    fut = h.reader()
    assert h.clone_names() == [clones.clone_name("idx", writer.job_name)]
    assert h.pod(fut).volumes[0].persistent_volume_claim.claim_name == h.clone_names()[0]


def test_dead_clients_marker_is_reconciled_from_the_job_itself(h: H) -> None:
    """The client that submitted the writer died: the next reader settles its marker."""
    h.volume()
    ok = h.populate()
    ghost = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    h.cluster._mount_plans.pop(ghost.job_name)  # nobody is left to settle it
    h.set_status(ghost.job_name, succeeded=1)
    fut = h.reader()
    ann = h.ann("idx")
    assert clones.WRITER_MARKER_PREFIX + ghost.job_name not in ann
    assert ann[clones.WRITER_GENERATION_ANNOTATION] == ghost.job_name != ok.job_name
    assert h.pod(fut).volumes[0].persistent_volume_claim.claim_name == clones.clone_name(
        "idx", ghost.job_name
    )


def test_dead_clients_failed_writer_is_not_a_generation(h: H) -> None:
    h.volume()
    ok = h.populate()
    ghost = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    h.cluster._mount_plans.pop(ghost.job_name)
    h.set_status(ghost.job_name, failed=1)
    h.reader()
    assert h.ann("idx")[clones.WRITER_GENERATION_ANNOTATION] == ok.job_name


def test_dead_clients_vanished_job_counts_as_completed(h: H) -> None:
    h.volume()
    h.populate()
    ghost = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    h.cluster._mount_plans.pop(ghost.job_name)
    del h.batch.jobs[(NS, ghost.job_name)]
    h.age(clones.MARKER_MIN_AGE + 1)  # a young marker could still be a Job about to be created
    h.reader()
    assert h.ann("idx")[clones.WRITER_GENERATION_ANNOTATION] == ghost.job_name


# --- re-clone and supersede -------------------------------------------------------------------------------


def test_a_completed_writer_triggers_a_new_clone_and_the_old_one_is_deleted(h: H) -> None:
    h.volume()
    w1 = h.populate()
    first = h.reader()
    (old,) = h.clone_names()
    h.end(first)  # the old clone has no active user left
    h.age(clones.CLONE_MIN_AGE + 1)  # ...and is old enough to be swept
    w2 = h.populate()
    fut = h.reader()
    (new,) = h.clone_names()
    assert new == clones.clone_name("idx", w2.job_name) != old
    assert old in h.core.pvc_deleted and (NS, old) not in h.core.pvcs  # the delete was issued
    assert h.pod(fut).volumes[0].persistent_volume_claim.claim_name == new
    assert w1.job_name != w2.job_name


def test_unchanged_volume_is_not_re_cloned(h: H) -> None:
    h.volume()
    h.populate()
    h.reader()
    (first,) = h.clone_names()
    creates: list[str] = []
    real = h.core.create_namespaced_persistent_volume_claim

    def spy(namespace: str, body: Any) -> Any:
        creates.append(body.metadata.name)
        return real(namespace, body)

    h.core.create_namespaced_persistent_volume_claim = spy  # type: ignore[method-assign]
    h.reader()
    h.reader()
    assert h.clone_names() == [first] and h.core.pvc_deleted == []
    assert creates == []  # not even a create attempt: the existing clone is simply read


def test_superseded_clone_still_used_by_an_active_job_is_kept_until_that_job_ends(h: H) -> None:
    h.volume()
    h.populate()
    early = h.reader()  # holds the old clone (job active)
    (old,) = h.clone_names()
    h.age(clones.CLONE_MIN_AGE + 1)
    h.populate()
    h.reader()  # supersedes; must not strand `early`'s pod on a vanished claim
    assert (NS, old) in h.core.pvcs and old not in h.core.pvc_deleted
    assert len(h.clone_names()) == 2
    h.end(early)  # its last user ends: the sweep now issues the delete
    assert old in h.core.pvc_deleted and h.clone_names() == [clones.clone_name("idx", h.ann("idx")[clones.WRITER_GENERATION_ANNOTATION])]


def test_terminating_clones_are_not_deleted_twice(h: H) -> None:
    h.volume()
    h.populate()
    h.reader()
    (old,) = h.clone_names()
    h.pvc(old).metadata.deletion_timestamp = datetime.datetime.now(datetime.timezone.utc)
    h.populate()
    h.reader()
    assert old not in h.core.pvc_deleted  # already terminating (pods still hold it): leave it


# --- a writer must not start while a clone is still being cut ---------------------------------------------------


def _pending_clone(h: H) -> str:
    h.volume()
    h.populate()
    h.reader()  # a reader Job references the fresh clone
    (name,) = h.clone_names()
    h.pvc(name).status = client.V1PersistentVolumeClaimStatus(phase="Pending")
    return name


def test_writer_waits_while_a_reader_is_still_waiting_on_an_unbound_clone(h: H) -> None:
    _pending_clone(h)
    with pytest.raises(VolumeWaitTimeout, match="clone"):
        h.cluster.submit(lambda: 1, volumes={"idx": RW})
    assert h.now >= WAIT


def test_writer_proceeds_once_the_clone_binds(h: H) -> None:
    name = _pending_clone(h)

    def bind() -> None:
        h.pvc(name).status = client.V1PersistentVolumeClaimStatus(phase="Bound")

    h.on_sleep = bind
    h.cluster.submit(lambda: 1, volumes={"idx": RW})  # no raise


def test_writer_is_not_held_by_an_unbound_clone_nobody_is_waiting_on(h: H) -> None:
    name = _pending_clone(h)
    for (ns, jname), job in list(h.batch.jobs.items()):
        if any(v.persistent_volume_claim.claim_name == name for v in job.spec.template.spec.volumes or []):
            h.set_status(jname, succeeded=1)
    h.age(clones.CLONE_MIN_AGE + 1)  # and no reader has touched it for a while
    h.cluster.submit(lambda: 1, volumes={"idx": RW})  # no raise, no wait
    assert h.now == 0.0


# --- delete_volume ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("keep", [False, True])
def test_delete_volume_deletes_its_clones(h: H, keep: bool) -> None:
    h.volume(keep=keep)
    h.populate()
    h.reader()
    (clone,) = h.clone_names()
    sk8s.delete_volume("idx", cluster=h.cluster)
    assert (NS, "idx") not in h.core.pvcs and (NS, clone) not in h.core.pvcs
    assert clone in h.core.pvc_deleted


def test_delete_volume_leaves_other_volumes_clones_alone(h: H) -> None:
    h.volume("a")
    h.volume("b")
    h.populate("a")
    h.populate("b")
    h.reader("a")
    h.reader("b")
    sk8s.delete_volume("a", cluster=h.cluster)
    assert h.clone_names("b") and not h.clone_names("a")


def test_delete_volume_sweeps_orphaned_clones_of_a_volume_that_is_already_gone(h: H) -> None:
    h.volume(keep=True)
    h.populate()
    h.reader()
    del h.core.pvcs[(NS, "idx")]
    sk8s.delete_volume("idx", cluster=h.cluster)
    assert h.clone_names() == []


def test_delete_volume_still_refuses_unmanaged_pvcs_without_touching_clones(h: H) -> None:
    h.core.pvcs[(NS, "theirs")] = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name="theirs", namespace=NS, labels={"app": "x"})
    )
    with pytest.raises(Sk8sError, match="not managed"):
        sk8s.delete_volume("theirs", cluster=h.cluster)
