# sk8s v2 Implementation Roadmap

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md`

The v2 rebuild is decomposed into one implementation plan per feature branch,
sized for agent-swarm execution. Each plan produces working, tested,
independently mergeable software.

## Workflow

- Integration branch: **`version-2`**. Nothing merges to `main` until v2 ships.
- One full clone per feature at `~/code/jobs/jobs.<feature>`, branched from
  `version-2` as `feat/<feature>`.
- Feature branches are integrated via GitHub PRs into `version-2`, reviewed
  before merge.
- Plans are written **just-in-time**: a feature's plan is written only when its
  dependencies have merged into `version-2`, so it plans against the real API.

## Plans

| # | Plan | Branch | Depends on | Status |
|---|------|--------|-----------|--------|
| 1 | Foundation: data plane, worker, futures, core API | `feat/core` | — | **Merged** (PR #70) |
| 2 | Workflows: anchors, detached submit/attach, retention GC | `feat/workflows` | 1 | **Merged** (PR #74) |
| 3 | Cloud stores: S3 + GCS backends, MinIO tests | `feat/stores-cloud` | 1 | **Merged** (PR #72) |
| 4 | Volumes: PVCs, anchor ownership, scratch_volume | `feat/volumes` | 1, 2 | **Merged** (PR #75) |
| 5 | Services: serve(), Service handle, get_service | `feat/services` | 1 | **Merged** (PR #73) |
| 6 | Images: published base images, builders, release CI | `feat/images` | 1 | **Merged** (PR #71) |
| 7 | Cluster config: ClusterProfile, `sk8s config`/`doctor`, CLI (local/generic only) | `feat/cluster-config` | 1, 2 | **Merged** (PR #76) |
| 8 | CI & docs: minikube per-PR workflow, stress test, docs site, migration guide (GKE/EKS TABLED) | `feat/ci-docs` | 3, 6, 7 | **In progress** (`...plan-8-ci-docs.md`) |

Execution notes (2026-10-10): Jared waived plan-review gates ("get it all done").
Integration suites share one minikube → integration tasks run serially across
features, each with its own image tag (SK8S_TEST_IMAGE). Plans 2 and 5 both
touch worker.py/__init__.py: second-to-merge rebases.

Parallelizable after Plan 1 merges: 2, 3, 4*, 5, 6, 7 (— *4's anchor-ownership
task waits on 2; its PVC core does not).

## Division of labor per plan

Each plan is executed subagent-driven: implementer agents work tasks in the
feature clone, a reviewer agent checks each task, and the finished branch gets
a whole-branch review before its PR into `version-2`.
