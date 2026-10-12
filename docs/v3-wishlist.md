# sk8s v3 wishlist

Brainstormed 2026-10-11, right after v2 completed (plans 1–11, PRs #70–#87).
Ideas, not commitments; no designs or plans exist yet. The theme: **v2 made jobs
reliable; v3 makes workflows cheap and durable.**

## The target interaction (the bar-napkin demo, 2026-10-11 — "YES. I want THAT.")

Every v3 feature is judged by whether it moves us toward these twelve lines:

```
You:  computer, align the new cohort in gs://lab/runs/oct/ against GRCh38
      and give me per-sample variant counts.

C:    Found 48 samples (FASTQ, ~12GB each). Plan: fastqc → bwa-mem →
      dedup → haplotypecaller → count. 240 jobs, est. $31, ~2h.
      Pilot on 3 samples first? [plan: wf-7f3a, diffable]

You:  yes, go.

C:    Pilot green — QC: 97% mapped, dup rate 8%. Running the other 45.

      ... (you close the laptop, go to the bar) ...

You:  computer, status?

C:    44/48 done. S23 failed QC (61% mapped — contamination?),
      quarantined, everything else on track. $26 so far.

You:  drop S23, finish up, publish counts to gs://lab/results/oct/.

C:    Done. 47 samples published, provenance attached —
      counts.tsv ← sha256:9c41… ← [pipeline wf-7f3a, GRCh38@2024,
      bwa@0.7.17]. Re-running tomorrow would cost $0 — it's all cached.
```

Decomposition — every line is a wishlist item: "Found 48 samples" = the data
catalog (10.1); the plan + estimate + diffable wf-7f3a = IR, dry-run, manifests
(9); "pilot first" = answer verification (10.4); stage names = task cards
(10.2); close-the-laptop = durable workflows (2); "status?" = explain (9);
S23 quarantined = QC gates + typed semantic failure (10.4); "drop S23, finish"
= dynamic control of a running workflow; publish + provenance = artifacts/CAS +
refs (8); "$0 tomorrow" = memoization as execution semantics (1, first
principle); the $31/$26 numbers = cost accounting + budgets (6, 9); the whole
conversation = the resident computer + autonomy policy (10.6).

## The first principle (candidate first sentence of any v3 spec)

> **Your outputs are supposed to be pure functions of your inputs — that's what
> "science" means.**

(Landed in discussion 2026-10-11; Jared: felt for years, never put to words.)
A methods section is a derivation written in prose; "couldn't reproduce" means
the function had hidden inputs; the replication crisis is, in build-system
terms, undeclared inputs and non-hermetic builds. Content-addressed
materialization (item 8, greenfield inversion #2) is not a metaphor borrowed
from software — it is what the methods section always wanted to be: executable,
with provenance you hand a reviewer instead of a promise.

Design consequences: execution IS cache-miss materialization ("should this be
cacheable?" is never a debate again); impurity becomes the thing that must be
*declared* (like `unsafe` blocks) rather than purity being the thing achieved
by discipline; a result is an artifact hash with a derivation trail.

Context, from the greenfield thought experiment ("rebuild from scratch with the
Star Trek goal primary"): keep v2's engine room (jobs, stores, RBAC, GKE,
doctor, typed errors, plan-first), rebuild the deck — the system lives in the
cluster with clients as stateless views; execution is cache-miss handling over
a content-addressed world; cloudpickle demoted to dev-mode sugar in favor of
declared task-card interfaces; contracts/budgets/policy in the kernel; the
event log as the primary artifact. The two breaks evolution can't deliver
politely: build-system semantics and no-pickle boundaries — and the cheapest
moment to make them is before v2 grows an installed base. Keep
`sk8s.run(lambda: 42)` alive as the dev-mode hello-world, whatever happens
underneath.

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

## 9. Agent-native sk8s (Jared's #1, discussed 2026-10-11)

sk8s as the substrate coding agents use to build and execute complex workflows —
"people don't write code anymore." v2 did parts of this by accident (typed
errors with fix-hints, doctor, plan-first config, SK8S_CONFIG isolation, the
in-repo skill); v3 makes it a design principle:

> **Every operation previewable, every state explainable in one call, every
> failure naming its fix, every blast radius bounded.**

The concrete surface:

- **One-call situational awareness:** `sk8s explain <workflow>` (+ `--json` on
  every CLI command) — the whole tree in one response: driver state, job phases,
  pod events already interpreted, logs pointers, artifacts, cost so far, what's
  blocking what. All state reconstructible from cluster + store, never from a
  process (an agent with zero context rebuilds the picture in one call).
- **Previewability:** `submit(..., dry_run=True)` returning the built Job,
  resolved image, store destination, cost estimate; reviewable workflow
  manifests ("what this pipeline WILL do") that diff in PRs before anything
  touches the cluster — the trust gate for agent-built infrastructure.
- **Bounded blast radius:** `budget=` per workflow (max pods / cpu-hours /
  estimated spend, typed error at the limit); `sk8s sandbox` = ephemeral
  namespace + config + budget, auto-reaped (the isolation pattern every agent in
  the v2 build maintained by hand, as one command).
- **Fast inner loop:** the dev-loop item (ship the module tree with the
  payload); `LocalCluster` promoted to a faithful simulator with failure
  injection (test error handling with no cluster); idempotent re-execution via
  memoization (an agent re-running a crashed script double-executes nothing).
- **The skill ships with the package and is CI-tested** (the retrieval-test loop
  from plan 11, institutionalized); possibly `sk8s skill install`; possibly an
  MCP server, though CLI-with-`--json` + the skill may serve coding agents
  better.

### Radical-option appendix: is Python even the right language?

The irony is noted: sk8s exists because workflow DSLs are miserable for humans —
and agents invert that trade (learning curves cost them nothing; verifiability
before execution is what they need most). The direction under consideration:

**Split the two jobs Python currently does.**

- *Leaf compute* (the function that runs bwa, the numpy transform) stays Python
  in a container — LLM training distribution dominates there, and leaf code
  gains nothing from static verifiability.
- *Orchestration* stops being a running Python process and becomes **data**: a
  workflow IR — versioned, typed, budget-annotated graphs with dynamic
  constructs. Python becomes ONE frontend that emits the IR (the PyTorch /
  Terraform-CDK move); agents may emit IR directly. The artifact reaching the
  cluster is checkable data, not code with opaque control flow.

Much of this wishlist collapses into the IR: durable/resumable workflows
(item 2) = an interpreter over data with an event log, replayable by
construction; memoization (item 1) = per-node; manifests/dry-run/explain =
views of the IR; cost estimation = a fold over it.

The hard problem is **dynamic workflows** (data-dependent fan-out, loops —
the reason DSLs were fled in the first place). Two proven escapes to evaluate:

- **Temporal's model:** orchestration stays code, but deterministic code whose
  effects all route through the engine and are event-logged — durability and
  resume by replay, full expressiveness, no static DAG.
- **Starlark** as the orchestration dialect: Python *syntax* (LLM generation
  quality carries over) but hermetic and deterministic by construction —
  evaluates to the IR, and its determinism makes replay sound.

Working bet on the underlying empirical question (familiar-but-uncheckable vs
constrained-but-verifiable): **verification-loop quality beats generation
familiarity** — agents iterate, and a verifier that answers in seconds converts
generation errors from cluster-hours to nothing. Shape: **sk8s-IR + a durable
in-cluster interpreter (the driver's successor) + two frontends** (eager Python
for humans; Starlark-or-direct-IR for agents). v2's job layer unchanged
underneath — a new floor, not a teardown.

## 10. The Star Trek goal (the organizing frame — Jared, 2026-10-11)

The north star: *"Computer, make an analysis pipeline that looks at this data
and produces these answers"* — and agents go do it, unattended, on workloads
from millions of tiny tasks to 100GB BAM files. Items 8 and 9 are the floor;
this is the building. Work backwards from the loop the computer must run —
**understand the data → plan → build → execute → verify the answer → report** —
and each stage names a missing layer. Each sub-item is independently shippable.

1. **Self-describing data (the catalog).** `sk8s data describe <uri>` → sniffed
   format (BAM/VCF headers, parquet schemas), sizes, lineage to producing
   workflows, user-attached semantics. Genomics formats carry half of this in
   their headers already; surface it. Without it, every agent run burns its
   budget on reconnaissance.
2. **A verified capability library (task cards).** Typed, contract-carrying task
   definitions (bwa, samtools, GATK, …): input/output artifact types, providing
   image, resource model. IR nodes with contracts — agents compose known-good
   blocks and write novel leaf code only where it's genuinely novel. Prior art:
   nf-core modules; difference: machine-checkable contracts the IR verifier
   enforces. The skill idea graduated from documentation to components.
3. **Resource autosizing.** Cheap version: auto-resubmit at 2× on OOM (we
   already classify OOM kills). Real version: record input-size →
   peak-RSS/runtime per task card from every run (the data already flows past
   us), fit, predict requests with a margin. The #1 human babysitting task,
   automated; with spot (item 4) it is most of the cost story.
4. **Verification of ANSWERS, not exit codes.** *(v2.x first cut shipped on `feat/qc`: `expect=` job
   option + typed `ExpectationFailed`, and `pilot=n` on the map family with typed `PilotFailed`.
   Still open: QC gates as IR nodes, golden-set runs, provenance reports.)* QC gates as first-class IR nodes
   (expectations: % mapped > X — semantic failure, not operational); golden-set
   runs (validate the built pipeline against known answers before the real
   cohort); `pipeline.pilot(n=3)` as a verb (the baseline agent reinvented this
   by instinct); reports with provenance — every figure traceable to artifact
   hashes. "Computer, show your work" is part of the UI.
5. **Both scale regimes natively.** Tiny tasks: a second executor tier
   (resident worker pods pulling a queue; `map` picks the tier adaptively —
   k8s Jobs have a seconds-long pod floor). Huge files: node-local
   content-addressed cache on local SSD (a 100GB BAM downloads once per node,
   not once per task) + data-aware scheduling (prefer the node already holding
   the artifact) — the HPC trick object storage took away, returned by the CAS.
6. **The resident "computer" — mostly NOT sk8s's job.** The conversational
   layer is an agent harness; sk8s owes it: item 9's surface, event-driven
   wakeups (bucket trigger → agent evaluates new data; failure → agent
   investigates within policy), the IR interpreter's **agent-in-the-driver
   option** (mid-run dynamic decisions made by an LLM call, event-logged and
   replayable like everything else), and an explicit **autonomy policy** —
   what the computer may do unprompted (spend ceilings, never delete published
   artifacts, never leave the project). Trust is a config file, not a vibe.

**Removals this frame justifies:**
- Laptop-tethered execution as a *primary* model: everything becomes a durable
  workflow; eager `submit` is sugar over a one-node one (one code path; the
  "closed my laptop, lost my work" failure class dies).
- `serve`/services: keep, freeze — peripheral to the mission.
- The ConfigMap store demoted in spirit to dev/simulator-only; real deployments
  assume an object store (the 950KB caveat class leaves the main path).

## Candidate v2.x fixes (small, could land before any v3)

- **The conda image-build path should install sk8s from the local checkout, not
  PyPI** (Jared, 2026-10-12 morning). `sk8s image build --conda ...` currently
  pip-installs sk8s from PyPI inside the miniforge base — a documented dead end
  pre-release and a violation of the build-your-own doctrine always. Fix: build
  a wheel from the checkout on the fly and COPY it in. Payoff: bioconda tools
  (bwa/samtools/bcftools) become a pure-Python one-liner via `images.build`,
  and the NA12878 demo loses its Dockerfile entirely.

- Done in v2.x: `MapFailed` carries the successes (`e.results`, `{index: value}`).
- Done in v2.x: fail fast at submit when no image is configured. Build-your-own doctrine
  means the default name can never pull, so `run`/`submit`/`map` raise `ImageNotConfigured`
  (with the build commands) before creating anything, instead of an `ImagePullError` timeout.
