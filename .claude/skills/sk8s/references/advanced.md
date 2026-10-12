# Advanced sk8s workflows

The worked example throughout: a genome-alignment pipeline — a big shared index,
wide fan-out, per-sample follow-up jobs, fire-and-forget execution.

## Detached workflows (close the laptop)

```python
def main() -> dict:                      # runs IN the cluster as the driver
    futs = sk8s.submit_map(align, samples, image=IMG, requests={"cpu": "2"})
    return harvest(futs)                 # see partial-failure pattern below

wf = sk8s.submit_workflow(main, image=IMG)
print(wf.workflow_id)                    # note it down / log it
```

Tomorrow, any machine with the same config (`SK8S_CONFIG` + kubeconfig + store
creds):

```python
h = sk8s.attach("wf-abc123")
result = h.result()                      # blocks if still running; raises the driver's error
```

- The driver is a pod: it needs everything in env/config, and its image needs the
  modules of everything it pickles/unpickles.
- `sk8s workflows` lists live ones; `sk8s logs <id>` tails the driver; `sk8s
  kill <id>` cascades the whole tree.
- Results outlive the driver until workflow deletion; `keep_results=True` (or
  config) keeps blobs past cleanup.

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

Nested submission needs no extra setup on any store — on GKE the pod's Workload
Identity covers the child's blobs. Depth is unlimited; everything hangs off one
anchor.

## Shared reference volumes (ReadOnlyMany)

```python
ref = sk8s.create_volume("100Gi", name="idx", keep=True,
                         access_modes=["ReadWriteOnce", "ReadOnlyMany"])
sk8s.run(build_index, volumes={"idx": {"access_mode": "rw"}}, image=IMG)
vals = sk8s.map(align, samples, volumes="idx", image=IMG)   # N readers, N nodes
```

Semantics to design around:

- Readers see a **point-in-time clone**: the volume as of the last *successful*
  `rw` job. A running writer is invisible; readers launched during a write wait
  inside `submit()` for it (up to `volume_wait_seconds`, default 300 — **raise it
  via `SK8S_VOLUME_WAIT_SECONDS` when writers run long**, e.g. hours-long index
  rebuilds), then get the fresh data. In-flight readers are untouchable by a
  writer — they're on their own disk. So "rebuild weekly, fan out daily" needs no
  coordination code at all; a long run spanning a rebuild can mix index versions,
  so record the version per result if that matters.
- A **failed** writer doesn't become a generation, but bytes it already wrote are
  on the primary and can appear in the next clone. For crash-safe in-place
  rebuilds, write to a scratch dir on the volume and atomically rename/symlink as
  the last step.
- Cost: one extra PD per volume (clone billed at full provisioned size,
  creation ~seconds, readers share it). Clones live in the source's zone.
- Wedge diagnostics are typed and bounded, never silent Pending: a second `rw`
  job queues behind a writer and just runs afterward (~15s hand-over); an
  accidental `rw` fan-out fails with `VolumeAttachBlocked` naming the holder
  after `volume_attach_grace_seconds` (300).
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
failures: dict[int, BaseException] = {}
for i, fut in enumerate(futs):
    try:
        results[i] = fut.result(timeout=remaining(deadline))
    except Exception as e:                        # RemoteTraceback is e.__cause__
        failures[i] = e
```

Run this *in the driver* of a detached workflow and return
`{"results": results, "failures": {i: traceback_text(e) for ...}}` — pickled
exceptions round-trip, but text is safer across client environments
(`ResultDecodeError` applies to returned exception objects too).

## Ops habits that matter at scale

- One namespace per team/project; `SK8S_CONFIG` per agent/CI run; never share a
  config file between concurrent automation.
- Autopilot bills per pod: a 50-pod fan-out of 2-CPU aligners is real money while
  running, ~nothing after. The standing costs are disks and the store, both tiny
  next to Filestore — avoid RWX unless concurrent *writers* are genuinely needed.
- `requests` sizing: Autopilot mutates small requests upward and fills memory to
  match CPU; ask for what the tool needs, don't assert what you got.
- Everything sk8s makes is labeled (`app.kubernetes.io/managed-by=sk8s`,
  `sk8s/workflow-id`, `sk8s/kind`) — `kubectl get all -l sk8s/workflow-id=<id>`
  is the debugging view. `docs/conventions.md` is the label/key reference.
- Retention: `clean()` runs opportunistically and via `sk8s clean`; workflows
  with running jobs and anything younger than 300s are never reaped; `keep=True`
  volumes and `keep_results=True` blobs survive.
