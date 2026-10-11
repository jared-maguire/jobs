"""Fix round 1: the clone/marker races, queueing grace, clone-provisioning diagnostics, ordering.

Race scenarios are adapted from the reviewer's reproductions: another client acts *while this
client uploads its payload*, which is where the old submit order left a seconds-wide window.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from sk8s import clones
from sk8s.errors import (
    Sk8sError, VolumeAttachBlocked, VolumeError, VolumeProvisioningFailed, VolumeWaitTimeout,
)
from tests.test_clones import H, NS, RW, WAIT
from tests.test_volume_diagnostics import ATTACH_MSG, event
from tests.test_volume_diagnostics import H as DiagH


@pytest.fixture
def h(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0)
    harness = H(tmp_path)
    yield harness
    harness.cluster.close()


def during_next_put(h: H, action: Any) -> None:
    """Run ``action`` (another client's doing) once, in the middle of this client's payload upload."""
    real_put = h.cluster.store.put
    fired: list[int] = []

    def slow_put(*a: Any, **k: Any) -> Any:
        if not fired:
            fired.append(1)
            action()
        return real_put(*a, **k)

    h.cluster.store.put = slow_put  # type: ignore[method-assign]


# --- (a) writers are marked before the payload upload ------------------------------------------


def test_writer_marker_is_visible_before_the_payload_is_uploaded(h: H) -> None:
    h.volume()
    h.populate()
    seen: dict[str, Any] = {}
    during_next_put(h, lambda: seen.update(ann=h.ann("idx")))
    fut = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    assert clones.WRITER_MARKER_PREFIX + fut.job_name in seen["ann"]


def test_a_reader_cloning_during_a_writers_upload_waits_for_it(h: H) -> None:
    """The reviewer's writer-side window: the reader resolves while the writer is still uploading."""
    h.volume()
    h.populate()
    outcome: dict[str, Any] = {}

    def other_client_reads() -> None:
        try:
            h.reader()
        except VolumeWaitTimeout as e:
            outcome["timeout"] = e

    during_next_put(h, other_client_reads)
    writer = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    assert "timeout" in outcome and writer.job_name in str(outcome["timeout"])
    assert h.clone_names() == []  # it waited for the writer instead of cloning under it


@pytest.mark.parametrize("failure", ["store", "anchor", "create", "rejected"])
def test_every_failure_after_the_marker_removes_it_without_a_generation(h: H, failure: str) -> None:
    h.volume()
    ok = h.populate()
    if failure == "store":
        def boom(*a: Any, **k: Any) -> Any:
            raise OSError("store down")
        h.cluster.store.put = boom  # type: ignore[method-assign]
        expected: type[BaseException] = OSError
    elif failure == "anchor":
        def boom_anchor() -> Any:
            raise RuntimeError("anchor gone")
        h.cluster._ensure_anchor = boom_anchor  # type: ignore[method-assign]
        expected = RuntimeError
    else:
        status = 500 if failure == "create" else 403

        def refuse(namespace: str, body: Any) -> Any:
            raise ApiException(status=status, reason="no")
        h.batch.create_namespaced_job = refuse  # type: ignore[method-assign]
        expected = ApiException if failure == "create" else Sk8sError
    with pytest.raises(expected):
        h.cluster.submit(lambda: 1, volumes={"idx": RW})
    ann = h.ann("idx")
    assert not [k for k in ann if k.startswith(clones.WRITER_MARKER_PREFIX)]
    assert ann[clones.WRITER_GENERATION_ANNOTATION] == ok.job_name


# --- (b) the reader resolves its clone after the upload ----------------------------------------


def test_a_clone_is_never_resolved_before_the_payload_is_uploaded(h: H) -> None:
    """Repro 1: another client's writer + sweep land during our upload; our Job must still
    claim a clone that exists (and is the NEW generation's), not the swept one."""
    h.volume()
    h.populate()
    h.end(h.reader())  # clone C_G exists, nobody uses it
    (cg,) = h.clone_names()
    h.age(clones.CLONE_MIN_AGE + 1)

    def other_client() -> None:
        h.populate()
        h.cluster._clone_manager().sweep_stale("idx")

    during_next_put(h, other_client)
    fut = h.reader()
    claim = h.pod(fut).volumes[0].persistent_volume_claim.claim_name
    assert (NS, claim) in h.core.pvcs
    assert claim != cg  # it follows the writer that completed during the upload
    assert claim == clones.clone_name("idx", h.ann("idx")[clones.WRITER_GENERATION_ANNOTATION])


# --- (c) the sweep leaves young and just-adopted clones alone -----------------------------------


def _superseded_setup(h: H) -> str:
    h.volume()
    h.populate()
    h.end(h.reader())
    (old,) = h.clone_names()
    h.populate()  # the old clone is now superseded
    return old


def test_sweep_skips_a_young_superseded_clone_then_deletes_it_once_old(h: H) -> None:
    old = _superseded_setup(h)
    mgr = h.cluster._clone_manager()
    assert mgr.sweep_stale("idx") == []  # created moments ago: a reader may be about to use it
    h.age(clones.CLONE_MIN_AGE + 1)
    assert mgr.sweep_stale("idx") == [old] and old in h.core.pvc_deleted


def test_sweep_skips_an_old_clone_that_was_just_adopted(h: H) -> None:
    h.volume()
    h.populate()
    h.end(h.reader())
    (old,) = h.clone_names()
    h.age(clones.CLONE_MIN_AGE + 1)
    h.end(h.reader())  # adopts the old clone: stamps it as used (its Job has already ended)
    assert clones.CLONE_LAST_USED_ANNOTATION in h.ann(old)
    h.populate()
    mgr = h.cluster._clone_manager()
    assert mgr.sweep_stale("idx") == []  # adoption protects it like creation does
    h.age(clones.CLONE_MIN_AGE + 1)
    assert mgr.sweep_stale("idx") == [old]


def test_adoption_stamps_are_throttled(h: H) -> None:
    h.volume()
    h.populate()
    h.reader()
    h.age(clones.CLONE_MIN_AGE + 1)
    h.reader()
    n = len(h.core.pvc_patches)
    for _ in range(5):
        h.reader()
    assert len(h.core.pvc_patches) == n  # no patch per job in a fan-out


# --- (d) the claim is re-checked after the Job exists ----------------------------------------------


def test_a_clone_swept_between_resolve_and_job_create_fails_loudly_and_cleans_up(h: H) -> None:
    h.volume()
    h.populate()
    real = h.batch.create_namespaced_job

    def create_then_lose_clone(namespace: str, body: Any) -> Any:
        out = real(namespace, body)
        for (_, n) in list(h.core.pvcs):
            if n in h.clone_names():
                del h.core.pvcs[(NS, n)]  # another client's sweep got there first
        return out

    h.batch.create_namespaced_job = create_then_lose_clone  # type: ignore[method-assign]
    with pytest.raises(VolumeError, match="deleted while this job was being submitted"):
        h.reader()
    assert len(h.batch.deleted) == 1  # the doomed Job was removed, not left Pending forever
    assert not list(h.root.rglob("*payload*"))  # and so was its payload


def test_a_terminating_clone_fails_the_recheck_like_a_deleted_one(h: H) -> None:
    h.volume()
    h.populate()
    real = h.batch.create_namespaced_job

    def create_then_mark_terminating(namespace: str, body: Any) -> Any:
        out = real(namespace, body)
        for (_, n) in list(h.core.pvcs):
            if n in h.clone_names():
                # deletion issued but held open by finishing pods: deletionTimestamp set
                h.core.pvcs[(NS, n)].metadata.deletion_timestamp = "2026-10-11T00:00:00Z"
        return out

    h.batch.create_namespaced_job = create_then_mark_terminating  # type: ignore[method-assign]
    with pytest.raises(VolumeError, match="is being deleted"):
        h.reader()
    assert len(h.batch.deleted) == 1  # the doomed Job was removed, not left to go Pending


def test_writer_job_with_a_lost_clone_is_unmarked(h: H) -> None:
    h.volume()
    h.populate()
    real = h.batch.create_namespaced_job

    def boom(namespace: str, body: Any) -> Any:
        real(namespace, body)
        raise ApiException(status=500, reason="after create")

    h.batch.create_namespaced_job = boom  # type: ignore[method-assign]
    with pytest.raises(ApiException):
        h.cluster.submit(lambda: 1, volumes={"idx": RW})
    assert not [k for k in h.ann("idx") if k.startswith(clones.WRITER_MARKER_PREFIX)]


# --- writer guard (repro 2) --------------------------------------------------------------------------


def test_writer_waits_for_a_recent_unbound_clone_even_before_its_reader_job_exists(h: H) -> None:
    h.volume()
    h.populate()
    gen = h.ann("idx")[clones.WRITER_GENERATION_ANNOTATION]
    h.cluster._clone_manager().ensure_clone(h.pvc("idx"), gen)
    h.pvc(h.clone_names()[0]).status = client.V1PersistentVolumeClaimStatus(phase="Pending")
    with pytest.raises(VolumeWaitTimeout):
        h.cluster.submit(lambda: 1, volumes={"idx": RW})
    # an abandoned Pending clone stops holding writers back once it has gone unused for a while
    h.age(clones.CLONE_MIN_AGE + 1)
    h.cluster.submit(lambda: 1, volumes={"idx": RW})


# --- minors -----------------------------------------------------------------------------------------------


def test_4a_a_young_marker_whose_job_does_not_exist_yet_is_not_settled(h: H) -> None:
    h.volume()
    h.populate()
    ghost = "job-upload1"  # marker set, payload still uploading: no Job yet
    h.cluster._clone_manager().mark_writer("idx", ghost)
    with pytest.raises(VolumeWaitTimeout, match=ghost):
        h.reader()
    assert clones.WRITER_MARKER_PREFIX + ghost in h.ann("idx")
    h.age(clones.MARKER_MIN_AGE + 1)  # nobody ever created that Job: now it is a dead marker
    h.reader()
    assert clones.WRITER_MARKER_PREFIX + ghost not in h.ann("idx")


def test_4b_a_late_settle_of_an_older_writer_never_moves_the_generation_back(h: H) -> None:
    h.volume()
    t0 = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)
    a = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    h.set_status(a.job_name, succeeded=1, completion_time=t0)
    # ...A is slow to settle; the next writer B completes later and settles first
    h.cluster._mount_plans.pop(a.job_name)
    b = h.cluster.submit(lambda: 1, volumes={"idx": RW})
    h.set_status(b.job_name, succeeded=1, completion_time=t0 + datetime.timedelta(minutes=5))
    h.cluster._on_terminal(b.job_name, "succeeded")
    assert h.ann("idx")[clones.WRITER_GENERATION_ANNOTATION] == b.job_name
    h.cluster._clone_manager().settle_writer("idx", a.job_name, wrote=True)  # A finally settles
    ann = h.ann("idx")
    assert ann[clones.WRITER_GENERATION_ANNOTATION] == b.job_name  # not regressed to A
    assert clones.WRITER_MARKER_PREFIX + a.job_name not in ann  # but A's marker is cleared


# --- Important 2: the attach grace is configurable ------------------------------------------------------------


def test_attach_grace_default_queues_a_serial_rw_job_behind_a_long_holder(tmp_path: Any) -> None:
    d = DiagH(tmp_path)
    try:
        fut, pod = d.job_with_pending_pod()
        d.holder("sk8s-rox-r1-ro1", "Running")
        d.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
        d.probe()
        d.now += 299  # a holder running for minutes is normal queueing, not a wedge
        assert d.probe() == []
        d.now += 2
        assert d.probe() == [(fut.job_name, "volume-stuck")]  # still typed and bounded
    finally:
        d.cluster.close()


def test_attach_grace_is_configurable(tmp_path: Any) -> None:
    import dataclasses

    d = DiagH(tmp_path)
    try:
        d.cluster.config = dataclasses.replace(d.cluster.config, volume_attach_grace_seconds=10.0)
        fut, pod = d.job_with_pending_pod()
        d.holder("sk8s-rox-r1-ro1", "Running")
        d.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))
        d.probe()
        d.now += 11
        assert d.probe() == [(fut.job_name, "volume-stuck")]
    finally:
        d.cluster.close()


# --- Important 3: the library's own clone failing to provision ---------------------------------------------------

QUOTA = (
    "Volume provisioning failed with infeasible error. rpc error: code = ResourceExhausted "
    "desc = Quota 'SSD_TOTAL_GB' exceeded"
)


def _clone_pending(d: DiagH, *, phase: str = "Pending", labels: dict[str, str] | None = None) -> tuple[Any, str]:
    fut, pod = d.job_with_pending_pod(claim="idx-ro-abc", volume="idx")
    d.core.pvcs[(NS, "idx-ro-abc")] = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name="idx-ro-abc", namespace=NS,
            labels=labels if labels is not None else {"sk8s/kind": "volume-clone"},
        ),
        status=client.V1PersistentVolumeClaimStatus(phase=phase),
    )
    d.core.events.append(event("ProvisioningFailed", QUOTA, "PersistentVolumeClaim", "idx-ro-abc"))
    return fut, pod


def test_a_clone_stuck_provisioning_surfaces_after_the_grace_with_the_event_text(tmp_path: Any) -> None:
    d = DiagH(tmp_path)
    try:
        fut, _ = _clone_pending(d)
        assert d.probe() == []  # the provisioner retries; first sighting starts the clock
        d.now += 299
        assert d.probe() == []
        d.now += 2
        assert d.probe() == [(fut.job_name, "volume-stuck")]
        d.cluster._on_terminal(fut.job_name, "volume-stuck")
        err = fut.exception(timeout=5)
        assert isinstance(err, VolumeProvisioningFailed)
        assert err.volume == "idx" and err.claim == "idx-ro-abc"
        assert "Quota 'SSD_TOTAL_GB' exceeded" in str(err) and "volume_provision_grace_seconds" in str(err)
        assert fut.job_name in d.batch.deleted
    finally:
        d.cluster.close()


def test_a_clone_that_provisioned_since_is_history(tmp_path: Any) -> None:
    d = DiagH(tmp_path)
    try:
        _clone_pending(d)
        d.probe()
        d.core.pvcs[(NS, "idx-ro-abc")].status.phase = "Bound"
        d.now += 1000
        assert d.probe() == []
    finally:
        d.cluster.close()


def test_provisioning_failures_of_claims_sk8s_did_not_clone_are_left_alone(tmp_path: Any) -> None:
    d = DiagH(tmp_path)
    try:
        _clone_pending(d, labels={"app": "theirs"})
        d.probe()
        d.now += 1000
        assert d.probe() == []
    finally:
        d.cluster.close()


def test_a_raw_api_error_creating_the_clone_is_typed(h: H) -> None:
    h.volume()
    h.populate()

    def deny(namespace: str, body: Any) -> Any:
        raise ApiException(status=403, reason="Forbidden")

    h.core.create_namespaced_persistent_volume_claim = deny  # type: ignore[method-assign]
    with pytest.raises(VolumeError, match="could not create the read-only clone") as e:
        h.reader()
    assert isinstance(e.value, Sk8sError) and not h.batch.deleted


# --- 6a ----------------------------------------------------------------------------------------------------------------


def test_6a_the_error_names_the_volume_whose_pv_the_event_is_about(tmp_path: Any) -> None:
    d = DiagH(tmp_path)
    try:
        fut, pod = d.job_with_pending_pod(claim="first-claim", volume="first")
        spec = d.core.pods[(NS, pod)].spec
        spec.volumes.append(client.V1Volume(
            name="second",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="second-claim"),
        ))
        for claim, pv in (("first-claim", "pvc-aaaa"), ("second-claim", "pvc-43dee828-dc4f-445e-a9c8-7b7dcf58dfb1")):
            d.core.pvcs[(NS, claim)] = client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name=claim, namespace=NS),
                spec=client.V1PersistentVolumeClaimSpec(volume_name=pv),
            )
        d.holder("sk8s-rox-r1-ro1", "Running")
        d.core.events.append(event("FailedAttachVolume", ATTACH_MSG, "Pod", pod))  # names pvc-43dee828...
        d.probe()
        d.now += 301
        d.probe()
        d.cluster._on_terminal(fut.job_name, "volume-stuck")
        err = fut.exception(timeout=5)
        assert isinstance(err, VolumeAttachBlocked)
        assert err.volume == "second" and err.claim == "second-claim"
    finally:
        d.cluster.close()


# --- Fix round 2: nothing else retries a sweep the age floor skipped, so close() does ------------------------


def _fast_write_read_write_read(h: H) -> tuple[str, str]:
    h.volume()
    h.populate()
    h.end(h.reader())
    (first,) = h.clone_names()
    h.populate()
    h.end(h.reader())  # all within CLONE_MIN_AGE: the reader-end sweep skips the first clone
    return first, [n for n in h.clone_names() if n != first][0]


def test_close_removes_the_superseded_clone_a_fast_sequence_left_behind(h: H) -> None:
    first, current = _fast_write_read_write_read(h)
    assert h.clone_names() == sorted([first, current])  # the floor kept it so far
    h.cluster.close()
    assert h.clone_names() == [current]
    assert first in h.core.pvc_deleted


def test_close_leaves_a_clone_an_active_job_still_uses(h: H) -> None:
    h.volume()
    h.populate()
    h.reader()  # still running
    (first,) = h.clone_names()
    h.populate()
    h.end(h.reader())
    h.cluster.close()
    assert first in h.clone_names()  # the active-Job guard outranks the age floor


def test_close_sweep_never_raises_and_skips_clients_that_read_nothing(h: H) -> None:
    h.cluster.close()  # nothing read: no API calls at all
    assert not h.core.pvc_deleted
    h.cluster._read_volumes.add("gone-volume")

    def boom(**_: Any) -> Any:
        raise ApiException(status=500, reason="boom")

    h.core.list_namespaced_persistent_volume_claim = boom  # type: ignore[method-assign]
    h.cluster._closed = False
    h.cluster.close()  # a failing sweep is logged, not raised


def test_a_failing_adoption_stamp_is_typed(h: H) -> None:
    h.volume()
    h.populate()
    h.reader()
    h.age(clones.CLONE_MIN_AGE + 1)

    def deny(name: str, namespace: str, body: Any) -> Any:
        raise ApiException(status=500, reason="boom")

    h.core.patch_namespaced_persistent_volume_claim = deny  # type: ignore[method-assign]
    with pytest.raises(VolumeError, match="could not stamp"):
        h.reader()
