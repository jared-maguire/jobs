# Advanced sk8s workflows

The worked example throughout: a genome-alignment pipeline — a big shared index,
wide fan-out, per-sample follow-up jobs, fire-and-forget execution.

## Detached workflows (close the laptop)

```python
def main() -> dict:                      # runs IN the cluster as the driver
    sk8s.configure(volume_wait_seconds=4 * 3600)   # knobs must be set IN the driver (see below)
    futs = sk8s.submit_map(align, samples, image=IMG, requests={"cpu": "2"})
    return harvest(futs)                 # see partial-failure pattern below

wf = sk8s.submit_workflow(main, image=IMG)
print(wf.id)                             # note it down / log it
```

Tomorrow, any machine with the same config (`SK8S_CONFIG` + kubeconfig + store
creds):

```python
h = sk8s.attach("wf-abc123")
result = h.result(delete=False)          # blocks if still running; raises the driver's error
```

- **`result()` deletes the workflow on success by default.** Use
  `delete=False` whenever anyone might attach again; delete explicitly when done.
- **Tuning knobs do not follow you into the cluster**: the driver pod gets the
  core `SK8S_*` env (namespace, store, image, …) but not fields like
  `volume_wait_seconds` — a laptop-side `SK8S_VOLUME_WAIT_SECONDS` does nothing
  for the driver or for nested submitters. Call `sk8s.configure(...)` inside the
  driver function.
- The driver is a pod: its image needs the modules of everything it pickles and
  unpickles (your pipeline package, if functions are imported rather than defined
  in `__main__`).
- `sk8s workflows` lists every anchored workflow (finished included) with the
  driver's phase; `sk8s logs <id>` prints the driver log once; `sk8s kill <id>`
  cascades the whole tree.
- `keep_results=True` stops the client-side close from deleting the workflow —
  it then **ages out via retention** (`retention_days`, default 7). It is "collect
  later this week", not an archive; copy anything permanent out of the store.

## Nested jobs (jobs submitting jobs)

Just call `sk8s.submit(...)` inside a worker function — the pod inherits
namespace, store, image, service account, and workflow identity from env, so
children join the *same* workflow tree (owned, killed, cleaned together):

```python
def align(sample: str) -> dict:
    bam = run_bwa(sample)                          # uses the mounted index
    qc = sk8s.submit(run_qc, bam)                  # launched from inside the cluster
    return {"bam": bam, "qc": qc.result()}
```

Nested submission is proven on the `k8s` (ConfigMap) store and on `gs://` with
Workload Identity (the pod's identity covers the child's blobs). **It does NOT
work on stores needing `store_env` credentials (e.g. MinIO `AWS_*`)** —
`store_env` is deliberately never read from pod env, so children lose the creds.
Depth is otherwise unlimited; everything hangs off one anchor.

## Shared reference volumes (ReadOnlyMany)

```python
ref = sk8s.create_volume("100Gi", name="idx", keep=True,
                         access_modes=["ReadWriteOnce", "ReadOnlyMany"])
sk8s.run(build_index, volumes={"idx": {"access_mode": "rw"}}, image=IMG)
vals = sk8s.map(align, samples, volumes="idx", image=IMG)   # N readers, N nodes
```

Semantics to design around:

- Readers see a **point-in-time clone**: the volume as of the last *completed*
  `rw` job (succeeded or cancelled; a *failed* writer never becomes the
  generation, though bytes it wrote first can appear in the next clone). A
  running writer is invisible; readers launched during a write wait inside
  `submit()` for it (up to `volume_wait_seconds`, default 300 — **raise it where
  the submit happens** — in the driver via `sk8s.configure`, since the env var
  doesn't propagate into pods — when writers run long), then get the fresh data.
  In-flight readers are untouchable by a writer. So "rebuild weekly, fan out
  daily" needs no coordination code; a run spanning a rebuild can mix index
  versions, so record the version per result if that matters.
- For crash-safe in-place rebuilds, write to a scratch dir on the volume and
  atomically rename/symlink as the last step (covers the failed-writer
  partial-bytes case above).
- Cost: one extra PD per volume (clone billed at full provisioned size; creation
  ~5s at 10Gi, larger sizes take longer, readers share one clone). Clones live in
  the source's zone.
- Wedge diagnostics are typed and bounded, never silent Pending: a second `rw`
  job queues behind a writer and runs after a ~15–30s hand-over; an accidental
  `rw` fan-out fails with `VolumeAttachBlocked` naming the holder after
  `volume_attach_grace_seconds` (300).
- **Non-cloning provisioners serve EMPTY clones silently** (minikube's default
  `standard` class does). ROX patterns are GKE/real-CSI territory; on minikube,
  test with RWO single-node semantics.

## Partial-failure harvesting (the MapFailed trap)

`sk8s.map` is all-or-nothing in its return: on any failure it raises `MapFailed`
with only `(index, exception)` pairs. When N-3 good results must survive 3
failures, hold the futures:

```python
futs = sk8s.submit_map(align, samples, image=IMG, max_concurrent=50)
results: dict[int, dict] = {}
failures: dict[int, str] = {}
for i, fut in enumerate(futs):
    try:
        results[i] = fut.result(timeout=remaining(deadline))
    except Exception as e:                        # RemoteTraceback is e.__cause__
        failures[i] = "".join(traceback.format_exception(e))
```

Run this *in the driver* of a detached workflow and return
`{"results": results, "failures": failures}` — traceback *text*, not exception
objects, so the collecting client never hits `ResultDecodeError` on an exotic
error type. (Caveat: if a `submit` itself raises mid-fan-out, `submit_map`
re-raises and you hold no futures — wrap the fan-out when even that must be
survivable.)

## Ops habits that matter at scale

- One namespace per team/project; `SK8S_CONFIG` per agent/CI run; never share a
  config file between concurrent automation.
- Autopilot bills per pod (plus a ~$0.10/h cluster management fee, free-tier
  covered for one cluster): a 50-pod fan-out of 2-CPU aligners is real money
  while running, ~nothing after. Standing costs are disks and the store, both
  tiny next to Filestore — avoid RWX unless concurrent *writers* are needed.
- `requests` sizing: Autopilot mutates small requests upward and fills memory to
  match CPU; ask for what the tool needs, don't assert what you got.
- Everything sk8s makes carries `app.kubernetes.io/managed-by=sk8s`; jobs and
  pods also carry `sk8s/workflow-id` — `kubectl get jobs,pods -l
  sk8s/workflow-id=<id>` is the debugging view (`sk8s/kind` marks anchors,
  services, volumes). `docs/conventions.md` is the label/key reference.
- Retention: `clean()` runs opportunistically and via `sk8s clean`; workflows
  with running jobs and anything younger than 300s are never reaped; `keep=True`
  volumes survive (kept *workflows* age out — see above).
