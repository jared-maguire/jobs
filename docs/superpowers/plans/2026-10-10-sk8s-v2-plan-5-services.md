# sk8s v2 Plan 5: Services — serve(), Service Handle, get_service

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `sk8s.serve(func, name=..., port=...)` deploys a function as a long-running Deployment+Service; a handle offers `.url/.logs()/.scale()/.delete()`; `get_service(name)` reattaches from anywhere.

**Architecture:** Same payload-via-store mechanism as jobs (no code in manifests); a service-mode flag in the worker entrypoint runs the function forever instead of storing a result. Services are exempt from retention (label `sk8s/kind: service`) and are cleaned only by explicit `.delete()`.

**Tech Stack:** Existing foundation; no new deps.

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md` §6.

**Branch/workflow:** `feat/services` from `version-2`, clone `~/code/jobs/jobs.services`. PR into `version-2`.

## Global Constraints

(Plan 1's constraints carry over: no shell/kubectl in package, client models only, mypy --strict, TDD, suite green per commit.)

- Labels on Deployment, pod template, Service, and the payload ConfigMap: managed-by, `sk8s/kind: service`, `sk8s/service: <name>`. NO `sk8s/workflow-id` label and NO anchor owner references — services outlive workflows by design, and retention sweeps must skip `sk8s/kind: service` (a parallel plan builds the sweep with that exemption; your only obligation is the label).
- Service payload blob key: `svc/<name>/payload`; it must survive pod restarts (never deleted by any job-side cleanup; deleted only in `.delete()`).
- Worker service mode via env `SK8S_SERVICE=1`: decode payload, call `func(*args, **kwargs)`; if it RETURNS, exit 1 with a logged error (service functions must block); if it raises, log the traceback to stderr and exit 1 (the Deployment restarts it). No result blob is ever written in service mode.
- Service names must be valid DNS-1123 labels; invalid names raise ValueError before any API call. Duplicate `serve` on an existing name raises `Sk8sError` naming the conflict (map the 409) — no silent replace.

## Review Focus

1. **`.delete()` removes all three artifacts** (Deployment, Service, payload ConfigMap) and is idempotent — pinned in Task 2.
2. **Payload survives restarts:** nothing in the job-side blob lifecycle can delete `svc/*` keys (distinct key prefix + no owner ref) — pinned in Task 1.
3. **Duplicate name → clear error**, not a half-created pair — pinned in Task 2 (create Service first or roll back on second-step 409; either way no orphan remains).
4. **`get_service` on a missing name** raises a typed error, not an SDK exception — Task 2.
5. **A service function that returns immediately** must surface as CrashLoopBackOff-style restarts, not a phantom "healthy" service — worker exit-1 path pinned in Task 1; integration observes restarts in Task 3.

---

### Task 1: Service specs + worker service mode

**Files:**
- Create: `sk8s/spec_service.py`, `tests/test_spec_service.py`
- Modify: `sk8s/worker.py` (service mode), `tests/test_worker.py` (service-mode tests)

**Interfaces:**
```python
# sk8s/spec_service.py
def build_deployment(name: str, *, image: str, namespace: str, payload_uri: str, store: str,
                     port: int, replicas: int, service_account: str, pull_policy: str,
                     requests: dict[str, str], limits: dict[str, str]) -> client.V1Deployment
def build_service(name: str, *, namespace: str, port: int) -> client.V1Service
```
Deployment pod template: container `worker`, command `["python", "-m", "sk8s.worker"]`, env `SK8S_STORE`, `SK8S_NAMESPACE`, `SK8S_PAYLOAD_URI`, `SK8S_SERVICE=1`, `containerPort=port`; selector/labels per Global Constraints. Service: ClusterIP, port→port, selector `sk8s/service: <name>`.

Worker: in `main`, when `environ.get("SK8S_SERVICE") == "1"`, decode payload, run the function; returning normally → log error, return 1; raising → `log.exception` + return 1; never touch `SK8S_RESULT_KEY` (which is absent in service mode — don't read it unconditionally).

- [ ] **Step 1:** Failing tests: deployment/service model fields (labels incl. kind=service and NO workflow-id, env set, no SK8S_RESULT_KEY in env, selector match, replicas, resources None when empty); worker service mode: blocking func simulated by one that raises `KeyboardInterrupt`? No — use a func that returns immediately → main returns 1 and writes no blob; a raising func → 1, no blob; normal job mode untouched (existing tests stay green).
- [ ] **Steps 2–5:** fail → implement → suite+mypy → commit `feat: service specs and worker service mode`.

### Task 2: services.py — serve / Service / get_service

**Files:**
- Create: `sk8s/services.py`, `tests/test_services.py`
- Modify: `sk8s/__init__.py` (export `serve`, `get_service`, `Service`), `tests/fakes.py` (add FakeAppsV1 with create/read/patch/delete_namespaced_deployment honoring 409/404, and Service methods on FakeCoreV1)

**Interfaces:**
```python
def serve(func, /, *args, name: str, port: int, replicas: int = 1,
          image: str | None = None, cluster: Any | None = None, **kwargs) -> Service
def get_service(name: str, cluster: Any | None = None) -> Service   # 404 → Sk8sError("no service named ...")
class Service:
    name: str; namespace: str; port: int
    @property def url(self) -> str                      # http://<name>.<ns>.svc.cluster.local:<port>
    def logs(self) -> str                                # concat pod logs by selector
    def scale(self, replicas: int) -> None               # patch spec.replicas
    def delete(self) -> None                             # deployment + service + payload blob; idempotent
```
`serve`: validate name; payload = encode_payload(func, args, kwargs) → store.put(`svc/<name>/payload`) (no owner); create Deployment then Service; on a 409 from either, delete anything this call created, then raise the conflict error. `get_service` reads the Service object to recover port/namespace.

- [ ] **Step 1:** Failing tests: serve creates all three with right wiring; url string exact; duplicate serve raises and leaves no NEW orphans (pre-existing untouched); scale patches; delete removes all three and twice is a no-op; get_service roundtrip and missing-name error; function kwargs reach the payload (decode it in the test and call).
- [ ] **Steps 2–5:** fail → implement → suite+mypy → commit `feat: serve(), Service handle, get_service`.

### Task 3: Integration on minikube

**Files:** Create `tests/integration/test_services.py`; modify `docs/dev-setup.md` (RBAC: role needs deployments+services verbs — update the `kubectl create role` line to include `--resource=deployments.apps,services` and note existing clusters must re-apply).

**Requirements** (marked `integration`): rebuild `sk8s-dev:latest` from this branch first; apply the updated RBAC.
1. Serve a real HTTP function: `def srv(): import http.server, socketserver; socketserver.TCPServer(("", 8080), http.server.SimpleHTTPRequestHandler).serve_forever()` with `name="sk8s-it-http", port=8080`.
2. Probe it from inside the cluster via a job: `cluster.run(lambda u=svc.url: __import__("urllib.request", fromlist=["urlopen"]).urlopen(u, timeout=10).status)` == 200, with retries while the pod comes up (poll ≤120s).
3. `get_service("sk8s-it-http")` from a fresh Cluster reaches the same URL.
4. `scale(2)` → poll until 2 ready replicas.
5. `delete()` → poll ≤30s that Deployment, Service, and the payload ConfigMap are gone.
Teardown must delete the service even on failure (fixture finalizer).

- [ ] Steps: write → iterate to green (`SK8S_INTEGRATION=1`) → unit suite + mypy → commit `feat: services integration suite`.

## Done criteria
Unit + mypy green; services integration green on minikube; PR into `version-2`.
