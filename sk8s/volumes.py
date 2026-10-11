"""Persistent volumes: ``create_volume``, ``delete_volume`` and ``scratch_volume``.

A volume is a PersistentVolumeClaim. Mount it in a job with ``volumes=``, where each
mount picks its own access mode and **defaults to read-only**:

- ``volumes="data"``: one read-only mount at ``/mnt/data``;
- ``volumes=["a", "b"]``: several read-only mounts, each at ``/mnt/<name>``;
- ``volumes={"data": {"access_mode": "rw", "mountpoint": "/work"}}``: per-volume
  choice. ``access_mode`` is ``"rw"`` or ``"ro"`` (default ``"ro"``) and ``mountpoint``
  an absolute path (default ``/mnt/<name>``); both keys are optional, so
  ``{"data": {}}`` is the same as ``"data"``.

A job that writes must therefore say so (``"access_mode": "rw"``); a read-only mount sets
``readOnly: true`` on both the pod volume and the volume mount, so a write fails in the job
with ``Read-only file system``. The pre-2.0 ``{name: path}`` form is rejected with a
``ValueError`` that shows the replacement.

Lifecycle, chosen at creation:

- ``keep=False`` (default): the PVC is owned by the workflow's anchor (an
  ownerReference, like jobs and blob ConfigMaps) and carries the workflow-id
  label, so it dies with its workflow, whether the anchor is deleted by
  ``Cluster.close`` or reaped by ``clean()``.
- ``keep=True``: no ownerReference and NO workflow-id label, plus
  ``sk8s/kind: volume-keep``, mirroring the exemption services have. Neither
  anchor cascade nor a retention sweep can reach it (``clean()`` only ever
  selects ``sk8s/kind: anchor`` ConfigMaps); only ``delete_volume`` removes it.

ReadOnlyMany and point-in-time reads
-----------------------------------

A volume is *logical*: ``create_volume(..., access_modes=["ReadWriteOnce", "ReadOnlyMany"])``
declares which modes mounts may use, and each job mount picks one (see above). Cloud block
storage cannot be written and read by many nodes at once (GKE's PD CSI driver refuses a
ReadOnlyMany claim with no content source and a dual-mode claim outright), so sk8s backs the
volume with a plain ReadWriteOnce PVC and serves read-only mounts from a hidden *clone* of it
(see :mod:`sk8s.clones` for the mechanism):

- ``rw`` mounts use the volume itself (one node at a time, as for any ReadWriteOnce disk).
- ``ro`` mounts use the volume's current clone, which any number of jobs on any number of
  nodes can mount at once. **Read-only mounts see a point-in-time view: the volume as of the
  last writer that completed.** A write that is still in flight, or lands after the clone was
  taken, is invisible to readers until the writing job finishes and a later read-only mount
  re-clones.
- The clone is taken lazily by the first read-only mount, and again by the first read-only
  mount after another ``rw`` job has completed. A read-only mount never clones under a running
  writer: it waits for the writer, up to ``Config.volume_wait_seconds`` (``VolumeWaitTimeout``).
  Superseded clones are deleted; pods still using one keep it alive until they finish.
- Clones are cut from the primary at its size and storage class, labeled
  ``sk8s/kind: volume-clone`` and owned like the volume (same anchor; none for ``keep=True``,
  where ``delete_volume`` removes them with the volume).

Read-only mounts need a storage class whose provisioner implements PVC cloning (GKE's PD CSI
driver does). A provisioner that ignores ``dataSource`` (minikube's default
``k8s.io/minikube-hostpath``) hands out an EMPTY clone without any error, so do not use
``ReadOnlyMany`` volumes there except to exercise the wiring.

A volume without ``ReadOnlyMany`` in its modes has no clone: a ``ro`` mount mounts the volume
itself read-only, with ReadWriteOnce's one-node-at-a-time limit (a second node's pod waits
for the first; sk8s reports that as ``VolumeAttachBlocked`` instead of hanging).

Deleting a PVC that a running pod still mounts blocks on Kubernetes' PVC
protection finalizer. ``delete_volume`` never waits for that: it issues a plain
delete (default propagation) and returns; the PVC vanishes once the pods that
mount it are gone.
"""
from __future__ import annotations

import contextlib
import logging
import re
import secrets
import string
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from kubernetes import client
from kubernetes.client import ApiException

from sk8s import anchors, clones
from sk8s.errors import Sk8sError
from sk8s.spec import MANAGED_BY

log = logging.getLogger("sk8s.volumes")

KIND_LABEL = anchors.ANCHOR_KIND_LABEL  # "sk8s/kind"
KIND_OWNED = "volume"
KIND_KEPT = "volume-keep"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"

_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")  # DNS-1123 label
_MAX_NAME = 63
_ALPHABET = string.ascii_lowercase + string.digits
_NAME_ATTEMPTS = 5


RW = "rw"
RO = "ro"
_ACCESS_MODES = (RW, RO)
_MOUNT_KEYS = ("access_mode", "mountpoint")


@dataclass(frozen=True)
class Mount:
    """One requested mount: a volume, how it is accessed, and where it appears in the pod."""

    name: str
    access_mode: str
    mountpoint: str

    @property
    def read_only(self) -> bool:
        return self.access_mode == RO


def _mount(name: Any, inner: Mapping[str, Any]) -> Mount:
    if not isinstance(name, str) or not name:
        raise ValueError(f"volume names must be non-empty strings, got {name!r}")
    unknown = sorted(str(k) for k in inner if k not in _MOUNT_KEYS)
    if unknown:
        raise ValueError(
            f"volumes[{name!r}]: unknown key(s) {', '.join(repr(k) for k in unknown)}; "
            f"the allowed keys are 'access_mode' (\"rw\" or \"ro\") and 'mountpoint'"
        )
    mode = inner.get("access_mode", RO)
    if not isinstance(mode, str) or mode not in _ACCESS_MODES:
        raise ValueError(
            f"volumes[{name!r}]['access_mode'] must be 'rw' or 'ro' (default 'ro'), got {mode!r}"
        )
    mountpoint = inner.get("mountpoint", f"/mnt/{name}")
    if not isinstance(mountpoint, str) or not mountpoint.startswith("/"):
        raise ValueError(
            f"volumes[{name!r}]['mountpoint'] must be an absolute path such as '/mnt/{name}', "
            f"got {mountpoint!r}"
        )
    return Mount(name, mode, mountpoint)


def parse_mounts(volumes: Any) -> list[Mount]:
    """Normalize a ``volumes=`` argument into :class:`Mount` objects (``ValueError`` if invalid).

    ``None``/empty means no mounts; a ``str`` is one read-only mount; a list or tuple of
    names is many read-only mounts; a dict maps each name to
    ``{"access_mode": "rw"|"ro", "mountpoint": path}`` with both keys optional.
    """
    if volumes is None:
        return []
    if isinstance(volumes, str):
        names: list[Any] = [volumes]
        mounts = [_mount(volumes, {})]
    elif isinstance(volumes, (list, tuple)):
        names = list(volumes)
        mounts = [_mount(n, {}) for n in names]
    elif isinstance(volumes, Mapping):
        names = list(volumes)
        mounts = []
        for name, inner in volumes.items():
            if isinstance(inner, str):
                raise ValueError(
                    f"volumes[{name!r}] = {inner!r}: the {{name: path}} form was replaced. Use "
                    f"{{{name!r}: {{\"mountpoint\": {inner!r}}}}} for a read-only mount (the new "
                    f"default is read-only), or add \"access_mode\": \"rw\" if the job writes"
                )
            if not isinstance(inner, Mapping):
                raise ValueError(
                    f"volumes[{name!r}] must be a dict like "
                    f"{{\"access_mode\": \"rw\", \"mountpoint\": \"/mnt/{name}\"}} "
                    f"(both keys optional), got {inner!r}"
                )
            mounts.append(_mount(name, inner))
    else:
        raise ValueError(
            "volumes must be a volume name, a list of names, or a dict of "
            f"{{name: {{\"access_mode\": ..., \"mountpoint\": ...}}}}, got {volumes!r}"
        )
    seen_names: set[str] = set()
    seen_points: set[str] = set()
    for m in mounts:
        if m.name in seen_names:
            raise ValueError(f"volume {m.name!r} is listed more than once in volumes=")
        if m.mountpoint in seen_points:
            raise ValueError(
                f"two volumes share the mountpoint {m.mountpoint!r}; give each its own 'mountpoint'"
            )
        seen_names.add(m.name)
        seen_points.add(m.mountpoint)
    return mounts


def _validate_name(name: str) -> None:
    if len(name) > _MAX_NAME or not _NAME_RE.match(name):
        raise ValueError(
            f"invalid volume name {name!r}: must be a DNS-1123 label (lowercase letters, "
            f"digits and '-'; start and end alphanumeric; at most {_MAX_NAME} chars)"
        )


def _generate_name() -> str:
    return "vol-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def _resolve(cluster: Any | None) -> Any:
    if cluster is not None:
        return cluster
    from sk8s.api import _cluster

    return _cluster()


def _namespace(c: Any) -> str:
    namespace = c.config.namespace
    assert namespace is not None
    return str(namespace)


def create_volume(
    size: str,
    *,
    name: str | None = None,
    access_modes: Sequence[str] = ("ReadWriteOnce",),
    storage_class: str | None = None,
    keep: bool = False,
    cluster: Any | None = None,
) -> str:
    """Create a PersistentVolumeClaim and return its name.

    ``size`` (e.g. ``"10Gi"``) and ``access_modes`` go to the PVC spec verbatim.
    ``name`` defaults to a generated ``vol-<rand6>``; an explicit name must be a
    DNS-1123 label (``ValueError``) and unused (``Sk8sError``).

    A volume declared with both ``"ReadWriteOnce"`` and ``"ReadOnlyMany"`` (and not
    ``"ReadWriteMany"``) is *logical*: the PVC itself is created ReadWriteOnce, because the
    PD CSI driver cannot provision a dual-mode or content-less ReadOnlyMany claim, and the
    declared modes are recorded in the ``sk8s/access-modes`` annotation. Read-only mounts of
    it are then served from a hidden clone and see the volume as of the last completed
    ``rw`` job (see the module docstring). Every other mode set goes to the PVC spec
    verbatim.

    ``storage_class`` always wins when given. Otherwise it is resolved from the access
    modes, as in v1: ``ReadWriteMany`` selects the class ``sk8s config`` recorded for this
    cluster (``Sk8sError`` with a ``sk8s config`` hint if none is recorded, rather than a
    PVC stuck in ``Pending``); any other modes leave it unset so the cluster default applies.

    By default the volume is owned by the workflow's anchor and is deleted with
    the workflow. ``keep=True`` makes it independent of any workflow; delete it
    with ``delete_volume``.
    """
    if not size or not isinstance(size, str):
        raise ValueError(f"size must be a non-empty string like '10Gi', got {size!r}")
    modes = [access_modes] if isinstance(access_modes, str) else list(access_modes)
    if not modes:
        raise ValueError("access_modes must not be empty")
    if name is not None:
        _validate_name(name)
    c = _resolve(cluster)
    namespace = _namespace(c)
    if storage_class is None and "ReadWriteMany" in modes:
        storage_class = c.config.rwx_storage_class
        if storage_class is None:
            raise Sk8sError(
                "access_modes includes ReadWriteMany but no RWX storage class is configured "
                "for this cluster; run `sk8s config` (it records the profile's class), set "
                "SK8S_RWX_STORAGE_CLASS, or pass storage_class= explicitly"
            )

    pvc_modes = modes
    annotations = None
    if clones.wants_clone(modes):
        pvc_modes = [m for m in modes if m != clones.READ_ONLY_MANY]
        annotations = {clones.ACCESS_MODES_ANNOTATION: ",".join(modes)}

    labels = {_MANAGED_BY_LABEL: MANAGED_BY}
    owner_references = None
    if keep:
        labels[KIND_LABEL] = KIND_KEPT
    else:
        anchor = c._ensure_anchor()  # the volume is owned from birth, as a submitted job is
        owner_references = [anchors.owner_ref(anchor)]
        labels[anchors.WORKFLOW_LABEL] = c.workflow_id
        labels[KIND_LABEL] = KIND_OWNED

    attempts = 1 if name is not None else _NAME_ATTEMPTS
    for _ in range(attempts):
        pvc_name = name if name is not None else _generate_name()
        body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=pvc_name, namespace=namespace, labels=dict(labels),
                owner_references=owner_references, annotations=annotations,
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=pvc_modes,
                resources=client.V1VolumeResourceRequirements(requests={"storage": size}),
                storage_class_name=storage_class,
            ),
        )
        try:
            c.core.create_namespaced_persistent_volume_claim(namespace=namespace, body=body)
        except ApiException as e:
            if e.status != 409:
                raise
            if name is not None:
                raise Sk8sError(
                    f"volume {name!r} already exists in namespace {namespace!r}; "
                    f"delete it first (delete_volume({name!r})) or pick another name"
                ) from e
            continue  # generated name collided: draw another
        return pvc_name
    raise Sk8sError("could not generate an unused volume name")  # pragma: no cover


def delete_volume(name: str, cluster: Any | None = None) -> None:
    """Delete the PVC ``name``; a no-op if it does not exist.

    Does not wait: a PVC still mounted by a running pod stays in ``Terminating``
    (PVC protection finalizer) and disappears when those pods release it. The volume's
    hidden read-only clones (``ReadOnlyMany`` volumes) are deleted with it, the same way.
    Raises ``Sk8sError`` for a PVC that sk8s did not create.
    """
    c = _resolve(cluster)
    namespace = _namespace(c)
    core = c.core
    try:
        pvc = core.read_namespaced_persistent_volume_claim(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            c._clone_manager().delete_all(name)  # a leftover clone of a volume already gone
            return
        raise
    if ((pvc.metadata.labels or {}).get(_MANAGED_BY_LABEL)) != MANAGED_BY:
        # Never destroy someone else's data through this helper.
        raise Sk8sError(f"volume {name!r} is not managed by sk8s; refusing to delete it")
    # Clones first: if deleting one fails the volume is still there and the call can be repeated.
    c._clone_manager().delete_all(name)
    try:
        core.delete_namespaced_persistent_volume_claim(name=name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise


@contextlib.contextmanager
def scratch_volume(size: str, cluster: Any | None = None) -> Iterator[str]:
    """Create a volume, yield its name, and delete it on exit (also on exceptions).

    Mounts default to read-only, so a job that fills the scratch space must ask to write::

        with sk8s.scratch_volume("10Gi") as vol:
            sk8s.run(work, volumes={vol: {"access_mode": "rw"}})
    """
    name = create_volume(size, cluster=cluster)
    try:
        yield name
    except BaseException:
        try:
            delete_volume(name, cluster=cluster)
        except Exception:  # noqa: BLE001 — don't mask the body's exception
            log.exception("could not delete scratch volume %s", name)
        raise
    else:
        delete_volume(name, cluster=cluster)
