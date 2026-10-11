# sk8s v2 Plan 4: Volumes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** PVC lifecycle helpers (`create_volume`/`delete_volume`/`scratch_volume`) with workflow-anchor ownership so volumes die with their workflow unless kept, mounted at `/mnt/<name>` via the existing job-spec support.

**Architecture:** Thin client-model builders over `V1PersistentVolumeClaim`; ownership reuses `anchors.owner_ref`. The mount path already exists (`spec.build_job(volumes={pvc: path})` and `Cluster._normalize_volumes` accept names/lists/dicts).

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md` §7.

**Branch/workflow:** `feat/volumes` from `version-2` (post-wave-1), clone `~/code/jobs/jobs.volumes`. PR into `version-2`.

## Global Constraints

(Plan 1's carry over: client models only, no kubectl/shell in package, mypy --strict, TDD, suite green per commit.)

- `create_volume(size, *, name=None, access_modes=("ReadWriteOnce",), storage_class=None, keep=False, cluster=None) -> str` returns the PVC name (generated `vol-<rand6>` when none given; DNS-1123 validated).
- PVCs carry the standard labels (managed-by + workflow-id) and, when `keep=False` and the cluster has an anchor, an ownerReference to the anchor. `keep=True` → no owner ref and NO workflow-id label (survives cascade AND retention sweeps — mirror the services exemption pattern with `sk8s/kind: volume-keep` or similar; document the choice).
- `delete_volume(name, cluster=None)` idempotent. `scratch_volume(size, cluster=None)` context manager: create → yield name → delete in finally.
- Exports from `sk8s/__init__`.

## Review Focus

1. A kept volume must survive both anchor cascade and `clean()` retention — pinned by unit tests against the fakes and by the integration cascade test.
2. A non-kept volume created inside a workflow dies with the anchor — integration-pinned.
3. `scratch_volume` deletes on exception paths — unit-pinned.
4. Access-mode and size strings pass through to the PVC spec verbatim — unit-pinned.
5. Deleting a PVC still mounted by a running pod blocks on the PVC protection finalizer — `delete_volume` must not hang: use a non-blocking delete (default propagation) and document that the PVC vanishes when pods release it.

---

### Task 1: volumes module (unit)

**Files:** Create `sk8s/volumes.py`, `tests/test_volumes.py`; modify `sk8s/__init__.py`, `tests/fakes.py` (PVC methods on FakeCoreV1: create/read/delete_namespaced_persistent_volume_claim with 409/404 semantics).

- [ ] TDD per the constraint list above: builder fields (size→resources.requests.storage, access modes, storage_class only when given, labels/owner rules for keep both ways), name generation/validation, idempotent delete, scratch_volume success + exception paths, anchor-less cluster (no anchor yet → create triggers `_ensure_anchor` like submit does — reuse the cluster's anchor path so the volume is owned from birth).
- [ ] Full suite + mypy. Commit `feat: volume lifecycle helpers`.

### Task 2: integration (minikube)

**Files:** Create `tests/integration/test_volumes.py`; touch docs/dev-setup.md only if needed (PVC RBAC verbs already granted).

- [ ] Tests (markers/timeouts per existing suite; feature image tag `sk8s-dev-volumes:latest`):
  1. Round-trip: `create_volume("100Mi")`; writer job writes a file under `/mnt/<vol>`; reader job returns its content; delete_volume; poll gone. (Single-node minikube: RWO re-use across sequential jobs is fine.)
  2. Cascade: volume created on a cluster with an anchor, `close(delete_workflow=True)` → poll PVC gone.
  3. Kept volume: `keep=True`, cascade the anchor, PVC survives; explicit delete_volume afterwards.
  4. scratch_volume context: inside the block a job writes/reads; after the block poll PVC gone.
- [ ] Existing integration suite green under `SK8S_TEST_IMAGE=sk8s-dev-volumes:latest`; unit + mypy green. Commit `feat: volumes integration suite`.

## Done criteria
Unit + mypy green; volumes integration green on minikube; PR into `version-2`.
