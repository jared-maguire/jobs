"""Memoization: ``run(f, a, memo=True)`` returns the recorded result instead of running (SPEC 3.4).

    key = sha256( MEMO_SCHEMA, python minor, image, memo_salt, volumes,
                  fingerprint(f), canonical(args, kwargs), sorted(input artifact identities) )

A memo entry lives at ``artifacts/memo/<key>`` and is written ONLY after a job succeeded (its
result was fetched and decoded without error; a failed or gated run resolves with an exception
and never reaches the writer). A hit is honoured only if every output Artifact still exists
somewhere; a dangling entry is a miss and is rewritten by the recompute.

STABILITY CAVEAT - read this before trusting a hit. The key can only see what it can see:

* the function's own bytecode, constants, defaults, closure and the globals it names (plain
  values, and recursively the plain functions it calls), but NOT other modules' code, files the
  function reads at run time, environment variables, the network, or the clock;
* the image STRING (use a digest, not ``:latest``) and the ``volumes`` option by name - not the
  contents of a mutable volume;
* arguments that are builtin values (None/bool/int/float/str/bytes/list/tuple/dict/set/Path) by
  a canonical encoding; any other object by the hash of its cloudpickle bytes, which can differ
  between processes for objects containing sets of strings, ids or memory addresses. That errs
  toward a spurious MISS, never a spurious hit; but an object whose pickle is stable while its
  behaviour changes would hit wrongly;
* input Artifacts by content hash (and file name) once hashed, or by their pinned (uri,
  generation/ETag) when external - an in-place overwrite changes the pin, so it misses.

Pass ``memo_salt="v2"`` (any string) to invalidate a function's entries by hand, or omit
``memo=True``. Memoization is opt-in (decision A1): silent staleness is worse than a recompute.
"""
from __future__ import annotations

import base64
import datetime
import functools
import hashlib
import json
import logging
import pickle
import struct
import sys
import types
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Callable

import cloudpickle  # type: ignore[import-untyped]  # no py.typed marker upstream

from sk8s import cas, serialize
from sk8s.artifacts import Artifact
from sk8s.cas import ArtifactStore
from sk8s.errors import BlobNotFound, Sk8sError
from sk8s.future import JobFuture
from sk8s.staging import find_artifacts

log = logging.getLogger("sk8s.memo")

MEMO_SCHEMA = 1  # bump to invalidate every memo entry written by an older sk8s
ENTRY_SCHEMA = 1
# A memo entry carries the pickled result inline. Keep it under the ConfigMap store's blob cap
# (950,000 bytes) after base64 (x4/3) and the JSON wrapper.
MAX_INLINE_RESULT = 512 * 1024
_MAX_FP_DEPTH = 8


class _Opaque(Exception):
    """canonical() met an object it has no stable encoding for."""


def _python_minor() -> str:
    """Bytecode differs between Python versions, so the version is part of every key."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def identity(art: Artifact) -> str:
    """What an input artifact contributes to a key: its content hash if known, else its pin."""
    if art.sha256:
        return f"sha256:{art.sha256}"
    return f"ext:{art.uri}@{art.version or '?'}"


def _canonical(obj: Any, out: list[bytes]) -> None:
    """Append a stable, type-tagged encoding of ``obj`` (raises _Opaque for unknown types)."""
    if obj is None:
        out.append(b"N")
    elif isinstance(obj, bool):
        out.append(b"T" if obj else b"F")
    elif isinstance(obj, int):
        out.append(b"i%d;" % obj)
    elif isinstance(obj, float):
        out.append(b"f" + struct.pack(">d", obj))
    elif isinstance(obj, str):
        data = obj.encode()
        out.append(b"s%d:" % len(data) + data)
    elif isinstance(obj, (bytes, bytearray)):
        out.append(b"b%d:" % len(obj) + bytes(obj))
    elif isinstance(obj, Artifact):
        token = (identity(obj) + "|" + obj.filename).encode()
        out.append(b"A%d:" % len(token) + token)
    elif isinstance(obj, PurePath):
        data = str(obj).encode()
        out.append(b"p%d:" % len(data) + data)
    elif isinstance(obj, (list, tuple)):
        out.append(b"l[" if isinstance(obj, list) else b"t[")
        for item in obj:
            _canonical(item, out)
        out.append(b"]")
    elif isinstance(obj, (set, frozenset)):
        encoded = sorted(canonical(x) for x in obj)
        out.append(b"S[" + b"".join(b"%d:" % len(e) + e for e in encoded) + b"]")
    elif isinstance(obj, dict):
        pairs = sorted((canonical(k), canonical(v)) for k, v in obj.items())
        out.append(b"d{" + b"".join(b"%d:" % len(k) + k + b"%d:" % len(v) + v for k, v in pairs) + b"}")
    else:
        raise _Opaque(type(obj).__name__)


def canonical(obj: Any) -> bytes:
    out: list[bytes] = []
    _canonical(obj, out)
    return b"".join(out)


def canonical_or_pickle(obj: Any) -> bytes:
    """Canonical bytes for builtin values; otherwise the cloudpickle bytes (see the module caveat)."""
    try:
        return canonical(obj)
    except _Opaque:
        try:
            return b"P" + bytes(cloudpickle.dumps(obj))
        except Exception as exc:  # noqa: BLE001
            raise Sk8sError(
                f"cannot compute a memo key: argument of type {type(obj).__name__} cannot be "
                f"pickled ({exc}); drop memo=True or pass a picklable value"
            ) from exc


# --- function fingerprints -----------------------------------------------------------------


def _fp_code(code: types.CodeType, h: Any, func_globals: dict[str, Any], seen: set[int], depth: int) -> None:
    h.update(code.co_code)
    h.update(canonical_or_pickle((
        code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags,
        code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars,
    )))
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _fp_code(const, h, func_globals, seen, depth)
        else:
            h.update(canonical_or_pickle(const))
    for name in code.co_names:
        if name in func_globals:
            _fp_global(name, func_globals[name], h, seen, depth)


def _fp_global(name: str, value: Any, h: Any, seen: set[int], depth: int) -> None:
    h.update(b"G" + name.encode())
    if isinstance(value, types.ModuleType):
        h.update(b"M" + value.__name__.encode() + str(getattr(value, "__version__", "")).encode())
    elif isinstance(value, (types.FunctionType,)):
        h.update(_fp_function(value, seen, depth + 1).encode())
    elif isinstance(value, type):
        h.update(b"C" + f"{value.__module__}.{value.__qualname__}".encode())
    else:
        try:
            h.update(canonical(value))
        except _Opaque:
            h.update(b"O" + type(value).__qualname__.encode())  # an object: only its type is visible


def _fp_function(func: Callable[..., Any], seen: set[int], depth: int) -> str:
    h = hashlib.sha256()
    if id(func) in seen or depth > _MAX_FP_DEPTH:
        return "recursive"
    seen.add(id(func))
    if isinstance(func, functools.partial):
        h.update(b"partial" + _fp_function(func.func, seen, depth).encode())
        h.update(canonical_or_pickle((func.args, func.keywords)))
        return h.hexdigest()
    code = getattr(func, "__code__", None)
    if isinstance(code, types.CodeType):
        h.update(f"{getattr(func, '__module__', '')}.{getattr(func, '__qualname__', '')}".encode())
        _fp_code(code, h, getattr(func, "__globals__", {}), seen, depth)
        h.update(canonical_or_pickle(getattr(func, "__defaults__", None)))
        h.update(canonical_or_pickle(getattr(func, "__kwdefaults__", None)))
        for cell in getattr(func, "__closure__", None) or ():
            try:
                contents = cell.cell_contents
            except ValueError:  # an empty cell
                continue
            if isinstance(contents, types.FunctionType):
                h.update(_fp_function(contents, seen, depth + 1).encode())
            else:
                h.update(canonical_or_pickle(contents))
        return h.hexdigest()
    # A builtin, a bound method, a callable object, a class: the pickle is all there is.
    h.update(canonical_or_pickle(func))
    return h.hexdigest()


def function_fingerprint(func: Callable[..., Any]) -> str:
    """Hash of everything about ``func`` the key can see (see the module caveat). Does not
    depend on file names or line numbers, so moving code around does not invalidate entries."""
    return _fp_function(func, set(), 0)


def memo_key(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    image: str,
    salt: str | None = None,
    volumes: Any = None,
    expect: Callable[..., Any] | None = None,
) -> str:
    """The memo key (a sha256 hex). ``args`` must already have external Artifacts pinned.

    ``expect`` (the QC gate) is part of the question: an entry is only ever written for a run
    that PASSED its gate, so a hit is valid only for the same gate - a stricter ``expect=``
    must not be answered by a result that was checked against a laxer one."""
    h = hashlib.sha256()
    header = ("sk8s-memo", MEMO_SCHEMA, _python_minor(), image, salt or "")
    h.update(canonical(header))
    h.update(canonical_or_pickle(volumes if volumes is None else _canon_volumes(volumes)))
    h.update(function_fingerprint(func).encode())
    h.update(b"expect:" + (function_fingerprint(expect).encode() if expect is not None else b"-"))
    h.update(canonical_or_pickle((args, kwargs)))
    h.update(canonical(sorted(identity(a) for a in find_artifacts((args, kwargs)))))
    return h.hexdigest()


def _canon_volumes(volumes: Any) -> Any:
    if isinstance(volumes, dict):
        return {str(k): str(v) for k, v in volumes.items()}
    if isinstance(volumes, (list, tuple)):
        return sorted(str(v) for v in volumes)
    return str(volumes)


# --- entries ---------------------------------------------------------------------------------


@dataclass
class MemoHit:
    value: Any
    key: str
    outputs: list[Artifact]


def _outputs_available(astore: ArtifactStore, outputs: list[Artifact]) -> bool:
    return all(astore.resolve(a) is not None for a in outputs)


def lookup(astore: ArtifactStore, key: str) -> MemoHit | None:
    """The recorded result for ``key`` if its entry is intact and every output still exists."""
    try:
        raw, _ = astore.store.read_with_version(cas.memo_entry_key(key))
    except BlobNotFound:
        return None
    try:
        entry = json.loads(raw)
        if entry.get("schema") != ENTRY_SCHEMA or entry.get("key") != key:
            raise ValueError("unexpected entry")
        value = serialize.decode_result(base64.b64decode(entry["result_b64"]))
        outputs = [Artifact.from_dict(d) for d in entry.get("outputs", [])]
    except (ValueError, KeyError, TypeError, pickle.UnpicklingError, Sk8sError) as exc:
        log.warning("ignoring unreadable memo entry %s: %s", key[:12], exc)
        return None
    except Exception as exc:  # noqa: BLE001 — e.g. a result type this client cannot import: recompute
        log.warning("ignoring memo entry %s that cannot be decoded here: %r", key[:12], exc)
        return None
    if not _outputs_available(astore, outputs):
        log.info("memo entry %s is dangling (an output artifact was collected); recomputing", key[:12])
        return None
    return MemoHit(value=value, key=key, outputs=outputs)


def write(astore: ArtifactStore, key: str, value: Any, *, function: str, produced_by: str | None,
          image: str, inputs: list[Artifact]) -> bool:
    """Record a SUCCESSFUL result. Returns False (and logs) if it is too large to inline.
    Call only from the success path."""
    blob = serialize.encode_result(value)
    if len(blob) > MAX_INLINE_RESULT:
        log.warning(
            "not memoizing %s: its result is %d bytes (limit %d); return the data as an "
            "OutDir file / Artifact instead", function, len(blob), MAX_INLINE_RESULT,
        )
        return False
    outputs = find_artifacts(value)
    entry: dict[str, Any] = {
        "schema": ENTRY_SCHEMA, "key": key,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "function": function, "image": image, "produced_by": produced_by,
        "inputs": sorted(identity(a) for a in inputs),
        "outputs": [a.to_dict() for a in outputs],
        "result_b64": base64.b64encode(blob).decode(),
    }
    body = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()

    from sk8s.store.retry import update_blob

    def mutate(current: bytes | None) -> bytes | None:
        if current is not None:
            try:
                old = json.loads(current)
                if old.get("outputs") == entry["outputs"] and old.get("result_b64") == entry["result_b64"]:
                    return None  # same result already recorded: keep its (older) created_at
            except ValueError:
                pass  # garbled: replace
        return body

    update_blob(astore.store, cas.memo_entry_key(key), mutate)
    return True


def adopt_outputs(astore: ArtifactStore, workflow: str, hit: MemoHit) -> None:
    """A memo hit makes this workflow depend on the outputs: record them (intent first)."""
    hashes = [a.sha256 for a in hit.outputs if a.sha256]
    if hashes:
        astore.record_refs(workflow, hashes)


class _NoBackend:
    def logs(self, job_name: str) -> str:
        return ""

    def cancel(self, job_name: str) -> bool:
        return False


def hit_future(workflow: str, hit: MemoHit) -> JobFuture:
    """An already-resolved future for a memo hit: no payload, no Job, no pod."""
    fut = JobFuture(f"memo-{hit.key[:10]}", workflow, backend=_NoBackend())
    fut.memo_hit = True
    fut.set_result(hit.value)
    return fut
