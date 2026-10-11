# sk8s v2 Plan 3: Cloud Stores — S3 + GCS Backends

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `store = "s3://bucket/prefix/"` and `"gs://bucket/prefix/"` work end-to-end, removing the ~1MB ConfigMap ceiling; verified against in-cluster MinIO.

**Architecture:** Two thin `BlobStore` implementations. boto3 / google-cloud-storage are quarantined inside their modules (~60 lines each) behind the 5-method interface; nothing else imports them. Credentials come from each SDK's standard chain (IRSA / Workload Identity / env) — never from sk8s config. An optional endpoint override (for MinIO and tests) threads from config into worker pods via one env var.

**Tech Stack:** boto3 (`sk8s[s3]` extra), google-cloud-storage (`sk8s[gcs]` extra), MinIO for integration.

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md` §4.

**Branch/workflow:** `feat/stores-cloud` from `version-2`, clone `~/code/jobs/jobs.stores`. PR into `version-2`.

## Global Constraints

(Plan 1's constraints carry over: no `shell=True`/kubectl in the package, mypy --strict, TDD, suite green per commit.)

- `delete_prefix` uses the SAME whole-segment semantics as Plan 1's stores (`wf-a` must not match `wf-ab`); empty prefix rejected with ValueError.
- `get` on a missing key raises `BlobNotFound`. `delete` is idempotent.
- URIs: `s3://bucket/key`, `gs://bucket/key` (key includes the configured prefix). `uri_for` is deterministic, no I/O.
- Importing `sk8s` must NOT import boto3/google-cloud-storage; a missing extra raises `Sk8sError` with the exact message `install with: pip install "sk8s[s3]"` (resp. `[gcs]`) only when the store is actually used.
- Endpoint resolution for S3 (MinIO support): env `SK8S_S3_ENDPOINT` > `Config.store_endpoint` > SDK default. New frozen `Config` field `store_endpoint: str | None = None`; `spec.build_job` exports it to workers as env `SK8S_S3_ENDPOINT` when set (worker-side `get_store` passes it through).

## Review Focus

1. **Pagination:** `delete_prefix` over >1000 keys must delete everything (S3 list is paginated) — pinned in Task 1 with a fake returning paginated results.
2. **Prefix boundary:** `wf-a` vs `wf-ab` across both backends — pinned in Tasks 1–2.
3. **Missing-key mapping:** SDK-specific not-found errors (ClientError 404/NoSuchKey; gcs NotFound) → `BlobNotFound`, everything else propagates as `StoreError` — pinned in Tasks 1–2.
4. **No creds in code or config:** only the SDK default chain; reviewer greps for access-key fields — Task 1–2.
5. **Oversized results actually work:** a >1MB result round-trips through MinIO from a real worker pod — pinned in Task 3.

---

### Task 1: S3Store

**Files:**
- Create: `sk8s/store/s3.py`, `tests/test_store_s3.py`, `tests/fakes_s3.py`
- Modify: `sk8s/store/__init__.py` (factory: `s3://` → S3Store, importing lazily inside the branch), `sk8s/config.py` (`store_endpoint` field), `sk8s/spec.py` (export `SK8S_S3_ENDPOINT` env when configured), `sk8s/cluster.py` or wherever the store factory is fed config (pass endpoint through)

**Interfaces:**
```python
class S3Store(BlobStore):
    def __init__(self, bucket: str, prefix: str = "", *, endpoint: str | None = None, client: Any | None = None) -> None
    # parse helper: from_url("s3://bucket[/prefix/]", endpoint=None) -> S3Store
```
`put` → put_object; `get` → get_object (404/NoSuchKey → BlobNotFound); `delete` → delete_object (idempotent by S3 semantics); `delete_prefix` → paginated list_objects_v2 + delete_objects in batches of ≤1000, filtering keys to whole-segment matches of `<prefix>/<arg>` (same rule as Plan 1). `owner` param accepted and ignored. Lazy boto3 import inside `__init__` (unless `client` injected) with the actionable error message.

- [ ] **Step 1:** Failing tests against `tests/fakes_s3.py` (a dict-backed fake with boto3-shaped responses: `get_object` raising `ClientError` with `Error.Code == "NoSuchKey"`, `list_objects_v2` honoring `ContinuationToken` to force pagination, `delete_objects` batch). Cover: roundtrip, uri_for determinism, missing-key BlobNotFound, delete idempotent, delete_prefix with 2500 fake keys plus the `wf-a`/`wf-ab` boundary pair, empty prefix ValueError, missing-boto3 message (monkeypatch import), endpoint resolution order (env > config > None), factory wiring `get_store("s3://b/p/")`.
- [ ] **Step 2:** fail. **Step 3:** implement (+Config field, spec env export, worker passthrough). **Step 4:** suite+mypy. **Step 5:** commit `feat: S3 blob store`.

### Task 2: GCSStore

**Files:** Create `sk8s/store/gcs.py`, `tests/test_store_gcs.py`, `tests/fakes_gcs.py`; modify `sk8s/store/__init__.py`.

Same shape as Task 1 with google-cloud-storage idioms (`bucket.blob(key)`, NotFound → BlobNotFound, `list_blobs(prefix=...)` with the same segment filter; no endpoint machinery — GCS tests run against fakes only this plan). Same test matrix minus endpoint cases. Commit `feat: GCS blob store`.

### Task 3: MinIO integration

**Files:**
- Create: `tests/integration/minio.py` (deploy/teardown helpers using the kubernetes client: Deployment `minio` image `minio/minio:latest` args `server /data`, Service port 9000, default minioadmin creds via env), `tests/integration/test_store_s3_minio.py`
- Modify: `docker/Dockerfile.dev` (install `.[s3]` so workers have boto3), `docs/dev-setup.md` (MinIO section)

**Requirements** (marked `integration`): fixture deploys MinIO (or reuses a running one), port-forwards 9000 to localhost via a `kubectl port-forward` subprocess (dev tooling in tests is sanctioned; terminate it in teardown), creates bucket `sk8s-test` via boto3, sets client-side env `SK8S_S3_ENDPOINT=http://127.0.0.1:<port>` and builds a Cluster with `store="s3://sk8s-test/it/"`, `store_endpoint="http://minio.default.svc.cluster.local:9000"` (workers resolve in-cluster). Worker pods need AWS creds env: pass `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (minioadmin) into the job env — add a generic `Config.store_env: dict[str,str]` export if no mechanism exists, or set them via the Deployment image defaults; choose the smallest mechanism and disclose it.
Tests: (1) run roundtrip via real job with the s3 store; (2) a ~2MB result (bytes) round-trips — impossible on the ConfigMap store; (3) remote exception path through s3; (4) cleanup: after fetch, the `it/<wf>` prefix is empty (poll ≤10s). Rebuild `sk8s-dev:latest` with the s3 extra first.

- [ ] Steps: write → deploy → iterate to green (`SK8S_INTEGRATION=1`) → unit suite + mypy → commit `feat: MinIO-backed S3 integration tests`.

## Done criteria
Unit + mypy green; MinIO integration green on minikube; PR into `version-2`.
