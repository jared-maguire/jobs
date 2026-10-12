# sk8s v3 wishlist

Brainstormed 2026-10-11, right after v2 completed (plans 1–11, PRs #70–#87).
Ideas, not commitments; no designs or plans exist yet. The theme: **v2 made jobs
reliable; v3 makes workflows cheap and durable.**

## 1. Memoization / content-addressed caching

Hash (function code + args + image digest) → check the store before submitting;
a re-run of a 50-sample pipeline after 3 failures costs 3 samples, not 50.
v1 had `state.memoize`; v2 cut it; it returns as a headline feature. Hard design
question: cache-invalidation ergonomics (when a rerun must NOT hit cache).
Secretly one feature with (2): replay needs idempotency, idempotency is the
cache.

## 2. Durable, resumable workflows

Today a detached driver is one pod babysitting futures; driver dies ⇒ workflow
dies. Event-source workflow state into the store so a restarted driver replays
to where it was. Unlocks: kill-then-resume, Kubernetes-restartable drivers,
crash-safe long pipelines.

## 3. Scheduled and event-driven workflows

`sk8s.schedule(rebuild, cron="@weekly")` → a k8s CronJob launching a workflow
driver (the weekly-index-rebuild case needs external cron today). Ambitious
version: bucket-event triggers ("new sample in gs://inbox/ ⇒ run pipeline").

## 4. Spot/preemptible pods + straggler handling

`spot=True` with automatic rescheduling on preemption; speculative re-execution
of map stragglers. Autopilot spot is ~60–90% off and batch genomics is the ideal
workload — the single biggest cost lever available, and it's mostly scheduling
policy, not new infrastructure.

## 5. GPUs as a first-class request

`requests={"gpu": 1}` mapped to Autopilot accelerator classes; GPU-flavored
bases in the image tooling; doctor checks for GPU availability/quota.

## 6. Observability: `sk8s top` + a lightweight dashboard

v1's dashboard was cut; v2's labels/anchors make a read-only one nearly free.
Per-workflow live view (pods, progress, cost-so-far from Autopilot per-pod
pricing), `fut.logs()` on running jobs, post-run cost accounting per workflow.
sk8s knows enough to answer "what did this run cost?" with real numbers.

## 7. The fast dev loop

The slowest cycle today: module-by-reference code ⇒ image rebuild ⇒ push ⇒ run.
A dev mode that ships the working package alongside the payload (the wire
already carries the function; the module tree is the same mechanism) makes
edit-run seconds instead of minutes, with real images reserved for production.

## Honorable mentions (less conviction)

(For the data plane / artifacts design, see item 8 below — promoted from this
brainstorm into a full sketch.)

- EKS adapter (committed in spirit, parked; more chore than feature).
- Multi-cluster submission ("run this where there's quota/cheapest").
- Collective ops beyond `map`: reduce / tree-aggregation.
- Per-user identity propagation / quotas for real multi-tenancy.

## 8. The data plane: artifacts (design sketch, discussed 2026-10-11)

The complement to ReadOnlyMany volumes: volumes carry *shared, big,
slow-changing* data; artifacts carry the *per-sample, flowing* inputs and
outputs that today travel as hand-managed `gs://` paths sk8s knows nothing
about. Making them first-class buys staging, provenance, cleanup, dedupe, and —
the real prize — trustworthy memoization keys, as one mechanism.

**Rulings from the brainstorm (Jared):**

- **Localization is STAGING** (the WDL/Nextflow model): declared inputs download
  to local scratch before the function runs (it receives real POSIX paths — CLI
  tools like bwa can't read `gs://`); declared outputs upload after it returns.
  FUSE mounts rejected as the default: "never performant"; at most a later
  opt-in.
- **Addressing is BOTH content-addressed and browsable**, via git's split:
  a content-addressed object store underneath, human-readable names as a thin
  refs layer on top.

**The sketch:**

```python
fq = sk8s.artifact("gs://bucket/in/s1.fq.gz")        # a reference; nothing moves

def align(fq: Path, out: sk8s.OutDir) -> sk8s.Artifact:
    subprocess.run(["bwa", "mem", IDX, fq], stdout=...)
    return out / "s1.bam"                             # declared output

bam = sk8s.run(align, fq)                             # staged in, uploaded out, tracked
bam.publish("gs://bucket/runs/2026-10-11/s1.bam")     # the human-legible refs layer
```

- Bytes live once in a CAS segment: `artifacts/cas/sha256/<hash>`, hashed in the
  upload stream (free — the worker is already streaming the file out). Identity
  = the hash ⇒ dedupe is automatic and memo keys are exact.
- `publish(path)` server-side copies/moves to a browsable path, recording the
  hash + producing job in object metadata (provenance survives both ways).
  Publishing IS the durability gesture: CAS intermediates are workflow-scoped
  and GC'd; published refs are permanent roots. (Open sub-choice: publish as
  copy vs move; start with move — the hash→location indirection is needed
  anyway.)
- External inputs import by hashing on first staging, pinned to `(uri, GCS
  generation, sha256)` — an in-place overwrite bumps the generation, so
  staleness is detectable without re-hashing and the memo cache correctly
  misses.

**The indirection layer on plain object storage** (no new stateful components —
same philosophy as v2):

- *Index = objects at deterministic keys*: per-hash JSON manifest at
  `artifacts/index/sha256/<hash>.json` (locations, size, provenance,
  checksums). Lookup is one GET at a computable key; never a LIST.
- *Concurrency = conditional writes*, which object stores now genuinely have:
  GCS generation preconditions (`ifGenerationMatch: 0` = atomic
  create-if-absent; precondition-retry for read-modify-write), S3
  `If-None-Match`/`If-Match` (landed late 2024; MinIO conforms), and the
  ConfigMap store's native `resourceVersion`. The identical-content double-create
  race resolves itself: the loser's 412 is a no-op by definition.
- *GC = mark-and-sweep, not refcounts*: workflows record referenced hashes in
  their own manifest BEFORE first use (the record-intent-first rule the clone
  machinery taught us); published refs are roots; the sweeper deletes
  unreachable CAS objects past an age floor. Structurally an extension of
  `cleaning.py`'s retention worldview.
- Platform assists: S3 computes/stores sha256 natively
  (`x-amz-checksum-sha256`); GCS gives crc32c + cheap in-bucket `rewrite` for
  publish-as-move; GCS Pub/Sub notifications adjoin this for item 3's
  bucket-event triggers.

**Spike before any code** (the ROX lesson — verify the slippery parts live):
conditional-write semantics on GCS vs S3 vs MinIO; multipart uploads vs
checksums (S3 multipart ETags are not md5s); whether `rewrite` preserves custom
metadata; staging throughput and scratch sizing on Autopilot (ephemeral-storage
requests / generic ephemeral volumes).

Interactions: wishlist item 1 (memoization) mostly collapses into "hash the
input artifacts, look up the key"; item 2's replay reads the same manifests;
item 3's triggers arrive on the same bucket notifications.

## Candidate v2.x fixes (small, could land before any v3)

- `MapFailed` should carry the successes (`e.results`), not just failures.
- Fail fast at submit when no image is configured (build-your-own doctrine means
  the default name can never pull): "no image configured — build one: …" instead
  of an `ImagePullError` timeout.
