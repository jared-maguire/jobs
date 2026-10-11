"""Persistent volumes: ``create_volume``, ``delete_volume`` and ``scratch_volume``.

A volume is a PersistentVolumeClaim. Mount it in a job with ``volumes=<name>``
(mounted at ``/mnt/<name>``), or ``volumes={name: path}``.

Lifecycle, chosen at creation:

- ``keep=False`` (default): the PVC is owned by the workflow's anchor (an
  ownerReference, like jobs and blob ConfigMaps) and carries the workflow-id
  label, so it dies with its workflow, whether the anchor is deleted by
  ``Cluster.close`` or reaped by ``clean()``.
- ``keep=True``: no ownerReference and NO workflow-id label, plus
  ``sk8s/kind: volume-keep``, mirroring the exemption services have. Neither
  anchor cascade nor a retention sweep can reach it (``clean()`` only ever
  selects ``sk8s/kind: anchor`` ConfigMaps); only ``delete_volume`` removes it.

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
from typing import Any, Iterator, Sequence

from kubernetes import client
from kubernetes.client import ApiException

from sk8s import anchors
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
    DNS-1123 label (``ValueError``) and unused (``Sk8sError``). ``storage_class``
    is set only when given (otherwise the cluster default applies).

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
                owner_references=owner_references,
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=modes,
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
    (PVC protection finalizer) and disappears when those pods release it.
    Raises ``Sk8sError`` for a PVC that sk8s did not create.
    """
    c = _resolve(cluster)
    namespace = _namespace(c)
    core = c.core
    try:
        pvc = core.read_namespaced_persistent_volume_claim(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return
        raise
    if ((pvc.metadata.labels or {}).get(_MANAGED_BY_LABEL)) != MANAGED_BY:
        # Never destroy someone else's data through this helper.
        raise Sk8sError(f"volume {name!r} is not managed by sk8s; refusing to delete it")
    try:
        core.delete_namespaced_persistent_volume_claim(name=name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise


@contextlib.contextmanager
def scratch_volume(size: str, cluster: Any | None = None) -> Iterator[str]:
    """Create a volume, yield its name, and delete it on exit (also on exceptions)."""
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
