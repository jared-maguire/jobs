# sk8s module usage

## Jobs and futures

```python
import sk8s

sk8s.run(f, *args)                 # submit + wait, returns the value (or re-raises)
fut = sk8s.submit(f, *args)        # JobFuture (a real concurrent.futures.Future)
vals = sk8s.map(f, items)          # fan out, ordered results; raises MapFailed on any failure
vals = sk8s.starmap(f, tuples)     # map with argument unpacking
futs = sk8s.submit_map(f, items)   # the futures, when you need per-item control
```

Job options (kwargs): `image`, `requests`, `limits` (e.g. `requests={"cpu":
"500m", "memory": "1Gi"}`), `volumes`, `backoff_limit`, `service_account`,
`name_prefix`, `privileged`, `pull_policy`, `keep_results`, `spot` (preemptible
nodes; requires `backoff_limit>=1`), `node_selector` (a dict merged into the pod's
`nodeSelector` — hardware pinning, see "Scratch space and hardware"), `expect` (a QC gate run
in the worker on the result: `None`/`True` passes, a string or a raise fails with
`ExpectationFailed`; see references/advanced.md), `dry_run` (see "Previewing and
inspecting"). `map`/`starmap`/`submit_map` add
`batch_size` (N items per job), `max_concurrent`, `pilot` (run the first N items,
rest only if all N are green; else `PilotFailed`), and `map`/`starmap` also `timeout` — one overall
deadline for the whole map, **not** a per-job kill; `submit`/`submit_map` accept
and ignore `timeout` (bound the wait at `fut.result(timeout=...)` instead).
`namespace` must equal the cluster's namespace (`ValueError` otherwise) — switch
namespaces via config, not per-job.
`timeout`, `expect`, `pilot` and `budget` are reserved option names like `image`: they are consumed by
sk8s and never forwarded to your function (`serve()` rejects `expect=` with `ValueError`).

`sk8s.configure(namespace=..., image=..., store=...)` overrides config for the
process; `sk8s.configure()` resets; `sk8s.configure(cluster=sk8s.LocalCluster())`
runs jobs in-process (no cluster — great for testing pipeline logic).

Pickling rule: functions defined in the running script (`__main__`) or notebook
pickle **by value**; functions imported from a module pickle **by reference**,
so the worker image must contain that module. Run pipeline scripts directly, or
bake your package into the image.

## Errors (all exported from `sk8s`)

- Your function raised → the **same exception type** re-raises locally, with
  `__cause__` = `RemoteTraceback` (full remote trace). Unreconstructible types →
  `RemoteException` (`.type_name`, `.message`, `.tb_text`).
- `MapFailed` — `.failures` is `[(index, exception), ...]`, `.results` is
  `{index: value}` for every item that succeeded, `.total` the item count. For
  streaming or per-item timeouts see the per-future pattern in references/advanced.md.
- `ExpectationFailed` — the function returned but its `expect=` check rejected the
  result (semantic failure, not operational). `.message` is the expectation's
  explanation, `.result_repr` a truncated repr of the offending result;
  `__cause__` is a `RemoteTraceback`. In a `map` it is a per-item failure inside
  `MapFailed` (whose message counts `N failed their expectation, M errored`).
- `BudgetExceeded` — a submission's *estimated* cost is over its `Budget`; nothing was
  created. `.violations` (one sentence per limit), `.verdict` (dict). See Budgets below.
- `PilotFailed` — a `pilot=` run failed; the remainder was **not submitted**.
  `.failures` is `[(index, exception), ...]` over the pilot items, `.results` is
  `{index: value}` for the pilot items that succeeded, `.pilot` how many ran, `.total`
  how many were given.
- `ResultDecodeError` — job *succeeded* but the client can't unpickle the result
  (missing module locally). `JobTimeout` (also a `TimeoutError`); `PodFailed`;
  `ImagePullError` (an image IS set but nodes can't pull it); `ImageNotConfigured`
  (no `image=` / `SK8S_IMAGE` / config `image` / `image_prefix` anywhere: raised by
  `run`/`submit`/`map`/`serve` before anything is created; `LocalCluster` and in-pod
  nested submits are exempt); `JobRejected` (admission refused the Job: privileged on
  Autopilot, RBAC 403, invalid spec — carries the server's message + a gated
  hint); `VersionMismatch` (client/worker Python minor); `StoreError`,
  `BlobNotFound`, `BlobTooLarge`.
- Volume family: `VolumeError` (base of `VolumeNotFound`, `VolumeNotPopulated`,
  `VolumeWaitTimeout`) and — subclassing `PodFailed`, *not* `VolumeError` —
  `VolumeAttachBlocked`, `VolumeProvisioningFailed`. Catch `Sk8sError` for all.

## Results and the store

Results are **cloudpickle blobs** in the configured store; futures resolve by a
watch on the Job plus a store read (no polling loop of yours). Result blobs are
deleted on fetch; Jobs expire by TTL (1h default) and workflow cleanup removes
the rest. The client reads the store directly — on `gs://`/`s3://` it needs its
own credentials (ADC / AWS chain) and the relevant extra (`[gcs]` / `[s3]`).

## Images

**Nothing is published** — you build the base yourself from the checkout (one
per Python minor, matching your client):

```bash
docker build -f docker/Dockerfile.worker --build-arg PYTHON_VERSION=3.12 \
  -t YOURREG/sk8s-base:py3.12 . && docker push YOURREG/sk8s-base:py3.12
```

Layer Python deps on it, no Dockerfile needed:

```bash
sk8s image build --tag YOURREG/science --base YOURREG/sk8s-base:py3.12 \
  --pip numpy --pip pysam --push
# conda flavor: --conda ... --channel bioconda builds FROM a conda base instead
# (not valid with --base; note it pip-installs sk8s from PyPI, so pre-release it won't work)
```

From Python: `from sk8s import images` (not reachable as `sk8s.images`) —
`images.generate_dockerfile(pip=[...])` returns the Dockerfile text (a `str`);
`images.build(tag, ...)`, `images.push(tag)`.

**apt/system packages** (bwa, samtools, …) need Dockerfile content on your base — but
NOT a Dockerfile *file*. `images.build` takes the whole thing as a string (fed to docker
on stdin; `path=` is the build context, default `.`), so a script that needs a custom
image stays one self-contained script:

```python
from sk8s import images

TAG = "YOURREG/science:py3.12-v1"
images.build(TAG, push=True, dockerfile="""
FROM YOURREG/sk8s-base:py3.12
RUN apt-get update && apt-get install -y bwa samtools && rm -rf /var/lib/apt/lists/*
RUN pip install numpy
""")
sk8s.submit(fn, image=TAG)   # or SK8S_CONFIG / config `image`
```

Multi-stage Dockerfiles work the same way (one string). `dockerfile=` cannot be combined
with `base`/`pip`/`conda`/`channels` — it replaces generation entirely. Prefer this over
shelling out to `docker build` or keeping a `docker/Dockerfile.*` sidecar next to an
example script.
The worker only unpickles and runs functions, so any image with sk8s installed
at the same Python minor works.

## Scratch space and hardware

**Job-local scratch wants ephemeral storage, not a volume.** If the data is born
and dies inside one job — downloaded inputs, intermediate files, an index pulled
from a bucket — request it as ephemeral storage and write anywhere in the
container filesystem:

```python
sk8s.submit(align, requests={"cpu": "16", "memory": "28Gi",
                             "ephemeral-storage": "40Gi"})
```

No PVC to create, nothing to clean up, no provisioning wait, no deletion
finalizer races. Reach for `create_volume` only when scratch cannot do the job:
the data must outlive the pod, be shared between jobs (reference volumes,
RWX), or be written by one job and read by another.

**GKE Autopilot caveat:** default pods cap ephemeral storage at **10Gi** and
*evict* on overflow. Past that, pin a dedicated machine with local-NVMe-backed
ephemeral storage (multi-GB/s, the cap becomes the machine's SSD capacity,
billed per machine, dies with the pod):

```python
sk8s.submit(align,
    requests={"cpu": "16", "memory": "28Gi", "ephemeral-storage": "200Gi"},
    node_selector={
        "cloud.google.com/compute-class": "Performance",       # dedicated node
        "cloud.google.com/machine-family": "c3d",              # pick the silicon
        "cloud.google.com/gke-ephemeral-storage-local-ssd": "true",  # NVMe scratch
    })
```

The same `node_selector` trio (with or without the local-SSD line) pins CPU
architecture — useful for benchmarks or AVX-512-dependent code. On minikube,
`node_selector` keys must match labels you set yourself.

## Volumes

Only for data that outlives a pod or crosses jobs — for job-local scratch,
see "Scratch space and hardware" above.

```python
vol = sk8s.create_volume("10Gi")                                   # RWO, name auto
ref = sk8s.create_volume("100Gi", name="idx", keep=True,
                         access_modes=["ReadWriteOnce", "ReadOnlyMany"])
```

- `volumes=` on a job: `"idx"` (one, **read-only**, `/mnt/idx`) | `["a", "b"]`
  (many, ro) | `{"idx": {"access_mode": "rw", "mountpoint": "/scratch"}}` (both
  inner keys optional; defaults `ro`, `/mnt/<name>`; unknown keys and relative
  mountpoints are `ValueError`s).
- **Mounts default read-only**; exactly the writers say `"access_mode": "rw"`.
- RWO+ROX volumes are *logical*: readers get a hidden clone — point-in-time as
  of the last **completed** writer (succeeded, or cancelled/vanished; a *failed*
  writer never becomes the generation). Details and patterns:
  references/advanced.md.
- `storage_class` resolves from modes via config (`ReadWriteMany` → the class
  `sk8s config` recorded; typed error with hint if unmapped); explicit always wins.
- `scratch_volume("10Gi")` — context manager, deleted on exit. `keep=True`
  volumes survive workflow cleanup; `delete_volume(name)` removes one (and its
  clones).

## Services

```python
svc = sk8s.serve(run_server, name="scorer", port=8000)   # Deployment + Service; DNS-1035 name
sk8s.get_service("scorer").url                            # from jobs: cluster DNS; reserved wf id "svc"
```

`run_server` must itself serve the port and block forever (it IS the server
process, not a factory).

## Workflows vs plain submissions

A plain `submit`'s future resolves in *your* process — the work is tethered to
it. `submit_workflow(main)` runs `main` as a detached in-cluster driver that owns
everything it submits; your laptop can vanish. `sk8s.attach(workflow_id)`
(any machine with the same config) → `WorkflowHandle` (`.id`, `.status()`,
`.result()`, `.kill()`). **`.result()` deletes the workflow on success by
default** — pass `delete=False` to collect again later. See
references/advanced.md.

## Describing data

Before planning an analysis, ask what the data is (cheap: header sniffing, never a
full read):

```python
d = sk8s.describe("gs://lab/runs/oct/")      # or a local path, an s3:// prefix, or a single file
d["headline"]   # "Found 48 samples (FASTQ, paired-end, 96 files, ~12 GB each)"
```

```
sk8s data describe gs://lab/runs/oct/ [--json] [--samples 5] [--max-objects 10000]
```

- One file -> `{"kind": "file", "format", "confidence", "compression", "facts", "notes"}`.
  Formats: FASTQ(.gz) (read length, phred offset guess, record count only if the file is
  <10MB), BAM (sort order, references, read groups, aligner), CRAM (version), VCF(.gz)
  (samples, contigs, reference), FASTA by content, whatever the name (first 5 contig names,
  contig count, sequence type, line width; total residues only if the file is <10MB, else
  the count is a lower bound and says so), `.fai` (`fasta-index (fai)`: contig names and
  lengths), `.bai` (`bam-index (bai)`, by magic: reference count), CSV/TSV (delimiter,
  header, column types), gzip/unknown.
- Directory or `gs://`/`s3://` prefix -> `{"kind": "cohort", ...}`: counts and bytes by
  format, size stats, `patterns` (FASTQ R1/R2 pairing + samples estimate, BAM index
  pairing), and full facts for the first 5 files (sorted by path).
- `confidence` is `"magic"` (format signature), `"content"` (bytes parse as the format) or
  `"extension"` (name only). Cohort-wide `formats` are by extension (`confidence:
  "extension"`); only the sampled files are sniffed by content (`sample_format_mismatches`
  flags disagreements).
- Remote: only the first 1 MiB of each *sampled* file is range-read. Listings stop at
  10,000 objects: `listing_capped: true` and a note say so. Needs the `sk8s[gcs]` /
  `sk8s[s3]` extra and ambient credentials; failures are typed `Sk8sError`s naming the fix.
- Everything is JSON-serializable. Parquet, pysam-backed extras and lineage are not here yet.

## Previewing and inspecting

**`dry_run=True`** on `submit`, `map`, `starmap`, `submit_map`, `submit_starmap`,
`run` and `submit_workflow` builds everything a submission would send —
validation, mount parsing, image/store/namespace resolution, the pickled payload
(unpicklable args and over-size ConfigMap blobs fail *now*), the full serialized
`V1Job` — and creates **nothing**: no anchor, no payload upload, no API call.
It returns a `sk8s.DryRun`:

```python
dr = sk8s.map(align, samples, dry_run=True, requests={"cpu": "4", "memory": "8Gi"})
dr.n_jobs, dr.total_cpu_request, dr.total_memory_request   # 48, 192.0 cores, bytes
dr.image, dr.store, dr.namespace                           # resolved, as submit would
dr.jobs[0]                                                 # ONE representative Job body (dict)
dr.notes                                                   # batching / clone / placeholder caveats
json.dumps(dr.to_dict())
```

For fan-outs only the first job's body is serialized (`n_jobs` counts batches
when `batch_size` is set). The owner reference / `SK8S_ANCHOR_*` env carry a
placeholder uid (the anchor does not exist yet); read-only mounts show the
volume itself as the claim (a RWO+ROX volume's hidden clone is chosen at real
submit). `LocalCluster` raises `ValueError` (no Job to preview).

A request you did not set counts as 0 in the totals, but sk8s adds no defaults
of its own: the cluster decides (GKE Autopilot gives an unsized pod 0.5 vCPU and
2 GiB; this includes a `submit_workflow` driver). `dr.unset_requests` lists
which of `"cpu"`/`"memory"` are unset and a note says so; pass `requests=` for
an exact preview.

**`sk8s.explain("wf-...")`** (CLI: `sk8s explain wf-...`, `--json` for the dict)
rebuilds the whole picture from cluster + store state, from any process — an
agent with zero context needs nothing else. Read-only. Keys: `workflow` (id,
namespace, detached, age, retention), `driver` (phase, `logs_tail`, `logs_note` when the log is empty), `jobs`
(name, role, phase `pending|running|succeeded|failed`, timings, requests, pods,
and for stuck pods `waiting_code`/`waiting_reason` — image-pull, unschedulable,
volume attach/provision, crash loops — or `failure_reason` for failed jobs: OOM
with the limit, exit codes), `counts`, `volumes` (owned volumes and clones),
`store` (`blob_count`), **`blocking`** (plain-English statements of what is
preventing progress; `[]` when healthy), `warnings` (sections it could not read,
e.g. no events RBAC), and — only when derivable — `estimated_cost_usd` labelled
`"rough"` (cpu requests x runtime x $0.04/vCPU-hour; nothing else). Unknown
workflow: `Sk8sError` / CLI exit 1; an unhealthy workflow is still exit 0 (it is
a report, not a check).

`--json` also exists on `sk8s workflows` (list of `{id, detached, age,
age_seconds, driver_job, driver}`), `sk8s doctor` (list of `{name, ok,
fix_hint}`; exit code unchanged) and `sk8s clean` (`{deleted: [...], kept:
[{workflow, reason}]}`, plus `sandboxes` when any exist), and `sk8s sandbox create|list`. Errors stay on stderr with exit 1.

## Budgets and sandboxes

**Budgets** are client-side guardrails: `budget=Budget(...)` on `submit`, `run`, the `map`
family and `submit_workflow`, or as a default via `sk8s.configure(budget=...)` /
`Config.budget` / config file `[budget]` table / env `SK8S_BUDGET` (JSON).

```python
from sk8s import Budget
b = Budget(max_concurrent_pods=8, max_cpu_hours=200, max_estimated_usd=10)   # all optional
sk8s.map(align, samples, requests={"cpu": "4"}, budget=b)     # raises BudgetExceeded BEFORE any job
dr = sk8s.map(align, samples, requests={"cpu": "4"}, budget=b, dry_run=True)
dr.budget.ok, dr.budget.violations, dr.budget.estimated_usd   # dry_run REPORTS, never raises
```

What is enforced, honestly:

- **Admission, estimate-based.** The submission's totals (the same ones `dry_run` shows:
  `n_jobs x cpu request`) become `cpu-hours = cpu x Budget.assumed_job_hours` (default 1h:
  sk8s cannot know how long a job runs) and `usd = cpu-hours x $0.04` (the
  `sk8s.explain` rate). Over `max_cpu_hours` / `max_estimated_usd` -> `BudgetExceeded`
  before anything is created. A fan-out (`pilot` included) is admitted **whole**, before
  its first job. **A job that requests no cpu estimates to 0 and passes any cost limit**: by
  default the cost limits are blind to it (the verdict carries a note and the first such
  submission per cluster logs a warning). Close the hole by setting `requests=`, or set
  `Budget(assumed_cpu_when_unset=1)` to charge such jobs that many cores each. Memory, GPUs and
  storage are not costed.
- **Memoization.** A single `memo=True` job is admitted *after* the memo lookup, so a hit is
  never refused for what it would have cost; a miss is refused as usual (it leaves at most a
  swept anchor and staged-input record, no payload or Job). A **fan-out is admitted whole before
  any lookup**, so its memo hits are charged as if they ran (the error says so): raise the
  budget or preview with `dry_run=True`.
- **Per-call vs configured.** A per-call `budget=` *replaces* the configured one outright
  (it may loosen it); there is no "tighter wins" merge.
- **Concurrency is a cap, not a failure.** `max_concurrent_pods` tightens `max_concurrent`
  (the min applies) for each `map`/`submit_map`/`starmap` call, so submission blocks
  instead of raising. It is per fan-out call: not across separate calls or a plain
  `submit()` loop.
- **Detached workflows.** The budget rides to the driver pod as `SK8S_BUDGET`, so every
  nested `submit`/`map` inside the workflow is admitted against it, per submission.
- **NOT enforced:** cumulative spend across submissions or workflows, actual pod runtime
  or metered usage, anything for a `LocalCluster` single `submit()`. A budget is
  admission control, not metering; cumulative accounting needs the v3 event log.

**Sandboxes** are the isolation pattern in one command:

```
sk8s sandbox create --name try1 --budget-usd 2 --ttl 6h   # namespace sk8s-sbx-try1 + RBAC
export SK8S_CONFIG=$PWD/.sk8s-sandbox-try1.toml            # printed by create
sk8s doctor; python my_experiment.py                       # confined to the sandbox
sk8s sandbox list [--json]                                 # label sk8s/kind=sandbox
sk8s sandbox delete try1                                   # namespace + store prefix cascade
```

`create` applies the normal profile (local/generic; gke only when the kube context is a GKE
one; `--gsa` for Workload Identity), carves the store (`s3://b/p` -> `s3://b/p/sbx-try1`, `local:` likewise;
`k8s` is already namespace-scoped and kept), and writes an isolated 0600 config (namespace, store, budget, image
settings, `store_env`) next to you; your own config is untouched. Use `--assume-cpu 1` with
`--budget-usd` (it sets `assumed_cpu_when_unset`), otherwise the usd limit cannot see jobs that
request no cpu. `--ttl` (default 24h) is an
annotation: `sk8s clean` / `sk8s.clean()` reaps **expired** sandboxes cluster-wide, under the
workflow rules (300s minimum age; never with active jobs or running services; unprovable ->
kept; `clean --json` lists them under `sandboxes`). Worker start-up sweeps never reap
sandboxes; `sk8s clean` prints `reaped sandbox X (ttl expired)` for each. Whoever runs
`delete`/`clean` needs credentials for the sandbox's base store (bucket access for s3/gs): the
store prefix is removed first and a sandbox whose prefix cannot be removed is kept. The
isolated `.sk8s-sandbox-<name>.toml` is removed by `delete` run from the directory holding it;
a reap (or a delete from elsewhere) leaves that stale file behind. `--ttl` is capped at 3650d.
`delete` is unconditional (live jobs die) but refuses any namespace that is not
labelled as a sandbox, and keeps the namespace if the store prefix cannot be deleted.

## CLI

`sk8s config` (plan/apply + write config; `--dry-run`), `sk8s doctor`,
`sk8s image build|push`, `sk8s workflows` (every anchored workflow, finished
included, with driver phase), `sk8s explain <wf>` (everything about one
workflow, see above), `sk8s logs <wf>` (prints the detached driver's log
once — not a follow/tail), `sk8s kill <wf>`, `sk8s clean [--older-than 7d]`,
`sk8s shell [--image IMG]`, `sk8s data describe <path|uri> [--json]`,
`sk8s sandbox create|list|delete` (see Budgets and sandboxes).

Cleanup rules: retention = older than `retention_days` AND no active jobs, with a
300s minimum age — right after a crash `sk8s clean --older-than 0` may say
"nothing to clean"; use `sk8s kill <id>` for a specific workflow.
