# Artifacts: staging, the CAS, publish, memoization

Artifacts are files that flow between jobs without you managing `gs://` paths by hand. Bytes
live once in a content-addressed store (CAS) under your configured store; a job gets real
files in, hands real files out, and `sk8s` records what produced what. This is the data plane
of the v3 design (`SPEC-DRAFT.md` section 3); everything below is shipped and tested, with the
gaps listed at the end.

```python
import sk8s
from pathlib import Path

fq = sk8s.artifact("gs://lab/runs/oct/s1.fq.gz")          # a reference; nothing moves

def align(fq: Path, out: sk8s.OutDir) -> Path:             # fq arrives as a real local file
    run_bwa(fq, out / "s1.bam")
    return out / "s1.bam"                                  # declared output

bam = sk8s.run(align, fq, memo=True)                       # -> sk8s.Artifact
bam.publish("gs://lab/results/oct/s1.bam")                 # permanent, human-legible home
bam.download("s1.bam")                                     # or copy it locally
sk8s.run(align, fq, memo=True)                             # again: answered from the memo, no pod
```

## Referencing inputs

`sk8s.artifact(uri)` accepts `gs://`, `s3://`, an absolute path or `local:/path`. At submit the
object is **pinned** to its current GCS generation / S3 ETag / local stat, and its size is
recorded (so the pod's `ephemeral-storage` can be sized). A missing object fails the submit
with `ArtifactNotFound` before any job exists. In the pod the bytes are hashed *as they
download*; if the object was overwritten in place after the pin, the run is refused with
`ArtifactChanged` (a changed input is a different artifact). `sk8s.artifact(uri, sha256=...)`
asserts the expected content up front.

Artifacts returned by earlier jobs are passed the same way (`sk8s.run(f, previous_result)`);
they are found anywhere inside lists, tuples, dicts and sets of the arguments. A custom
object or dataclass holding an Artifact is **not** walked.

## What the function sees

Every `Artifact` argument is replaced by a `pathlib.Path` to a file in pod-local scratch,
named like the original (`s1.fq.gz`). A parameter annotated `sk8s.OutDir` receives an empty
scratch directory; `out / "name"` is an ordinary `Path` inside it.

What you return decides what is uploaded: an absolute `Path` under the OutDir, or a relative
one when the function takes an OutDir, is an output - alone or inside lists / tuples / dicts.
Returning the `OutDir` itself (or a sub-directory) declares every file in it
(`{relative path: Artifact}`). A `Path` outside the OutDir is just data. Each output is hashed,
recorded in the workflow's reference set **before** the bytes are uploaded, uploaded only if
the CAS does not already hold that hash, and replaced by an `Artifact` in the result you get
back. A declared output that was never written fails the job.

Pod sizing: the ephemeral-storage request is `2 x` the known input bytes (room for outputs),
**floored at 1 GiB** (GKE Autopilot sets the ephemeral *limit* equal to the request, so a tiny
request would evict the pod), and only when you did not set `ephemeral-storage` yourself.
Generic ephemeral volumes (for inputs beyond ~50 GiB) are **not implemented**: sk8s logs a
warning, and you set `requests={"ephemeral-storage": ...}` yourself.

## Composition with `expect=`, `pilot=` and `dry_run=`

- **`expect=` gates BEFORE anything is uploaded.** The gate receives the function's return value
  as returned: an `OutDir` path is a real, readable local file (open it and check it), not yet an
  `Artifact`. Only if the gate passes are the outputs hashed, recorded and uploaded and the result
  stored. A gated-out run leaves no CAS object, no reference-set entry for its outputs and no memo
  entry; the only trace is the staged inputs' learned hashes (facts about the inputs).
- **Memo keys include the gate.** An entry exists only for a run that passed its `expect=`, so a hit
  is valid only for the same gate; a different or stricter `expect=` re-runs.
- **`pilot=`** needs nothing special: pilot items are ordinary jobs and may be memo hits.
- **`dry_run=True` touches nothing**: no pin (no stat), no hash, no reference-set write, no memo
  lookup. It reflects staging from the Artifacts' *declared* metadata: `DryRun.n_artifacts`,
  `artifact_input_bytes`, the `ephemeral-storage` request in the job body, and notes saying which
  inputs have no declared size. `memo=True` with `batch_size > 1` is refused in a preview too.
- Wire format: unchanged envelope v2. Artifact descriptors ride in an extra payload/result *header*
  key; the pickled body is byte-identical to a payload without artifacts. A worker that predates
  artifacts cannot unpickle `Artifact`/`OutDir` and fails loudly before running anything.

## Layout

```
<store>/artifacts/cas/sha256/<hh>/<hash>            the bytes
<store>/artifacts/index/sha256/<hh>/<hash>.json     size, locations, provenance, last_referenced
<store>/artifacts/memo/<key>                        memo entries
<store>/workflows/<wf>/artifacts.json               the workflow's references (GC roots)
<store>/workflows/<wf>/<job>/artifacts.json         one job's shard of the same set
```

Index and manifest updates are optimistic read-modify-writes on the backend's precondition
(GCS `ifGenerationMatch`, S3/MinIO `If-None-Match`/`If-Match`, ConfigMap `resourceVersion`,
a flock'd compare-and-swap locally) through one bounded, jittered retry helper; every update
is an idempotent set merge. The index records *where* a hash lives (`cas`, `published`,
`external` locations), so a handle keeps resolving after the CAS copy is moved away.

## Publish

`artifact.publish(uri)` gives the artifact a permanent home and returns the `Artifact` that
lives there. It **moves** by default: the destination becomes the recorded location and the
CAS copy is retired (one copy). `publish(uri, copy=True)` keeps both (and the kept CAS copy is
then protected from GC). Server-side where the stores allow it (GCS `rewrite`, S3 copy, local
copy), otherwise streamed with a hash check. The destination object carries
`sk8s-artifact-sha256` and `sk8s-produced-by`; the metadata is sent *with* the content (a later
content write without it would wipe it on GCS) and read back before anything is retired.

Publish never overwrites a different object (`PublishConflict`), treats re-publishing the same
content to the same name as a no-op, and only ever retires a CAS copy: an external reference or
an earlier published ref is copied from, never deleted.

## Memoization

`memo=True` on `run` / `submit` / `map` / `starmap` (or `memo = true` in the config file, or
`SK8S_MEMO=1`) looks the call up before submitting and returns the recorded result on a hit:
no payload, no Job, no pod. `memo_salt="v2"` invalidates a function's entries by hand. It is
**opt-in** (silent staleness is worse than a recompute) and not supported with `batch_size > 1`.

The key is a sha256 of: the memo schema version, the Python minor version, the **image string**,
`memo_salt`, the `volumes` option, a fingerprint of the function, the arguments, and the sorted
identities of the input artifacts (content hash once known, otherwise the pinned URI+version).
An entry is written only after the job succeeded - failed, cancelled and pod-lost runs never
write one - and a hit is honoured only if every output artifact still exists somewhere
(otherwise it recomputes and rewrites the entry).

**Read this before trusting a hit.** The key sees the function's bytecode, constants, defaults,
closure, the globals it names and (recursively) the plain functions it calls - not other
modules' code, files read at run time, environment variables, the network, the clock, or what a
mutable volume contains. Use an image *digest*, not `:latest`. Builtin arguments (numbers,
strings, bytes, lists, tuples, dicts, sets, `Path`) use a canonical encoding that is stable
across processes and hash seeds; any other object falls back to its cloudpickle bytes, which can
differ between processes (a spurious *miss*, never a false hit - but an object whose pickle is
stable while its behaviour changes would hit wrongly). When in doubt, bump `memo_salt`.

## Garbage collection

`sk8s clean` (and `sk8s.clean()`) also sweeps the CAS: mark every hash referenced by any
`workflows/**/artifacts.json`, any memo entry younger than 7 days, and any CAS object that also
has a published location; delete the rest if both the object and its index entry are older than
300 seconds. Deleting a workflow (`sk8s kill`, `clean`, or `Cluster.close()` auto-cleanup) deletes
its reference set, so its outputs become collectable unless published or memoized. Anything
unreadable (a corrupt manifest, a listing error) aborts the sweep before the first delete.
Workers never sweep the CAS. **Publish what you want to keep.**

## Backends, as verified

| | create-if-absent | compare-and-swap | metadata with content | server-side copy |
|---|---|---|---|---|
| GCS | `ifGenerationMatch=0` (live) | `ifGenerationMatch=N` (live) | upload / `rewrite` (live) | `rewrite` (live) |
| ConfigMap | create-only (live) | merge-patch with `resourceVersion` (live) | not supported | - |
| MinIO | `If-None-Match` (live) | `If-Match` (live) | upload / copy (live) | `CopyObject` (live) |
| AWS S3 | same calls - **not verified** | same - **not verified** | same - **not verified** | same - **not verified** |
| local | hard link | flock'd read-compare-replace | sidecar file | copy |

"Live" means exercised against the real service by `tests/integration/test_artifacts*.py`
(GCS: bucket `jared-genome-analysis-sk8s`; MinIO: RELEASE.2026-09-22; ConfigMap: minikube
v1.35.1).

## Not built (yet)

Generic ephemeral volumes for very large inputs; a node-local CAS cache (every pod downloads its
inputs); `Artifact.open()`; memoization with `batch_size > 1`; walking arbitrary objects for
Artifacts; cross-store artifacts (an Artifact is resolved against the store that minted it);
pin/hash caching keyed by (uri, generation) (external inputs are re-hashed on each staging);
workflow-IR integration (task cards, equivalence declarations). `OutDir` is not supported
inside `batch_size > 1` jobs.
