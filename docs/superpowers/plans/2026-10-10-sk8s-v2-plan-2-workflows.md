# sk8s v2 Plan 2: Workflows — Anchors, Detached Submit/Attach, Retention

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every workflow's resources hang off one anchor object (cascade delete = whole-workflow GC); workflows can be submitted detached, reattached from any machine, and expire by retention age.

**Architecture:** A per-workflow "anchor" ConfigMap owns (via ownerReferences) every Job and blob ConfigMap the workflow creates; object-store prefixes are recorded on the anchor and deleted with it. `submit_workflow` runs the driver function as an in-cluster Job (the driver IS the per-workflow controller); `attach(id)` reconstructs a handle from the anchor's annotations alone. Retention sweeping is `sk8s.clean()` plus a best-effort worker-side sweep.

**Tech Stack:** Existing sk8s v2 foundation (kubernetes client, cloudpickle); no new deps.

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md` §5 (binding).

**Branch/workflow:** `feat/workflows` from `version-2`, clone `~/code/jobs/jobs.workflows`. PR into `version-2`.

## Global Constraints

(Plan 1's constraints carry over: no `shell=True`, no kubectl in the package, kubernetes client models only, `mypy --strict`, labels `app.kubernetes.io/managed-by: sk8s` + `sk8s/workflow-id` on every object, TDD per task, full suite + mypy green before each commit.)

- Anchor ConfigMap name is deterministic: `sk8s-anchor-<workflow_id>`.
- Anchor labels: managed-by, workflow-id, plus `sk8s/kind: anchor`. Annotations: `sk8s/driver-job` (empty until a driver exists), `sk8s/detached` ("true"/"false"), `sk8s/created-at` (ISO-8601 UTC), `sk8s/retention-days`, `sk8s/store` (the store string), `sk8s/store-prefix` (the workflow's blob key prefix).
- Deleting an anchor must also `delete_prefix` the workflow's store prefix (owner references only cover ConfigMaps; object stores need the explicit delete).
- A Cluster **inside a pod** (env `SK8S_WORKFLOW_ID` set) must ADOPT the existing anchor, never create a second one.
- `sk8s.clean()` must never delete resources labeled `sk8s/kind: service`, and never delete anchors younger than their retention.
- The `atexit` cleanup must never delete an anchor while any tracked future is unresolved, and never for a detached workflow.

## Review Focus

1. **Adopt-don't-recreate:** a nested Cluster in a worker pod reuses the parent's anchor; a second anchor would orphan half the workflow's resources — pinned in Task 1.
2. **Cascade covers the object store:** deleting an anchor with an s3-style store string must call `delete_prefix` even though no ownerReference exists — pinned in Task 1 (unit) and noted for Plan 3's integration.
3. **attach() from a cold process:** a fresh interpreter with nothing but the workflow id must reach status and result — pinned in Task 2 (unit) and Task 4 (integration).
4. **atexit safety:** interpreter shutdown with pending futures must leave everything intact (ages out instead) — pinned in Task 1.
5. **clean() scoping:** expired anchors die, fresh anchors and service-kind objects survive — pinned in Task 3.

---

### Task 1: Anchors and ownership wiring

**Files:**
- Create: `sk8s/anchors.py`, `tests/test_anchors.py`
- Modify: `sk8s/cluster.py` (anchor creation/adoption + owner refs + close semantics), `sk8s/spec.py` (`build_job` gains `owner: V1OwnerReference | None = None`), `tests/fakes.py` (FakeCoreV1 assigns `uid` on ConfigMap create; records owner_references)

**Interfaces:**
- Produces:
  ```python
  # sk8s/anchors.py
  ANCHOR_KIND_LABEL = "sk8s/kind"
  def anchor_name(workflow_id: str) -> str                      # "sk8s-anchor-<id>"
  def create_anchor(core_api, *, namespace: str, workflow_id: str, store: str,
                    store_prefix: str, retention_days: int, detached: bool) -> Any   # V1ConfigMap (409 → read existing)
  def get_anchor(core_api, *, namespace: str, workflow_id: str) -> Any | None
  def owner_ref(anchor: Any) -> Any                             # V1OwnerReference(kind=ConfigMap, uid=anchor uid, controller=True)
  def set_annotation(core_api, *, namespace: str, workflow_id: str, key: str, value: str) -> None
  def delete_anchor(core_api, store, *, namespace: str, workflow_id: str) -> None
      # deletes the ConfigMap (propagation Background) AND store.delete_prefix(prefix from annotations); both best-effort/logged
  ```
- `Cluster` behavior: first `submit()` ensures the anchor exists (create, or adopt when `SK8S_WORKFLOW_ID` env was inherited — adoption = `get_anchor`, and if somehow absent, create with `detached="false"`); every `build_job` call passes `owner=owner_ref(anchor)`; every `store.put` for payloads/results passes the same owner (ConfigMapStore already accepts it; LocalStore ignores it).
- `Cluster.close(delete_workflow: bool | None = None)`: default None = delete the anchor iff (not detached) and every tracked future is done; True forces, False skips. An `atexit` hook calls `close()` once (idempotent; guard double-close).

- [ ] **Step 1:** Write failing tests in `tests/test_anchors.py` covering: anchor created lazily on first submit with exact name/labels/annotations; second submit does not re-create (fake raises 409 → adopted); jobs and payload/result ConfigMaps carry an ownerReference with the anchor's uid; in-pod adoption (monkeypatch env `SK8S_WORKFLOW_ID` + pre-created anchor in the fake → no create call); `delete_anchor` deletes the CM and calls `store.delete_prefix` with the recorded prefix; `close()` with a pending (unresolved) future does NOT delete; `close(delete_workflow=True)` does; detached anchors never auto-delete on close.
- [ ] **Step 2:** Run, confirm failures.
- [ ] **Step 3:** Implement. FakeCoreV1: `create_namespaced_config_map` assigns `metadata.uid = uuid4().hex` when unset; keep 409-on-duplicate behavior. `spec.build_job(owner=...)` sets `metadata.owner_references=[owner]`.
- [ ] **Step 4:** Full suite + mypy green.
- [ ] **Step 5:** Commit `feat: workflow anchors with cascade ownership`.

### Task 2: submit_workflow / attach / WorkflowHandle

**Files:**
- Create: `sk8s/workflows.py`, `tests/test_workflows.py`
- Modify: `sk8s/api.py` + `sk8s/__init__.py` (export `submit_workflow`, `attach`, `WorkflowHandle`)

**Interfaces:**
- Produces:
  ```python
  def submit_workflow(func, /, *args, cluster: Any | None = None, **job_opts) -> WorkflowHandle
      # fresh workflow id; anchor created with detached="true"; driver Job submitted with
      # name_prefix "wfdriver", keep_results=True, env SK8S_WORKFLOW_ID (already set by spec);
      # anchor annotation sk8s/driver-job set to the driver job name; returns immediately.
  def attach(workflow_id: str, cluster: Any | None = None) -> WorkflowHandle
      # reads the anchor; raises Sk8sError("unknown workflow ...") if absent.
  class WorkflowHandle:
      id: str
      def status(self) -> dict          # {"driver": "active|succeeded|failed|unknown", "jobs": {"active": n, "succeeded": n, "failed": n}}
      def result(self, timeout: float | None = None, *, delete: bool = True) -> Any
          # registers the driver job with the cluster's watcher, waits, fetches the driver's
          # result blob (key <wf>/<driver-job>/result), re-raising remote exceptions as usual;
          # on success with delete=True, delete_anchor (cascade). Driver result must persist
          # until fetched (keep_results=True at submit covers this).
      def kill(self) -> None            # delete_anchor — cascade removes every job/blob
  ```
- `status()` counts jobs via `batch.list_namespaced_job(label_selector="sk8s/workflow-id=<id>")` using `job_phase` from cluster.py; driver phase from the annotated driver-job name.
- `attach` constructs its own `Cluster` bound to the anchor's workflow id when none is passed (the Cluster must NOT create a new anchor — it adopts; reuse Task 1's adoption path by setting the cluster's workflow_id before first use).

- [ ] **Step 1:** Failing tests (Harness-style fakes from `tests/test_cluster.py`): submit_workflow creates anchor (detached=true) + driver job with keep_results; annotation records driver name; attach on a cold Cluster (fresh fakes sharing state) finds it; `result()` returns the driver's value after the harness finishes the driver job, and the anchor is deleted after fetch; `result(delete=False)` keeps it; `kill()` deletes anchor; attach of unknown id raises; status() counts phases.
- [ ] **Step 2:** Confirm failures. **Step 3:** Implement. **Step 4:** Suite + mypy. **Step 5:** Commit `feat: detached workflows — submit_workflow/attach/WorkflowHandle`.

### Task 3: Retention — sk8s.clean() and the worker sweep

**Files:**
- Create: `sk8s/cleaning.py`, `tests/test_cleaning.py`
- Modify: `sk8s/worker.py` (pre-run best-effort sweep), `sk8s/__init__.py` (export `clean`)

**Interfaces:**
- Produces:
  ```python
  def clean(namespace: str | None = None, *, older_than_days: float | None = None,
            workflow: str | None = None, core_api=None, store=None) -> list[str]
      # returns deleted workflow ids. workflow=<id>: delete that one unconditionally.
      # otherwise: delete anchors whose age exceeds older_than_days if given, else their own
      # sk8s/retention-days annotation. NEVER touches sk8s/kind=service objects. Each delete
      # is delete_anchor (cascade + store prefix). Malformed annotations: skip + log.
  ```
- Worker sweep: in `worker.main`, before executing the function, call `clean()` for the pod's namespace wrapped in try/except-log, disabled when env `SK8S_SWEEP == "0"`. It must never fail the job and never add RBAC requirements beyond list/delete on configmaps+jobs (already granted).

- [ ] **Step 1:** Failing tests: expired anchor (created-at backdated beyond retention) deleted with its store prefix; fresh anchor survives; `older_than_days` override respected; `workflow=` targets exactly one; service-kind ConfigMap in the namespace untouched; worker sweep runs on main() and a sweep failure (core_api raising) doesn't affect the job result; SK8S_SWEEP=0 disables.
- [ ] **Step 2-5:** fail → implement → suite+mypy → commit `feat: retention clean and worker-side sweep`.

### Task 4: Integration on minikube

**Files:**
- Create: `tests/integration/test_workflows.py`
- Modify: `docs/dev-setup.md` (note: workflows need the sk8s ServiceAccount RBAC — already in the role)

**Requirements** (marked `integration`, `requires_cluster`, timeouts like the existing suite; reuse its `cluster` fixture conventions):
1. **Jobs-submitting-jobs** (closes the foundation review's named gap): `cluster.run` a function that itself calls `sk8s.map(lambda x: x*x, range(3))` and returns the sum → 5. Requires the worker image to have been built from this branch (rebuild `sk8s-dev:latest` via `minikube image build` first — document in the test module docstring).
2. **Detached round-trip:** `submit_workflow(pipeline)` where pipeline maps 3 squares and sums; then from a NEW `Cluster` (fresh instance), `attach(id).result(timeout=240)` == 5; afterwards poll ≤10s that the anchor and all workflow-labeled ConfigMaps/Jobs are gone.
3. **Kill cascade:** submit_workflow of a long sleeper; `kill()`; poll ≤20s that jobs and ConfigMaps with the workflow label are gone.
4. **Anchor ownership:** submit a plain job, delete the anchor directly via the API, poll that the job's ConfigMaps disappear (cascade proof).

- [ ] Steps: write tests → rebuild dev image in minikube → run `SK8S_INTEGRATION=1` until green → full unit suite + mypy → commit `feat: workflow integration suite (nested jobs, detach/attach, cascade)`.

## Done criteria
Unit suite + mypy green; integration green on minikube including the nested-jobs test; PR into `version-2`.
