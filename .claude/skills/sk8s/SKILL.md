---
name: sk8s
description: Use when running Python functions on a Kubernetes cluster with sk8s — submitting jobs, maps, or detached workflows; setting up a cluster or namespace for sk8s (minikube or GKE, as user or admin); building worker images; using volumes (scratch, shared-read reference data); or debugging sk8s errors like ImageNotConfigured, ImagePullError, VolumeWaitTimeout, MapFailed, ResultDecodeError, or pods stuck Pending.
---

# sk8s: Python functions as Kubernetes jobs

## Overview

sk8s runs ordinary Python functions as Kubernetes Jobs and returns real
`concurrent.futures.Future`s. Functions and results travel by cloudpickle; results
land in a pluggable blob store (in-cluster ConfigMaps by default, `gs://`/`s3://`
for real clusters). One `sk8s config` sets a namespace up; `sk8s doctor` verifies
it check by check — and with a `gs://` store, proves the setup from *inside a pod*
(the in-cluster probe), which your laptop's credentials cannot prove.

```python
import sk8s
print(sk8s.run(lambda: 40 + 2))                    # one job, blocks for the result
results = sk8s.map(f, inputs)                       # fan out; sk8s.submit_map for futures
wf = sk8s.submit_workflow(main)                     # detached: survives laptop close
```

No cluster handy? `sk8s.configure(cluster=sk8s.LocalCluster())` runs everything
in-process for API experiments.

## Reading order

| You need to… | Read |
|---|---|
| Install, set up minikube or GKE (user **or** cluster admin), config/env | references/setup.md |
| Submit jobs/maps, futures, errors, images, services, volumes, CLI | references/api.md |
| Detached workflows, nested jobs, shared reference volumes, partial-failure handling, ops | references/advanced.md |

The repo's `README.md` and `docs/` (esp. `docs/gke.md`, `docs/conventions.md`) are
the narrative versions; the references above are the distilled operational truth
and are self-sufficient for agents.

## The five facts that prevent most mistakes

1. **Results are pickles, not JSON.** The *client* needs the modules of every
   returned type (`numpy` result ⇒ `pip install numpy` locally, or return
   builtins). Failure mode: `ResultDecodeError` *after* the job succeeded.
2. **You build the worker image yourself** — no image is published anywhere, by
   design. Build the base from the checkout (`docker build -f
   docker/Dockerfile.worker --build-arg PYTHON_VERSION=3.X .`), push it to *your*
   registry, and layer deps on it. The worker's Python minor **must match the
   client's**, and the base has **no scientific stack**. Failures:
   `ImageNotConfigured` (no image set anywhere; raised at submit, before anything
   is created), `VersionMismatch`, remote `ModuleNotFoundError`.
3. **Volume mounts default to read-only.** Any job that writes passes
   `volumes={"name": {"access_mode": "rw"}}`. Failure: `OSError: Read-only file
   system` raised *by your function*.
4. **A plain `submit`/`map` is tethered to your process** (the client resolves
   futures). Close the laptop ⇒ orphaned work. `submit_workflow` detaches; attach
   from anywhere later — and collect with `result(delete=False)` if you'll look
   again.
5. **`sk8s.map` raises `MapFailed` on any failed item, but the successes ride along**
   — `e.results` is `{index: value}`, `e.failures` is `[(index, exc), ...]`. For
   streaming results as they land, or per-item timeout control, keep the futures
   (`submit_map`) and harvest per-future (pattern in references/advanced.md).

## Quick reference

| Task | Command / call |
|---|---|
| Set up a namespace (any cluster) | `sk8s config --namespace NS --store STORE` (`--dry-run` first) |
| GKE, with cloud IAM | `sk8s config --provider gke --project P --region R --gsa sk8s-worker --store gs://B/sk8s/ --apply-cloud` |
| Verify (in-pod probe runs for `gs://` stores) | `sk8s doctor` (red first run on fresh IAM is normal; retry ~60s) |
| Point sk8s at a non-default config file | `SK8S_CONFIG=/path/to.toml` (agents/CI: always set this) |
| Any config field via env | `SK8S_<FIELD>`, e.g. `SK8S_NAMESPACE`, `SK8S_IMAGE`, `SK8S_IMAGE_PREFIX` |
| Layer Python deps on your base image | `sk8s image build --tag TAG --base YOURBASE --pip numpy --push` |
| apt/system deps in an image | custom Dockerfile `FROM` your base (references/api.md) |
| Try the API with no cluster | `sk8s.configure(cluster=sk8s.LocalCluster())` |
| What is this data? (format, size, cohort) | `sk8s data describe gs://B/prefix/ --json` / `sk8s.describe(uri)` (references/api.md) |
| Watch / kill / clean | `sk8s workflows`, `sk8s kill <id>`, `sk8s clean --older-than 7d` |
| Understand a workflow in one call (jobs, stuck-pod reasons, what blocks it) | `sk8s explain <wf>` / `sk8s.explain("wf-...")` (add `--json` for a dict) |
| Preview before touching the cluster | `sk8s.submit(f, dry_run=True)` (also `map`, `starmap`, `submit_map`, `submit_workflow`) → `sk8s.DryRun` |
| Machine-readable CLI | `--json` on `sk8s explain`, `workflows`, `doctor`, `clean` |

## Common mistakes

| Symptom | Cause → fix |
|---|---|
| `ResultDecodeError` on a *successful* job | client lacks the result type's module → install it client-side, or return builtins |
| `ImageNotConfigured` at submit | nothing is published — build your own base, set `image=`/`SK8S_IMAGE`/config `image` (or `image_prefix`) |
| Remote `ModuleNotFoundError` | base image has no deps → `sk8s image build` or a Dockerfile layer |
| `OSError: Read-only file system` in your function | mounts default ro → `{"access_mode": "rw"}` for the writer |
| Everything gone after closing the laptop | tethered `submit` → `submit_workflow` |
| Workflow gone when you attach the next day | `result()` deletes on success by default → `result(delete=False)` |
| Job "succeeded" with a wrong answer; fan-out of 48 should have been piloted | `expect=` (QC gate) + `pilot=3` on map → `ExpectationFailed` / `PilotFailed` (references/advanced.md) |
| `map` raised and you want the 47 good results | they are on the exception: `e.results[i]`; use `submit_map` + per-future harvest for streaming/timeout control |
| `sk8s doctor` red right after `--apply-cloud` on GKE | IAM propagation (~60s) → retry before debugging |
| Writes to the config file clobbered another session | global default path → set `SK8S_CONFIG` per agent/CI run |
