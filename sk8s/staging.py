"""Staging: artifact-typed arguments become local paths; declared outputs become artifacts.

Two halves:

* client side, at submit: ``prepare_submission`` finds the Artifact arguments (anywhere inside
  lists/tuples/dicts), pins external references to their current generation/ETag (so an
  in-place overwrite is detected, never silently read), records the hashes in the workflow's
  reference set BEFORE the job exists (record-intent-first), and sizes the pod's ephemeral
  storage (2x the known input bytes, decision A2).
* worker side, around the user function: ``run_with_artifacts`` stages inputs into a scratch
  directory (hash-while-downloading), hands the function ``Path`` s (and an ``OutDir`` where its
  signature asks for one), and after it returns hashes and uploads the declared outputs into
  the CAS, replacing them with ``Artifact`` s in the value that becomes the result.

Scope notes (see the artifacts report): a nested dataclass or custom object holding an Artifact
is NOT walked; ``batch_size > 1`` stages inputs but does not support ``OutDir``.
"""
from __future__ import annotations

import contextlib
import dataclasses
import inspect
import math
import posixpath
import shutil
import tempfile
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from sk8s.artifacts import Artifact, OutDir
from sk8s import cas
from sk8s.cas import ArtifactStore, location, store_for_uri
from sk8s.errors import ArtifactChanged, ArtifactError, ArtifactNotFound

EPHEMERAL = "ephemeral-storage"
_MIB = 1024 * 1024
GENERIC_EPHEMERAL_THRESHOLD = 50 * 1024**3  # A2: above this a generic ephemeral volume is wanted
# Never request less than GKE Autopilot's own default (1 GiB): Autopilot sets the ephemeral
# LIMIT equal to the request, and the container's writable layer and logs count against it, so a
# request sized only for a few small inputs would evict the pod. Deviation from A2's bare 2x.
EPHEMERAL_FLOOR = 1024**3


# --- walking argument trees ------------------------------------------------------------


def find_artifacts(obj: Any) -> list[Artifact]:
    """Every Artifact inside plain lists / tuples / dicts / sets / frozensets of ``obj``, in
    order, de-duplicated. Only these exact container types are walked (``map_tree`` rebuilds the
    same ones), so a subclass, namedtuple or dataclass holding an Artifact is left alone
    consistently rather than half-handled."""
    seen: dict[Artifact, None] = {}

    def walk(x: Any) -> None:
        if isinstance(x, Artifact):
            seen.setdefault(x)
        elif type(x) in (list, tuple, set, frozenset):
            for item in x:
                walk(item)
        elif type(x) is dict:
            for item in x.values():
                walk(item)

    walk(obj)
    return list(seen)


def map_tree(obj: Any, fn: Callable[[Any], Any], leaf: type | tuple[type, ...]) -> Any:
    """Rebuild the containers find_artifacts walks, with every ``leaf``-typed value replaced by
    fn(value)."""
    if isinstance(obj, leaf):
        return fn(obj)
    kind = type(obj)
    if kind is list:
        return [map_tree(x, fn, leaf) for x in obj]
    if kind is tuple:
        return tuple(map_tree(x, fn, leaf) for x in obj)
    if kind in (set, frozenset):
        return kind(map_tree(x, fn, leaf) for x in obj)
    if kind is dict:
        return {k: map_tree(v, fn, leaf) for k, v in obj.items()}
    return obj


# --- client side -----------------------------------------------------------------------


@dataclass
class Prepared:
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    inputs: list[Artifact] = field(default_factory=list)  # pinned
    ephemeral_bytes: int = 0
    header: dict[str, Any] = field(default_factory=dict)  # extra payload-header fields


def pin(astore: ArtifactStore, art: Artifact) -> Artifact:
    """Resolve a bare external reference to its current (version, size). Raises ArtifactNotFound
    for a missing object and ArtifactChanged if it no longer matches a size the caller gave."""
    if art.store is not None or art.version is not None:
        return art  # minted by sk8s (CAS / published) or already pinned
    reader, _ = store_for_uri(art.uri, astore.store)
    st = reader.stat(art.uri)
    if st is None:
        raise ArtifactNotFound(f"input artifact {art.uri} does not exist")
    if art.size is not None and art.size != st.size:
        raise ArtifactChanged(f"input artifact {art.uri} is {st.size} bytes, expected {art.size}")
    return dataclasses.replace(art, version=st.version, size=st.size)


def ephemeral_request(input_bytes: int) -> str | None:
    """The ephemeral-storage request for staged inputs: 2x their size (headroom for outputs),
    rounded up to a whole MiB and never below EPHEMERAL_FLOOR; None when no sizes are known."""
    if input_bytes <= 0:
        return None
    return f"{math.ceil(max(2 * input_bytes, EPHEMERAL_FLOOR) / _MIB)}Mi"


def preview_submission(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Prepared:
    """What staging WOULD do, from the Artifacts' declared metadata alone: no pinning, no
    hashing, no store or API access of any kind (a ``dry_run=True`` preview must create and read
    nothing). Sizes come from ``Artifact.size`` where known; ``unsized`` counts the rest."""
    found = find_artifacts((args, kwargs))
    if not found:
        return Prepared(args, kwargs)
    return Prepared(
        args, kwargs, inputs=found, ephemeral_bytes=sum(a.size or 0 for a in found),
        header={"artifacts": [a.to_dict() for a in found]},
    )


def prepare_submission(
    astore: ArtifactStore, workflow: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Prepared:
    found = find_artifacts((args, kwargs))
    if not found:
        return Prepared(args, kwargs)
    pinned = {a: pin(astore, a) for a in found}

    def swap(a: Artifact) -> Artifact:
        return pinned[a]

    new_args = map_tree(args, swap, Artifact)
    new_kwargs = map_tree(kwargs, swap, Artifact)
    inputs = list(pinned.values())
    # Record intent first: every hash this workflow is about to depend on is in its reference
    # set before any job (or sweeper) can act on it.
    astore.record_refs(workflow, [a.sha256 for a in inputs if a.sha256])
    total = sum(a.size or 0 for a in inputs)
    return Prepared(
        new_args, new_kwargs, inputs=inputs, ephemeral_bytes=total,
        header={"artifacts": [a.to_dict() for a in inputs]},
    )


# --- worker side -----------------------------------------------------------------------


def _safe_name(name: str) -> str:
    return posixpath.basename(name.replace("\\", "/")) or "artifact"


def stage_artifact(astore: ArtifactStore, art: Artifact, dest: Path) -> tuple[str, int]:
    """Download one artifact to ``dest``, hashing as the bytes stream. Returns (sha256, size).

    Verifies against a known hash; learns and indexes the hash of an external reference
    (the lazy hash-on-first-staging of SPEC 3.2)."""
    problems: list[str] = []
    res = astore.resolve(art, problems)
    if res is None:
        raise cas.not_available(art, problems)
    reader, _ = store_for_uri(res.uri, astore.store)
    sha, size = cas.fetch(reader, res.uri, dest, version=res.version, expected_sha=art.sha256)
    if res.kind in ("own", "external") and art.store is None:
        # An external reference: remember where this content was seen (and its hash), so the
        # hash is known for next time and provenance can name it.
        astore.update_index(sha, size=size, add=[location("external", art.uri, res.version)])
    return sha, size


def outdir_params(func: Callable[..., Any]) -> list[str]:
    """Names of ``func``'s parameters annotated ``sk8s.OutDir``."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return []
    try:
        hints = typing.get_type_hints(func)
    except Exception:  # noqa: BLE001 — unresolvable forward references: fall back to the raw text
        hints = {}
    names = []
    for name, param in sig.parameters.items():
        ann = hints.get(name, param.annotation)
        if ann is OutDir or (isinstance(ann, str) and ann.split(".")[-1] == "OutDir"):
            names.append(name)
    return names


def _output_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file())


def collect_outputs(
    value: Any, outdir: Path, astore: ArtifactStore, *, workflow: str | None, job: str | None,
    inputs: list[str], relative_ok: bool = True,
) -> tuple[Any, list[Artifact]]:
    """Replace declared outputs in ``value`` by Artifacts, uploading them to the CAS.

    An absolute Path under ``outdir`` is a declared output. A relative Path counts (resolved
    against ``outdir``) only when ``relative_ok``, i.e. the function took an OutDir: otherwise
    a returned ``Path("x")`` is just a value."""
    produced: list[Artifact] = []
    root = outdir.resolve()

    def upload(path: Path, name: str) -> Artifact:
        art = astore.put_file(path, name=name, workflow=workflow, job=job, inputs=inputs)
        produced.append(art)
        return art

    def inside(path: Path) -> Path | None:
        if not path.is_absolute() and not relative_ok:
            return None
        candidate = path if path.is_absolute() else root / path
        resolved = candidate.resolve()
        return resolved if resolved == root or root in resolved.parents else None

    def convert(leaf: Any) -> Any:
        if isinstance(leaf, OutDir):
            leaf = leaf.path
        path = inside(leaf)
        if path is None:
            return leaf  # a Path outside the OutDir is data, not a declared output
        if path.is_dir():
            return {
                str(p.relative_to(path).as_posix()): upload(p, p.name) for p in _output_files(path)
            }
        if not path.is_file():
            raise ArtifactError(
                f"declared output {leaf} does not exist; write it under the OutDir "
                f"({root}) before returning it"
            )
        return upload(path, path.name)

    return map_tree(value, convert, (Path, OutDir)), produced


class ArtifactRun:
    """One function call with artifact inputs staged and outputs held back for the caller.

    ``call()`` stages inputs into scratch and runs the function; ``finish(value)`` then hashes
    and uploads the declared outputs and returns the value the client will receive. The split
    exists so the worker can run an ``expect=`` gate on the function's return value BETWEEN the
    two: a gated-out result uploads nothing, registers nothing in the reference set and writes
    no memo entry (only input facts - the staged inputs' learned hashes - remain). The gate sees
    the value as returned: OutDir paths are still real, readable files in scratch."""

    def __init__(
        self, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], *,
        astore: Callable[[], ArtifactStore], environ: Mapping[str, str],
    ) -> None:
        self._func, self._args, self._kwargs = func, args, kwargs
        self._astore_factory, self._environ = astore, environ
        self._out_params = outdir_params(func)
        self._found = find_artifacts((args, kwargs))
        self._scratch: Path | None = None
        self._outdir: Path | None = None
        self._store: ArtifactStore | None = None
        self._hashes: list[str] = []

    @property
    def active(self) -> bool:
        """False for a plain call (no Artifact arguments, no OutDir parameter)."""
        return bool(self._found or self._out_params)

    def call(self) -> Any:
        if not self.active:
            return self._func(*self._args, **self._kwargs)
        self._store = self._astore_factory()
        base = self._environ.get("SK8S_SCRATCH") or tempfile.gettempdir()
        Path(base).mkdir(parents=True, exist_ok=True)
        self._scratch = Path(tempfile.mkdtemp(prefix="sk8s-scratch-", dir=base))
        staged: dict[Artifact, Path] = {}
        for i, art in enumerate(self._found):
            dest = self._scratch / "in" / str(i) / _safe_name(art.filename)
            sha, _ = stage_artifact(self._store, art, dest)
            staged[art] = dest
            self._hashes.append(sha)
        new_args = map_tree(self._args, staged.__getitem__, Artifact)
        new_kwargs = map_tree(self._kwargs, staged.__getitem__, Artifact)
        self._outdir = self._scratch / "out"
        self._outdir.mkdir()
        if self._out_params:
            bound = inspect.signature(self._func).bind_partial(*new_args, **new_kwargs).arguments
            for name in self._out_params:
                if name not in bound:
                    new_kwargs[name] = OutDir(self._outdir)
        return self._func(*new_args, **new_kwargs)

    def finish(self, value: Any) -> tuple[Any, list[Artifact]]:
        """Upload the declared outputs; (value with Artifacts in place of Paths, the Artifacts)."""
        if not self.active or self._store is None or self._outdir is None:
            return value, []
        result_key = self._environ.get("SK8S_RESULT_KEY", "")
        parts = result_key.split("/")
        workflow = self._environ.get("SK8S_WORKFLOW_ID") or (parts[0] if len(parts) >= 3 else None)
        job = parts[1] if len(parts) >= 3 else None
        return collect_outputs(
            value, self._outdir, self._store, workflow=workflow, job=job,
            inputs=sorted(set(self._hashes)), relative_ok=bool(self._out_params),
        )

    def close(self) -> None:
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


@contextlib.contextmanager
def artifact_run(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], *,
    astore: Callable[[], ArtifactStore], environ: Mapping[str, str],
) -> Iterator[ArtifactRun]:
    """``with artifact_run(...) as run: value = run.call(); ...; run.finish(value)`` - scratch
    is removed on exit whatever happens."""
    run = ArtifactRun(func, args, kwargs, astore=astore, environ=environ)
    try:
        yield run
    finally:
        run.close()


def run_with_artifacts(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    astore: Callable[[], ArtifactStore],
    environ: Mapping[str, str],
) -> tuple[Any, list[Artifact]]:
    """Call and finish in one go (no gate in between). Returns (value, produced artifacts)."""
    with artifact_run(func, args, kwargs, astore=astore, environ=environ) as run:
        return run.finish(run.call())
