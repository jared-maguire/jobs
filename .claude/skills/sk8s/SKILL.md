---
name: sk8s
description: Use when running Python functions on a Kubernetes cluster with sk8s — submitting jobs, maps, or detached workflows; setting up a cluster or namespace for sk8s (minikube or GKE, as user or admin); building worker images; using volumes (scratch, shared-read reference data); or debugging sk8s errors like ImagePullError, VolumeWaitTimeout, MapFailed, ResultDecodeError, or pods stuck Pending.
---

# sk8s: Python functions as Kubernetes jobs

## Overview

sk8s runs ordinary Python functions as Kubernetes Jobs and returns real
`concurrent.futures.Future`s. Functions and results travel by cloudpickle; results
land in a pluggable blob store (in-cluster ConfigMaps by default, `gs://`/`s3://`
for real clusters). One `sk8s config` sets a namespace up; `sk8s doctor` proves it
works — including from *inside* a pod, which your laptop's credentials cannot prove.

```python
import sk8s
print(sk8s.run(lambda: 40 + 2))                    # one job, blocks for the result
results = sk8s.map(f, inputs)                       # fan out; sk8s.submit_map for futures
wf = sk8s.submit_workflow(main)                     # detached: survives laptop close
```

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
2. **The worker's Python minor version must match the client's**, and the stock
   worker image contains **no scientific stack** — a function using numpy needs a
   custom image (`sk8s image build --pip numpy …`). Failure: `VersionMismatch` or
   a remote `ModuleNotFoundError`.
3. **Volume mounts default to read-only.** Any job that writes passes
   `volumes={"name": {"access_mode": "rw"}}`. Failure: `OSError: Read-only file
   system` raised *by your function*.
4. **A plain `submit`/`map` is tethered to your process** (the client resolves
   futures). Close the laptop ⇒ orphaned work. `submit_workflow` detaches; attach
   from anywhere later.
5. **`sk8s.map` raises `MapFailed` carrying only the failures** — completed
   values are not on the exception. When partial results matter, keep the futures
   (`submit_map`) and harvest per-future (pattern in references/advanced.md).

## Quick reference

| Task | Command / call |
|---|---|
| Set up a namespace (any cluster) | `sk8s config --namespace NS --store STORE` (`--dry-run` first) |
| GKE, with cloud IAM | `sk8s config --provider gke --project P --region R --gsa sk8s-worker --store gs://B/sk8s/ --apply-cloud` |
| Verify, incl. from inside a pod | `sk8s doctor` (red first run on fresh IAM is normal; retry ~60s) |
| Point sk8s at a non-default config file | `SK8S_CONFIG=/path/to.toml` (agents/CI: always set this) |
| Any config field via env | `SK8S_<FIELD>`, e.g. `SK8S_NAMESPACE`, `SK8S_IMAGE`, `SK8S_IMAGE_PREFIX` |
| Build + push a worker image | `sk8s image build --pip numpy --pip pysam -t TAG --push` |
| apt/system deps in an image | custom Dockerfile `FROM` the sk8s base (references/api.md) |
| Watch / kill / clean | `sk8s workflows`, `sk8s kill <id>`, `sk8s clean --older-than 7d` |

## Common mistakes

| Symptom | Cause → fix |
|---|---|
| `ResultDecodeError` on a *successful* job | client lacks the result type's module → install it client-side, or return builtins |
| Remote `ModuleNotFoundError` | stock image has no deps → build an image, set `image=`/`SK8S_IMAGE` |
| `OSError: Read-only file system` in your function | mounts default ro → `{"access_mode": "rw"}` for the writer |
| Pod Pending forever on minikube | image not in the cluster (`ImagePullError`/`ErrImageNeverPull`) → registry workflow in references/setup.md |
| Everything gone after closing the laptop | tethered `submit` → `submit_workflow` |
| 47 good results lost because 3 failed | `MapFailed` carries failures only → per-future harvest pattern |
| `sk8s doctor` red right after `--apply-cloud` on GKE | IAM propagation (~60s) → retry before debugging |
| Writes to the config file clobbered another session | global default path → set `SK8S_CONFIG` per agent/CI run |
