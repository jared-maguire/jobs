# sk8s v2 Plan 7: Cluster Config and CLI (local/generic only — GKE/EKS tabled)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** One declarative `ClusterProfile` that `sk8s config` plans/applies idempotently and `sk8s doctor` verifies check-by-check, plus the full `sk8s` CLI (config, doctor, image, workflows, logs, kill, clean, shell).

**Scope note (user decision 2026-10-10):** GKE/EKS adapters and all cloud-side IAM work are TABLED. Providers this plan ships: `local` (minikube/docker-desktop) and `generic`. The adapter interface must make adding cloud providers later additive.

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md` §9–10 (narrowed as above).

**Branch/workflow:** `feat/cluster-config` from `version-2`, clone `~/code/jobs/jobs.config`. PR into `version-2`.

## Global Constraints

(Plan 1's carry over: client models only, mypy --strict, TDD, suite green per commit. No kubectl/shell in the LIBRARY; see the one ruled CLI exception below.)

- `ClusterProfile` (frozen dataclass): namespace, service_account ("sk8s"), role rules (exact: verbs get/list/watch/create/delete/patch on jobs, pods, pods/log, configmaps, persistentvolumeclaims, deployments [apps], services), rwx_storage_class (None for local), image_prefix, store.
- Provider adapters supply PARAMETERS only (a `profile_for(provider, namespace, store) -> ClusterProfile` function per provider); apply logic is shared. `detect_provider()` from the kube context name: "minikube"/"docker-desktop" → local, else generic.
- `plan(profile)` returns a human-readable list of actions; `apply(profile)` is idempotent (create-or-replace per object: Namespace [ensure], ServiceAccount, Role, RoleBinding); re-running converges.
- `doctor(profile)` returns `list[Check]` (name, ok, fix_hint): cluster reachable; namespace exists; SA exists; role verbs present (read the Role and diff rules); store writable (put/get/delete round-trip of a tiny probe key, prefix `doctor/`); storage class exists when profile names one. Each check independent — one failure never hides the rest.
- `sk8s config` writes the resolved client config to `~/.config/sk8s/config.toml` (namespace, store, image_prefix) after a successful apply; `--dry-run` prints the plan only.
- **Ruled CLI exception:** `sk8s shell` may `os.execvp("kubectl", ...)` for the interactive exec ONLY (TTY streaming via the ws client is out of scope); it must check kubectl exists and print a clear error otherwise. This is the sole kubectl use and lives in cli.py, never the library.
- CLI exit codes: 0 success, 1 failure, doctor exits 1 if any check fails.

## Review Focus

1. `apply` twice converges byte-identically (idempotence) — unit-pinned with fakes recording actions; second apply performs no creates.
2. `doctor`'s store probe must clean up its probe key even on failure, and must not be fooled by a read-only store (write THEN read THEN delete, assert each).
3. The Role diff in doctor must catch a MISSING verb, not just a missing Role — pinned with a role lacking `patch`.
4. CLI argument errors print usage and exit 2 (argparse default), never tracebacks — pinned via subprocess tests on `sk8s` entrypoint.
5. `sk8s clean --older-than` parses durations ("36h", "7d", bare days int) and passes older_than_days correctly — unit-pinned including rejection of garbage.

---

### Task 1: ClusterProfile, adapters, plan/apply (unit)

**Files:** Create `sk8s/profile.py`, `tests/test_profile.py`; modify `tests/fakes.py` (RbacAuthorizationV1 fakes: ServiceAccount/Role/RoleBinding/Namespace create/read/replace with 409/404).

- [ ] TDD: profile exactness (the role-rule table verbatim); detect_provider from context names; plan() action list; apply() creates all four kinds, 409 → replace (converges), namespace ensure; second apply → zero creates (fake records).
- [ ] Full suite + mypy. Commit `feat: ClusterProfile with plan/apply and provider adapters`.

### Task 2: doctor (unit)

**Files:** Create `sk8s/doctor.py`, `tests/test_doctor.py`.

- [ ] TDD: each check pass+fail against fakes (unreachable cluster → first check fails, others still run where meaningful; missing verb caught; store probe write/read/delete with cleanup-on-failure; storage-class check skipped when profile has none). `Check` carries a fix_hint string per failure naming the exact remedy (`sk8s config` or a specific grant).
- [ ] Full suite + mypy. Commit `feat: doctor checks`.

### Task 3: CLI (unit + light subprocess tests)

**Files:** Rewrite `sk8s/cli.py`; create `tests/test_cli.py`.

Subcommands: `config` (--namespace, --store, --provider, --dry-run → plan/apply + write config file), `doctor`, `image build|push` (thin wrappers over sk8s.images with --pip/--conda/--channel/--tag/--push/--minikube), `workflows` (list anchors: id, detached, age, driver phase), `logs <wf>` (driver job logs via the client), `kill <wf>`, `clean [--older-than D]`, `shell [--image]` (hang job + execvp kubectl exec; ruled exception).

- [ ] TDD: parser wiring per subcommand (monkeypatched library calls record args); duration parsing; exit codes; `--dry-run` never applies; subprocess smoke of `sk8s --version`, `sk8s doctor` against a monkeypatched module, and an argparse-error case (exit 2, no traceback).
- [ ] Full suite + mypy. Commit `feat: sk8s CLI`.

### Task 4: integration (minikube)

**Files:** Create `tests/integration/test_config_cli.py`; update docs/dev-setup.md ("or just run: sk8s config" replacing the manual kubectl RBAC block, keeping the manual block as an appendix).

- [ ] Tests (feature image tag `sk8s-dev-config:latest`):
  1. Fresh-namespace lifecycle: create namespace `sk8s-it-cfg` via the client; `apply(profile_for("local", namespace="sk8s-it-cfg", store="k8s"))`; doctor all-green; run one job in that namespace end-to-end; second apply converges; delete the namespace (cascades everything) in a finalizer.
  2. Doctor red path: against a namespace WITHOUT the profile applied, doctor reports the specific failures (SA missing etc.) with fix hints, exit path exercised via the CLI entrypoint.
  3. CLI smoke on the real cluster: `sk8s workflows` lists a live detached workflow; `sk8s kill` removes it (poll cascade).
- [ ] Existing integration suite green under `SK8S_TEST_IMAGE=sk8s-dev-config:latest`; unit + mypy green. Commit `feat: cluster-config integration suite`.

## Done criteria
Unit + mypy green; config/CLI integration green on minikube; PR into `version-2`.
