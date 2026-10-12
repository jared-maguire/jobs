# Artifacts, staging, publish, memoization

Files flow between jobs as `sk8s.Artifact`s. Bytes live once in a content-addressed store
(CAS) under the configured store (`k8s` ConfigMaps for small dev files, `gs://`/`s3://` for
real data). Narrative version: `docs/artifacts.md`.

```python
fq  = sk8s.artifact("gs://lab/in/s1.fq.gz")             # reference only; pinned to the object's generation at submit
def align(fq: Path, out: sk8s.OutDir) -> Path:          # fq is a REAL local file; out is an empty scratch dir
    ...; return out / "s1.bam"                           # returned OutDir paths are uploaded to the CAS
bam = sk8s.run(align, fq, memo=True)                     # -> Artifact (hashed while uploading)
bam.publish("gs://lab/results/s1.bam")                   # moves (CAS copy retired); copy=True keeps both
bam.download("s1.bam")                                   # hash-verified local copy
```

## Rules that prevent mistakes

- **Artifact args become `Path`s in the pod, named like the original.** Found anywhere in
  lists/tuples/dicts/sets of the arguments; NOT inside custom objects/dataclasses.
- **Outputs are what you RETURN**: an absolute `Path` under the OutDir (relative too, if the
  function takes an `sk8s.OutDir`), alone or in list/tuple/dict; returning the OutDir itself
  uploads every file (`{relpath: Artifact}`). Other `Path`s are plain data. A declared output
  that was not written fails the job.
- **Missing input** -> `ArtifactNotFound` at submit (no job created). **Input overwritten in
  place after the pin** (or bytes not matching a given `sha256=`) -> `ArtifactChanged` in the pod.
- **`ephemeral-storage` request = 2x known input bytes, floor 1 GiB**, unless you pass your own
  in `requests=`/`limits=`. Inputs beyond ~50 GiB: no generic ephemeral volume yet -> set the
  request yourself or the pod is evicted.
- **Publish is how you keep things.** CAS objects are workflow-scoped: killing/expiring a
  workflow, then `sk8s clean`, collects them after 300 s unless published (copy=True keeps the
  CAS copy too) or referenced by a memo entry younger than 7 days.
- `publish` never overwrites a different object (`PublishConflict`); re-publishing the same
  content to the same name is a no-op; it never deletes an external input or an earlier publish.
- **Memo is opt-in** (`memo=True`, `SK8S_MEMO=1`, `memo = true`); `memo_salt="v2"` invalidates;
  not with `batch_size > 1`. Failed/cancelled/lost runs never write an entry. A hit needs every
  output artifact to still exist, else it recomputes. The key sees the function's bytecode,
  closure, named globals and called helpers, args, input artifact hashes, the image STRING and
  python minor - NOT other modules' code, runtime file reads, env vars, mutable volume contents.
  Use an image digest, not `:latest`; when unsure, bump `memo_salt`.
- A memo hit is a resolved future with `fut.memo_hit == True`; no pod, no Job.
- The worker image needs the `gcs` extra for `gs://` locations (`docker/Dockerfile.dev
  --build-arg EXTRAS=s3,gcs`, or `Dockerfile.worker`).
- Build a worker image from THIS code under its own tag; never overwrite a shared one.

| Symptom | Cause -> fix |
|---|---|
| `ArtifactNotFound` at submit | the URI does not exist (or no creds client-side) |
| `ArtifactChanged` in the pod | input overwritten after submit pinned it -> resubmit |
| `PublishConflict` | destination holds different bytes -> pick another name |
| memo never hits | the key changed (code/closure/global/arg/image/salt) or an output was collected -> publish outputs you want to keep |
| memo hit returns stale data | something the key cannot see changed (a volume, a library, a runtime file) -> bump `memo_salt` |
| output vanished after a day | unpublished CAS objects are collectable after the workflow goes -> `publish` |
