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

Job options (kwargs on any of the above): `image`, `requests`, `limits`
(e.g. `requests={"cpu": "500m", "memory": "1Gi"}`), `volumes`, `timeout`,
`backoff_limit`, `service_account`, `namespace`, `name_prefix`, `privileged`,
`pull_policy`, `keep_results`. `map`/`submit_map` add `batch_size` (N items per
job) and `max_concurrent`. `timeout` on `map` is one overall deadline, not
per-item.

`sk8s.configure(namespace=..., image=..., store=...)` overrides config for the
process; `sk8s.configure()` resets.

## Errors (all exported from `sk8s`)

- Your function raised → the **same exception type** re-raises locally, with
  `__cause__` = `RemoteTraceback` (full remote trace). Unreconstructible types →
  `RemoteException` (`.type_name`, `.message`, `.tb_text`).
- `MapFailed` — `.failures` is `[(index, exception), ...]`, `.total` the item
  count. **Successful values are not on it**; see references/advanced.md for
  harvesting.
- `ResultDecodeError` — job *succeeded* but the client can't unpickle the result
  (missing module locally). `JobTimeout` (also a `TimeoutError`); `PodFailed`;
  `ImagePullError`; `JobRejected` (admission refused the Job: privileged on
  Autopilot, RBAC 403, invalid spec — carries the server's message + a gated
  hint); `VersionMismatch` (client/worker Python minor); `StoreError`,
  `BlobNotFound`, `BlobTooLarge`.
- Volume family: `VolumeError` (base: `VolumeNotFound`, `VolumeNotPopulated`,
  `VolumeWaitTimeout`) and — subclassing `PodFailed`, *not* `VolumeError` —
  `VolumeAttachBlocked`, `VolumeProvisioningFailed`. Catch `Sk8sError` for all.

## Results and the store

Results are **cloudpickle blobs** in the configured store; futures resolve by a
watch on the Job plus a store read (no polling loop of yours). Pods are deleted
and blobs reaped when a workflow completes (`keep_results=True` to retain).
The client reads the store directly — on `gs://`/`s3://` it needs its own
credentials (ADC / AWS chain) and the relevant extra (`sk8s[gcs]` / `sk8s[s3]`).

## Images

Python deps, no Dockerfile needed:

```bash
sk8s image build --tag localhost:5000/science --pip numpy --pip pysam --push
# conda flavor: --conda ... --channel bioconda (builds FROM a conda base, not valid with --base)
```

From Python: `sk8s.images.generate_dockerfile(pip=[...])`, `build(tag, ...)`,
`push(tag)`.

**apt/system packages** (bwa, samtools, …) need a Dockerfile layered on the sk8s
base:

```dockerfile
FROM ghcr.io/jared-maguire/sk8s:VERSION-pyMINOR   # match your client's Python minor
RUN apt-get update && apt-get install -y bwa samtools && rm -rf /var/lib/apt/lists/*
RUN pip install numpy
```

`docker build` + `docker push` to your registry/AR, then `image=` /
`SK8S_IMAGE` / config `image`. The worker only unpickles and runs functions, so
any image with sk8s installed at the same Python minor works.

## Volumes

```python
vol = sk8s.create_volume("10Gi")                                   # RWO, name auto
ref = sk8s.create_volume("100Gi", name="idx", keep=True,
                         access_modes=["ReadWriteOnce", "ReadOnlyMany"])
```

- `volumes=` on a job: `"idx"` (one, **read-only**, `/mnt/idx`) | `["a", "b"]`
  (many, ro) | `{"idx": {"access_mode": "rw", "mountpoint": "/scratch"}}` (both
  inner keys optional; defaults `ro`, `/mnt/<name>`).
- **Mounts default read-only**; exactly the writers say `"access_mode": "rw"`.
- RWO+ROX volumes are *logical*: readers get a hidden clone, point-in-time as of
  the last **successful** writer (details + patterns: references/advanced.md).
- `storage_class` resolves from modes via config (`ReadWriteMany` → the class
  `sk8s config` recorded; typed error with hint if unmapped); explicit always wins.
- `scratch_volume("10Gi")` — context manager, deleted on exit. `keep=True`
  volumes survive workflow cleanup; `delete_volume(name)` removes one (and its
  clones).

## Services

```python
svc = sk8s.serve(make_app, name="scorer", port=8000)   # Deployment + Service, DNS-1035 name
sk8s.get_service("scorer").url                          # from jobs: cluster DNS; reserved wf id "svc"
```

## Workflows vs plain submissions

A plain `submit`'s future resolves in *your* process — the work is tethered to
it. `submit_workflow(main)` runs `main` as a detached in-cluster driver that owns
everything it submits; your laptop can vanish. `sk8s.attach(workflow_id)`
(any machine with the same config) → `WorkflowHandle`: `.result()`, `.kill()`.
See references/advanced.md.

## CLI

`sk8s config` (plan/apply + write config; `--dry-run`), `sk8s doctor`,
`sk8s image build|push`, `sk8s workflows`, `sk8s logs <wf>`, `sk8s kill <wf>`,
`sk8s clean [--older-than 7d]`, `sk8s shell [--image IMG]`.

Cleanup rules: retention = older than `retention_days` AND no active jobs, with a
300s minimum age — right after a crash `sk8s clean --older-than 0` may say
"nothing to clean"; use `sk8s kill <id>` for a specific workflow.
