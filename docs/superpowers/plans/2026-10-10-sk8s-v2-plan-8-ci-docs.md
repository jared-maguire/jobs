# sk8s v2 Plan 8: CI, Stress, Docs (GKE/EKS tabled)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close out v2: carried code fixes from earlier branch reviews, per-PR CI (unit + minikube integration — the cloud matrix is TABLED per user decision), a multi-client stress test proving the isolation invariants under concurrent load, and the docs (README rewrite, v1→v2 migration guide, label/conventions reference).

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md` §10–12 (narrowed: no GKE/EKS).

**Branch/workflow:** `feat/ci-docs` from `version-2`, clone `~/code/jobs/jobs.cidocs`. PR into `version-2`.

## Global Constraints
(Plan 1's carry over: mypy --strict, TDD for code changes, suite green per commit; actions pinned by major; no secrets beyond GITHUB_TOKEN.)

## Review Focus
1. The stress test must be able to FAIL: every assertion scoped so a cross-client deletion or an unresolved future is detected, not averaged away.
2. CI must run the real integration suite on minikube in the workflow, not merely unit tests labeled "integration".
3. The migration guide's v1→v2 mapping must match the SHIPPED API (verify each line against the code, not the spec).
4. Carried fixes must not regress the suites that pinned the original behavior.
5. README examples must be runnable verbatim against the merged tree.

---

### Task 1: Carried code fixes + CI workflows

**Files:** Modify `sk8s/spec.py`, `sk8s/config.py`, `sk8s/cluster.py`, `.github/workflows/release.yml`; create `.github/workflows/ci.yml`; tests.

Carried fixes (each with a pinning test):
- Nested endpoint propagation (from the stores branch review): `spec.build_job` exports `SK8S_STORE_ENDPOINT=config.store_endpoint` when set (in-pod `load_config` already reads it via the generic env layer) — nested clusters on MinIO/custom endpoints then work.
- Reserve workflow id `"doctor"` alongside `"svc"` in `Cluster.__init__` (doctor probe keys live under `doctor/`).
- `load_config` reads the config file as UTF-8 explicitly.
- release.yml: add a `concurrency` group; set `platforms: linux/amd64` and `provenance: false` explicitly with a one-line comment each (conscious defaults; revisit when clouds untable).

CI workflow (`ci.yml`): on pull_request + push to version-2: job `unit` (matrix 3.10 + 3.12: `pip install -e ".[dev,s3,gcs]"`, `pytest -m "not integration"`, `mypy`); job `integration` (ubuntu, medyagh/setup-minikube@latest pinned by major, build `docker/Dockerfile.dev` into minikube, apply RBAC via `sk8s config --namespace default` (CLI dogfood) or the dev-setup commands, run `SK8S_INTEGRATION=1 pytest tests/integration -m "integration and not stress"` with SK8S_TEST_IMAGE set). Validate YAML by parse; CI can't run here — correctness by review.

- [ ] TDD the code fixes; author workflows; full suite + mypy. Commit `feat: carried fixes and CI workflows`.

### Task 2: Multi-client stress test (minikube)

**Files:** Create `tests/integration/test_stress.py` (marker `stress` in addition to `integration`; registered in pyproject).

Shape: 6 client PROCESSES (multiprocessing, spawn) sharing one cluster/namespace for ~3 minutes: two batch-y clients (maps with batch_size, exact result sums), two workflow clients (detached submit → attach → result == expected; one also creates a kept volume and asserts it survives everything), one churn client (submit sleepers, cancel half, kill one of its own detached workflows mid-run), one chaos-lite client (runs `clean()` repeatedly and lists/asserts it never deletes anything younger than retention). Parent asserts: every client exits 0 with exact expected values; afterwards, per-client label-scoped sweeps confirm no cross-workflow deletions occurred and nothing is left behind except kept objects + MinIO + the probe-free namespace baseline. All waits bounded; one shared deadline; diagnostics on failure (dump of surviving labeled objects).

- [ ] Build `sk8s-dev-cidocs:latest`; run the stress test to green at least twice; run the full integration suite once (not stress) to confirm no regressions; unit + mypy. Commit `test: multi-client stress suite`.

### Task 3: Docs

**Files:** Rewrite `README.md`; create `docs/migration-v1-to-v2.md`, `docs/conventions.md`; update `docs/dev-setup.md` cross-links.

- README: pitch; 5-minute quickstart (minikube → `pip install -e .` → `sk8s config` → `sk8s.run` → `sk8s.map` → detached workflow → service → volume — every snippet runnable verbatim); store backends section (k8s default, s3/gs extras, MinIO pointer); CLI table; link to migration + conventions + dev-setup. No fabricated features; no GKE/EKS promises (one line: cloud guides forthcoming).
- migration-v1-to-v2.md: the mapping table (verify each row against shipped code), semantic changes called out loudly: results are pickled not JSON; remote exceptions re-raise typed; namespace defaults to `sk8s` not `default`/context; `asynchro`/`wait` gone (futures); chunked_* → batch_size; config file moved/format; `python -m sk8s` → `sk8s`.
- conventions.md: label taxonomy (`sk8s/kind`: anchor/service/volume/volume-keep; managed-by; workflow-id), reserved workflow ids (`svc`, `doctor`), blob key layout, retention/ownership rules, the `svc/` bucket-lifecycle exclusion note for object stores.

- [ ] Verify every README/migration snippet by running it (minikube available); suite + mypy untouched-green. Commit `docs: README, migration guide, conventions`.

## Done criteria
Suite + mypy green; stress green twice; docs verified; PR into `version-2`.
