"""Artifacts: content-addressed files that flow between jobs.

``sk8s.artifact(uri)`` references an existing object; jobs that return files produce Artifacts
whose bytes live once in the CAS (see ``sk8s.cas``). An Artifact is a small immutable value, safe
to pickle and to put in JSON (``to_dict``)."""
from __future__ import annotations

import os
import posixpath
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sk8s.cas import normalize_uri, split_uri


@dataclass(frozen=True)
class Artifact:
    """A reference to immutable bytes.

    ``uri``: where the bytes are (a CAS object, a published object, or an external one).
    ``sha256`` / ``size``: content identity; None for an external reference that has not been
    hashed yet (hashing is lazy, on first staging).
    ``version``: the pin of an external object (GCS generation / S3 ETag / local stat); an
    in-place overwrite changes it, which is detected, never silently read.
    ``name``: the file name the bytes had; staging recreates it so tools see ``s1.fq.gz``.
    ``store``: the artifact store (index + CAS) this artifact belongs to; None for a bare ref.
    ``produced_by``: ``<workflow>/<job>`` that made it (provenance).
    """

    uri: str
    sha256: str | None = None
    size: int | None = None
    version: str | None = None
    name: str | None = None
    store: str | None = None
    produced_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def filename(self) -> str:
        """The file name staging gives this artifact."""
        if self.name:
            return self.name
        base = posixpath.basename(split_uri(self.uri)[2])
        return base or (self.sha256 or "artifact")

    def __repr__(self) -> str:
        ident = f"sha256:{self.sha256[:12]}" if self.sha256 else "unhashed"
        return f"Artifact({self.filename!r}, {ident}, {self.uri})"

    def publish(self, uri: str, copy: bool = False) -> Artifact:
        """Give the artifact a permanent, human-legible home at ``uri`` and return the Artifact
        that lives there (``gs://``, ``s3://`` or a local path).

        Move by default: the destination becomes the recorded location and the CAS copy is
        retired (one copy, decision A3). ``copy=True`` keeps both. Never overwrites a different
        object; the object carries ``sk8s-artifact-sha256`` / ``sk8s-produced-by`` metadata."""
        from sk8s import cas

        return cas.publish(cas.open_artifact_store(self.store, self.uri), self, uri, copy=copy)

    def download(self, path: str | os.PathLike[str]) -> Path:
        """Copy the bytes to a local ``path`` (a directory gets the artifact's file name inside),
        verifying the hash while streaming. Returns the file's path."""
        from sk8s import cas

        return cas.download(cas.open_artifact_store(self.store, self.uri), self, path)


def artifact(
    uri: str,
    *,
    sha256: str | None = None,
    version: str | None = None,
    size: int | None = None,
    name: str | None = None,
) -> Artifact:
    """Reference an existing object (``gs://``, ``s3://``, or a local path). Nothing moves.

    The reference is pinned lazily: on submission the object's current generation/ETag is
    recorded, and staging hashes the bytes as it downloads them. Pass ``sha256=`` to assert
    the expected content up front (a mismatch fails the run instead of using other bytes)."""
    norm = normalize_uri(uri)
    return Artifact(uri=norm, sha256=sha256, size=size, version=version, name=name)


class OutDir(os.PathLike[str]):
    """Annotate a parameter with ``sk8s.OutDir`` and the worker passes an empty scratch directory.

    ``out / "s1.bam"`` is a plain ``pathlib.Path`` inside it. Return such paths (alone, or in a
    list / tuple / dict) and the worker hashes and uploads them into the CAS after the function
    returns, replacing each with an ``Artifact`` in the result the caller gets. Returning the
    OutDir itself declares every file in it (a dict of relative path to Artifact)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def __fspath__(self) -> str:
        return str(self.path)

    def __truediv__(self, other: str | os.PathLike[str]) -> Path:
        return self.path / other

    def __repr__(self) -> str:
        return f"OutDir({str(self.path)!r})"
