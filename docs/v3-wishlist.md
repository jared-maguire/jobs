# sk8s v3 wishlist

Brainstormed 2026-10-11, right after v2 completed (plans 1–11, PRs #70–#87).
Ideas, not commitments; no designs or plans exist yet. The theme: **v2 made jobs
reliable; v3 makes workflows cheap and durable.**

## 1. Memoization / content-addressed caching

Hash (function code + args + image digest) → check the store before submitting;
a re-run of a 50-sample pipeline after 3 failures costs 3 samples, not 50.
v1 had `state.memoize`; v2 cut it; it returns as a headline feature. Hard design
question: cache-invalidation ergonomics (when a rerun must NOT hit cache).
Secretly one feature with (2): replay needs idempotency, idempotency is the
cache.

## 2. Durable, resumable workflows

Today a detached driver is one pod babysitting futures; driver dies ⇒ workflow
dies. Event-source workflow state into the store so a restarted driver replays
to where it was. Unlocks: kill-then-resume, Kubernetes-restartable drivers,
crash-safe long pipelines.

## 3. Scheduled and event-driven workflows

`sk8s.schedule(rebuild, cron="@weekly")` → a k8s CronJob launching a workflow
driver (the weekly-index-rebuild case needs external cron today). Ambitious
version: bucket-event triggers ("new sample in gs://inbox/ ⇒ run pipeline").

## 4. Spot/preemptible pods + straggler handling

`spot=True` with automatic rescheduling on preemption; speculative re-execution
of map stragglers. Autopilot spot is ~60–90% off and batch genomics is the ideal
workload — the single biggest cost lever available, and it's mostly scheduling
policy, not new infrastructure.

## 5. GPUs as a first-class request

`requests={"gpu": 1}` mapped to Autopilot accelerator classes; GPU-flavored
bases in the image tooling; doctor checks for GPU availability/quota.

## 6. Observability: `sk8s top` + a lightweight dashboard

v1's dashboard was cut; v2's labels/anchors make a read-only one nearly free.
Per-workflow live view (pods, progress, cost-so-far from Autopilot per-pod
pricing), `fut.logs()` on running jobs, post-run cost accounting per workflow.
sk8s knows enough to answer "what did this run cost?" with real numbers.

## 7. The fast dev loop

The slowest cycle today: module-by-reference code ⇒ image rebuild ⇒ push ⇒ run.
A dev mode that ships the working package alongside the payload (the wire
already carries the function; the module tree is the same mechanism) makes
edit-run seconds instead of minutes, with real images reserved for production.

## Honorable mentions (less conviction)

- EKS adapter (committed in spirit, parked; more chore than feature).
- Multi-cluster submission ("run this where there's quota/cheapest").
- Collective ops beyond `map`: reduce / tree-aggregation.
- Per-user identity propagation / quotas for real multi-tenancy.

## Candidate v2.x fixes (small, could land before any v3)

- `MapFailed` should carry the successes (`e.results`), not just failures.
- Fail fast at submit when no image is configured (build-your-own doctrine means
  the default name can never pull): "no image configured — build one: …" instead
  of an `ImagePullError` timeout.
