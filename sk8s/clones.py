"""The hidden read-only clone behind a ReadOnlyMany-capable volume.

Why a clone exists (Plan 10 spike, ``spike-report.md`` section 5.1/5.4): GKE's PD CSI driver
refuses to provision a ReadOnlyMany claim without a content source, and refuses a
``[ReadWriteOnce, ReadOnlyMany]`` claim outright, so no single PVC can be both written and
mounted read-only on many nodes. The documented method is a *clone*: a ReadOnlyMany claim
with ``dataSource: {kind: PersistentVolumeClaim, name: <primary>}`` of the same class and
size, which attaches read-only to any number of nodes.

So a volume created with ``access_modes=["ReadWriteOnce", "ReadOnlyMany"]`` is a *logical*
volume: a plain ReadWriteOnce **primary** PVC (annotation ``sk8s/access-modes`` records the
modes the user asked for) plus, on demand, a library-maintained clone:

- ``rw`` mounts use the primary directly.
- ``ro`` mounts use the primary's *current clone*, created lazily by the first read-only
  mount and re-created when a ``rw`` job has completed since it was taken. Readers therefore
  see a **point-in-time view: the volume as of the last completed writer**, never a live one.

Generation tracking, all as annotations on the primary PVC (so every client sees it):

- ``writers.sk8s/<job>``: one key per rw job in flight, set before the Job is created and
  removed when it ends. A clone is never taken while any such job is running (a clone of a
  mounted-read-write ext4 is a dirty-journal copy that only mounts with ``norecovery``;
  spike section 8): the read-only mount waits, bounded by ``Config.volume_wait_seconds``.
  A client that dies leaves its marker behind; the next reader reconciles it by looking at
  the Job itself, so a lost marker never blocks anyone for good.
- ``sk8s/writer-generation``: the name of the last rw job that *completed* (succeeded).
  Job names are unique, so this is an idempotent token rather than a counter: two parties
  settling the same job write the same value and cause no spurious second re-clone.

The clone is named ``<primary>-ro-<hash of the generation>`` and carries the generation it was
taken at (``sk8s/clone-generation``). The deterministic name makes creation lock-free: two
clients racing to create it both end up using the same PVC (the loser gets a 409 and adopts).

Superseded clones are deleted once no active Job references them. Readers may still hold an
old clone; Kubernetes' PVC protection keeps the claim ``Terminating`` until those pods are
gone, which is intended, so the delete is issued immediately and never waited on. Clones are
labeled ``sk8s/kind: volume-clone`` and ``sk8s/clone-of: <primary>``, owned by the primary's
anchor (and carry its workflow label) when the primary is workflow-owned, and ownerless when
the primary is ``keep=True`` (``delete_volume`` removes them with their primary).

Races and how they are closed (Plan 10 fix round 1):

- ``submit`` order is: read volumes, hold back writers (:meth:`CloneManager.guard_writer_start`)
  and **mark them at once**, then store the payload, and only then resolve read-only mounts, so a
  clone is created immediately before its Job. Nothing slow (the payload upload, which can take
  seconds) sits between a clone's creation and the Job that references it, nor between a
  writer's guard and its marker.
- :meth:`CloneManager.sweep_stale` never deletes a clone created or *used* (adopted by a new
  read-only mount) within ``CLONE_MIN_AGE`` seconds, which is far above the reader's remaining
  gap (a handful of API calls) even on a slow API server. Adoption stamps ``sk8s/clone-last-used``.
- The writer-side guard also holds a writer back while the current clone is Pending and was
  used within that window, even if its reader's Job does not exist yet.
- After creating a Job that claims a clone, the Cluster re-checks that the clone still exists and
  fails loudly (and removes the Job) if another client's sweep got there first.
- A marker whose Job does not exist yet is not settled until it is ``MARKER_MIN_AGE`` old.
- The generation never moves backward: a late settle of an older writer is compared by Job
  completion time and skipped if a newer writer is already the generation.

Retrying the sweep: a superseded clone younger than ``CLONE_MIN_AGE`` is skipped, and
there is no timer to retry it. It is removed by the next sweep on the volume (any later
read-only submit or reader end), and finally by ``Cluster.close()``, which sweeps every
volume this client read with the floor set to 0. Closing may therefore delete a superseded
clone a *different* client resolved moments ago and has not yet attached a Job to; that
client's post-create check fails loudly ("submit again") rather than hanging, and a Job
that already exists is protected by the active-Job guard regardless of age.

What is still true (by design, not bugs):

- A reader submit that creates a clone and then fails (or dies) leaves a Pending clone that
  holds writers back for up to ``CLONE_MIN_AGE`` seconds after its last use. Intended: the
  floor is what protects submits that are still in flight.
- A clone is cut from the *live primary*. If a writer failed, or was cancelled, before the
  next reader, whatever it had already written to the primary is captured by that reader's clone
  even though that writer never became the generation.
- A marker whose Job is gone is treated as a *completed* writer (the conservative direction: it
  may re-clone needlessly; the alternative is stale reads).
- With a ``WaitForFirstConsumer`` class the clone's disk is cut when its first reader pod is
  scheduled, not when the PVC is created; the writer-side guard above covers that window as long
  as the reader's Job (or the recent-use stamp) exists.
- A cross-client writer that starts in the (now API-calls-wide) gap between a reader's
  clone creation and its Job creation, and whose marker-check lost the race, is not prevented.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import time
from typing import Any, Callable, Sequence

from kubernetes import client
from kubernetes.client import ApiException

from sk8s import anchors
from sk8s.errors import VolumeError, VolumeNotFound, VolumeNotPopulated, VolumeWaitTimeout
from sk8s.spec import MANAGED_BY

log = logging.getLogger("sk8s.clones")

KIND_LABEL = anchors.ANCHOR_KIND_LABEL  # "sk8s/kind"
KIND_CLONE = "volume-clone"
CLONE_OF_LABEL = "sk8s/clone-of"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"

ACCESS_MODES_ANNOTATION = "sk8s/access-modes"
WRITER_GENERATION_ANNOTATION = "sk8s/writer-generation"
CLONE_GENERATION_ANNOTATION = "sk8s/clone-generation"
CLONE_LAST_USED_ANNOTATION = "sk8s/clone-last-used"
WRITER_MARKER_PREFIX = "writers.sk8s/"  # a valid annotation prefix; the job name is the key's name

READ_ONLY_MANY = "ReadOnlyMany"
READ_WRITE_ONCE = "ReadWriteOnce"
READ_WRITE_MANY = "ReadWriteMany"

# Writer states, as reported by the Cluster for a job name.
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
GONE = "gone"  # the Job no longer exists and none of its pods are still running

# Seconds. A clone younger (or used more recently) than this is never swept; it must dwarf the
# reader's gap between resolving a clone and its Job existing (a few API calls), whatever the
# API server latency, yet stay short enough that an abandoned clone is cleaned up promptly.
CLONE_MIN_AGE = 120.0
# Seconds before a writer marker whose Job does not exist yet may be settled as "gone". The
# marker is set before the payload upload, so this bounds how long that upload may take.
MARKER_MIN_AGE = 120.0

WriterState = Callable[[str], str]
CompletedAt = Callable[[str], "datetime.datetime | None"]
WallClock = Callable[[], datetime.datetime]
JobActive = Callable[[Any], bool]


def wants_clone(modes: Sequence[str]) -> bool:
    """True for the logical-volume case: readable by many AND writable, without RWX.

    ``ReadWriteMany`` volumes (Filestore and friends) already support concurrent readers
    natively, and a ReadOnlyMany-only claim is passed through exactly as before.
    """
    return (
        READ_ONLY_MANY in modes and READ_WRITE_ONCE in modes and READ_WRITE_MANY not in modes
    )


def is_clone_backed(pvc: Any) -> bool:
    """True if ``pvc`` is the primary of a logical volume whose read-only mounts use a clone."""
    annotations = pvc.metadata.annotations or {}
    return READ_ONLY_MANY in (annotations.get(ACCESS_MODES_ANNOTATION) or "").split(",")


def generation(pvc: Any) -> str:
    """The last completed writer's job name, or "" if none ever completed."""
    return str((pvc.metadata.annotations or {}).get(WRITER_GENERATION_ANNOTATION) or "")


def active_writers(pvc: Any) -> list[str]:
    return sorted(
        key[len(WRITER_MARKER_PREFIX):]
        for key in (pvc.metadata.annotations or {})
        if key.startswith(WRITER_MARKER_PREFIX)
    )


def clone_name(volume: str, gen: str) -> str:
    return f"{volume}-ro-{hashlib.sha1(gen.encode()).hexdigest()[:8]}"


def _is_404(exc: ApiException) -> bool:
    return bool(exc.status == 404)


class CloneManager:
    """Reads and writes the clone/generation state of volumes in one namespace.

    ``writer_state(job)`` and ``job_active(V1Job)`` come from the Cluster (they need its
    notion of a job's phase); the clock, sleep and wait bounds are injectable for tests.
    """

    def __init__(
        self,
        core: Any,
        batch: Any,
        namespace: str,
        *,
        writer_state: WriterState,
        job_active: JobActive,
        wait_seconds: float,
        poll_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        completed_at: CompletedAt = lambda job: None,
        wall_clock: WallClock = lambda: datetime.datetime.now(datetime.timezone.utc),
    ) -> None:
        self.core = core
        self.batch = batch
        self.namespace = namespace
        self.writer_state = writer_state
        self.job_active = job_active
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sleep = sleep
        self.completed_at = completed_at
        self.wall_clock = wall_clock

    # --- reading -----------------------------------------------------------------------------

    def read_volume(self, name: str) -> Any:
        """The primary PVC ``name``; :class:`VolumeNotFound` if there is none."""
        try:
            return self.core.read_namespaced_persistent_volume_claim(
                name=name, namespace=self.namespace
            )
        except ApiException as e:
            if _is_404(e):
                raise VolumeNotFound(name, self.namespace) from e
            raise VolumeError(
                f"cannot read volume {name!r} in namespace {self.namespace!r} "
                f"(HTTP {e.status}: {e.reason}); not submitting a job that could mount it unchecked"
            ) from e

    def _age(self, stamp: str | datetime.datetime | None) -> float | None:
        """Seconds since ``stamp`` (an ISO string or datetime), or None if unknown."""
        if stamp is None or stamp == "":
            return None
        try:
            when = (
                stamp if isinstance(stamp, datetime.datetime)
                else datetime.datetime.fromisoformat(stamp)
            )
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return (self.wall_clock() - when).total_seconds()

    # --- writer markers ------------------------------------------------------------------------

    def mark_writer(self, volume: str, job: str) -> None:
        """Record that ``job`` is about to mount ``volume`` read-write (before the Job exists)."""
        stamp = self.wall_clock().isoformat()
        self._patch_annotations(volume, {WRITER_MARKER_PREFIX + job: stamp})

    def settle_writer(self, volume: str, job: str, *, wrote: bool) -> None:
        """``job`` is over: drop its marker and, if it completed, make it the new generation."""
        patch: dict[str, str | None] = {WRITER_MARKER_PREFIX + job: None}
        if wrote and self._newest_generation(volume, job):
            patch[WRITER_GENERATION_ANNOTATION] = job
        try:
            self._patch_annotations(volume, patch)
        except ApiException as e:
            if not _is_404(e):  # a deleted volume has nothing left to settle
                raise

    def _newest_generation(self, volume: str, job: str) -> bool:
        """False if the volume's generation is already a writer that completed *after* ``job``.

        Settles can arrive out of order (a slow client settling an old writer after a reader
        already settled a newer one); the generation must never move backward, or readers would
        be served an older clone's data as current. Compared by Job completion time; if either
        time is unknown the settle goes ahead (the conservative direction: a re-clone).
        """
        try:
            current = generation(self.read_volume(volume))
        except VolumeError:
            return True
        if not current or current == job:
            return True
        mine, theirs = self.completed_at(job), self.completed_at(current)
        return not (mine is not None and theirs is not None and mine < theirs)

    def _patch_annotations(self, volume: str, annotations: dict[str, str | None]) -> None:
        self.core.patch_namespaced_persistent_volume_claim(
            name=volume, namespace=self.namespace, body={"metadata": {"annotations": annotations}}
        )

    def _reconcile(self, volume: str, pvc: Any) -> tuple[Any, list[str]]:
        """Settle markers of jobs that are over; returns (fresh pvc, jobs still running)."""
        running: list[str] = []
        settled = False
        for job in active_writers(pvc):
            state = self.writer_state(job)
            if state == GONE:
                # The marker precedes the Job (and the payload upload before it): a Job that does
                # not exist yet is a writer about to start, not one that is over.
                age = self._age((pvc.metadata.annotations or {}).get(WRITER_MARKER_PREFIX + job))
                if age is not None and age < MARKER_MIN_AGE:
                    state = RUNNING
            if state == RUNNING:
                running.append(job)
                continue
            self.settle_writer(volume, job, wrote=state in (SUCCEEDED, GONE))
            settled = True
        return (self.read_volume(volume) if settled else pvc), running

    # --- the read-only mount ---------------------------------------------------------------------

    def resolve_read_only(self, volume: str, pvc: Any) -> str:
        """The claim a read-only mount of clone-backed ``volume`` should use.

        Waits (bounded) for running rw jobs, then ensures a clone of the current generation
        exists, sweeps superseded clones, and returns the clone's name.
        """
        deadline = self.clock() + self.wait_seconds
        while True:
            pvc, running = self._reconcile(volume, pvc)
            if not running:
                break
            if self.clock() >= deadline:
                raise VolumeWaitTimeout(
                    volume,
                    f"rw job(s) {', '.join(running)} to finish",
                    self.wait_seconds,
                    "A read-only mount is served from a clone taken after the last writer "
                    "completes, and a clone is never taken under a running writer. Wait for "
                    "the writer first (future.result()), or raise Config.volume_wait_seconds "
                    "(SK8S_VOLUME_WAIT_SECONDS).",
                )
            self.sleep(self.poll_seconds)
            pvc = self.read_volume(volume)
        gen = generation(pvc)
        if not gen:
            raise VolumeNotPopulated(volume)
        name = self.ensure_clone(pvc, gen)
        self.sweep_stale(volume, current=name)
        return name

    def ensure_clone(self, primary: Any, gen: str) -> str:
        """Create (or adopt, on a race) the clone of ``primary`` for generation ``gen``."""
        volume = primary.metadata.name
        name = clone_name(volume, gen)
        labels = {
            MANAGED_BY_LABEL: MANAGED_BY,
            KIND_LABEL: KIND_CLONE,
            CLONE_OF_LABEL: volume,
        }
        workflow = (primary.metadata.labels or {}).get(anchors.WORKFLOW_LABEL)
        if workflow:  # workflow-owned primary: the clone is selected and cascaded with it
            labels[anchors.WORKFLOW_LABEL] = workflow
        size = self._size_of(primary)
        body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self.namespace,
                labels=labels,
                annotations={CLONE_GENERATION_ANNOTATION: gen},
                # Same anchor as the primary; none for a keep=True volume.
                owner_references=list(primary.metadata.owner_references or []) or None,
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=[READ_ONLY_MANY],
                resources=client.V1VolumeResourceRequirements(requests={"storage": size}),
                # A clone must use its source's class (GKE requirement); read from the live
                # PVC, where the API server has already filled in a defaulted class.
                storage_class_name=primary.spec.storage_class_name,
                data_source=client.V1TypedLocalObjectReference(
                    kind="PersistentVolumeClaim", name=volume
                ),
            ),
        )
        try:
            existing = self.core.read_namespaced_persistent_volume_claim(
                name=name, namespace=self.namespace
            )
        except ApiException as e:
            if not _is_404(e):
                raise self._clone_error(name, volume, "read", e) from e
        else:
            self._check_clone(existing, volume)
            self._touch(existing)
            return name
        try:
            self.core.create_namespaced_persistent_volume_claim(
                namespace=self.namespace, body=body
            )
        except ApiException as e:
            if e.status != 409:
                raise self._clone_error(name, volume, "create", e) from e
            # Another client created it between our read and our create: adopt theirs.
            adopted = self.core.read_namespaced_persistent_volume_claim(
                name=name, namespace=self.namespace
            )
            self._check_clone(adopted, volume)
            self._touch(adopted)
        return name

    @staticmethod
    def _clone_error(name: str, volume: str, verb: str, exc: ApiException) -> VolumeError:
        return VolumeError(
            f"could not {verb} the read-only clone {name!r} of volume {volume!r} "
            f"(HTTP {exc.status}: {exc.reason}); read-only mounts of this volume cannot start"
        )

    def _touch(self, clone: Any) -> None:
        """Stamp an adopted clone as in use, so a concurrent sweep leaves it alone.

        Throttled to once per half ``CLONE_MIN_AGE`` so a fan-out does not patch per job.
        """
        annotations = clone.metadata.annotations or {}
        fresh = self._age(annotations.get(CLONE_LAST_USED_ANNOTATION))
        if fresh is not None and fresh < CLONE_MIN_AGE / 2:
            return
        if (age := self._age(clone.metadata.creation_timestamp)) is not None and age < CLONE_MIN_AGE / 2:
            return  # brand new: creation already protects it
        try:
            self.core.patch_namespaced_persistent_volume_claim(
                name=clone.metadata.name, namespace=self.namespace,
                body={"metadata": {"annotations": {
                    CLONE_LAST_USED_ANNOTATION: self.wall_clock().isoformat()
                }}},
            )
        except ApiException as e:
            if not _is_404(e):
                raise self._clone_error(
                    clone.metadata.name, clone.metadata.labels.get(CLONE_OF_LABEL, "?"), "stamp", e
                ) from e

    def recently_used(self, clone: Any, min_age: float = CLONE_MIN_AGE) -> bool:
        """True if the clone was created, or adopted by a read-only mount, within ``min_age``."""
        for stamp in (
            clone.metadata.creation_timestamp,
            (clone.metadata.annotations or {}).get(CLONE_LAST_USED_ANNOTATION),
        ):
            age = self._age(stamp)
            if age is not None and age < min_age:
                return True
        return False

    @staticmethod
    def _check_clone(existing: Any, volume: str) -> None:
        name = existing.metadata.name
        if (existing.metadata.labels or {}).get(CLONE_OF_LABEL) != volume:
            raise VolumeError(
                f"PVC {name!r} already exists and is not a clone of volume {volume!r}; "
                f"rename or delete it"
            )
        if existing.metadata.deletion_timestamp is not None:
            raise VolumeError(
                f"the read-only clone {name!r} of volume {volume!r} is being deleted; "
                f"retry in a moment"
            )

    @staticmethod
    def _size_of(primary: Any) -> str:
        status = getattr(primary, "status", None)
        capacity = (getattr(status, "capacity", None) or {}).get("storage")
        if capacity:  # what the disk really is; never smaller than the request
            return str(capacity)
        return str(primary.spec.resources.requests["storage"])

    # --- the read-write mount ----------------------------------------------------------------------

    def guard_writer_start(self, volume: str, pvc: Any) -> None:
        """Hold a writer back while the volume's current clone is still being cut.

        A Pending clone that a reader Job is waiting on is cloned the moment that reader is
        scheduled; a writer attached by then would be cloned mid-write. Waits (bounded) for the
        clone to bind. A Pending clone nobody references is harmless and is not waited on.
        """
        gen = generation(pvc)
        if not gen:
            return
        name = clone_name(volume, gen)
        deadline = self.clock() + self.wait_seconds
        while True:
            try:
                clone = self.core.read_namespaced_persistent_volume_claim(
                    name=name, namespace=self.namespace
                )
            except ApiException as e:
                if _is_404(e):
                    return
                raise
            phase = getattr(getattr(clone, "status", None), "phase", None)
            # A Pending clone is about to be cut by a reader: one whose Job exists, or one a
            # reader resolved moments ago and is still about to create the Job for.
            if phase != "Pending" or not (self.recently_used(clone) or self.active_jobs_using(name)):
                return
            if self.clock() >= deadline:
                raise VolumeWaitTimeout(
                    volume,
                    f"the read-only clone {name!r} to bind",
                    self.wait_seconds,
                    "A read-write job is held back while a read-only job is still waiting to be "
                    "scheduled, because the clone is cut when that reader's pod starts and must "
                    "not capture a live writer. Check why the read-only job is not starting "
                    "(kubectl get pods), cancel it, or raise Config.volume_wait_seconds.",
                )
            self.sleep(self.poll_seconds)

    # --- cleanup ---------------------------------------------------------------------------------------

    def active_jobs_using(self, claim: str) -> list[str]:
        """Names of unfinished sk8s Jobs whose pod template claims ``claim``."""
        jobs = self.batch.list_namespaced_job(
            namespace=self.namespace, label_selector=f"{MANAGED_BY_LABEL}={MANAGED_BY}"
        )
        users = []
        for job in jobs.items:
            template = getattr(getattr(job, "spec", None), "template", None)
            pod = getattr(template, "spec", None)
            claims = {
                v.persistent_volume_claim.claim_name
                for v in (getattr(pod, "volumes", None) or [])
                if v.persistent_volume_claim is not None
            }
            if claim in claims and self.job_active(job):
                users.append(job.metadata.name)
        return users

    def clones_of(self, volume: str) -> list[Any]:
        listing = self.core.list_namespaced_persistent_volume_claim(
            namespace=self.namespace, label_selector=f"{CLONE_OF_LABEL}={volume}"
        )
        return list(listing.items)

    def sweep_stale(
        self, volume: str, *, current: str | None = None, min_age: float = CLONE_MIN_AGE
    ) -> list[str]:
        """Delete clones of ``volume`` other than ``current`` that no active Job references.

        ``current`` defaults to the clone of the volume's present generation. ``min_age`` is the
        floor described in the module docstring; ``Cluster.close`` passes 0 (see there). A superseded
        clone still referenced by an unfinished Job is left for a later sweep: deleting it
        would strand that Job's pending (or retried) pod on a vanished claim. Returns the names
        whose delete was issued.
        """
        if current is None:
            try:
                gen = generation(self.read_volume(volume))
            except VolumeNotFound:
                return []
            current = clone_name(volume, gen) if gen else None
        deleted = []
        for clone in self.clones_of(volume):
            name = clone.metadata.name
            if name == current or clone.metadata.deletion_timestamp is not None:
                continue
            if self.recently_used(clone, min_age):  # a reader may be between resolving it and its Job
                continue
            if self.active_jobs_using(name):
                continue
            try:
                self.core.delete_namespaced_persistent_volume_claim(
                    name=name, namespace=self.namespace
                )
            except ApiException as e:
                if not _is_404(e):
                    raise
            deleted.append(name)
        return deleted

    def delete_all(self, volume: str) -> list[str]:
        """Issue a delete for every clone of ``volume`` (used when the volume itself goes)."""
        deleted = []
        for clone in self.clones_of(volume):
            name = clone.metadata.name
            try:
                self.core.delete_namespaced_persistent_volume_claim(
                    name=name, namespace=self.namespace
                )
            except ApiException as e:
                if not _is_404(e):
                    raise
            deleted.append(name)
        return deleted
