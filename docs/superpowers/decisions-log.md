# sk8s v2 rebuild — decisions log (extracted from SDD ledgers, 2026-10-10)

Plan 1 (foundation): its workspace was already retired; its rulings are recorded in the session's final report.

## docs/superpowers/plans/2026-10-10-sk8s-v2-plan-8-ci-docs.md [jobs.cidocs]
- Task 1: implemented (14e491c..270ec15); review dispatched. INCIDENT: committed conflict markers found in docs/dev-setup.md on version-2 — controller's P7 merge staged a conflicted file unchecked (git add -A without git status). Directed fix in T3. Process note: always `git status` + full-tree marker grep before committing a merge.
- Final fix wave (3b61586..1456d5a): verified directly by controller (doc rows, version-sync test, cli fatal reason; 596 tests + mypy green). Ruling: direct verification substitutes the scoped re-review for this 4-line wave (precedent: P3 docs wave). Merging.

## docs/superpowers/plans/2026-10-10-sk8s-v2-plan-7-cluster-config.md [jobs.config]
- Ruling: read-then-write-on-drift kept (stronger than create-or-replace); ServiceAccount ensure-only (secrets). Directed into T2: normalization pinning tests (shuffled-equal → no write; extra verb/rule → replace) + resource_names in the rule key + shared normalized helper for doctor's subset check.
- Ruling (user, 2026-10-10): serial integration slot DROPPED — the original hazards (shared image tag, one-time RBAC replace) are gone; suites are namespace-coexistent by design and concurrent runs are now expected. Multi-client stress test added to Plan 8 scope.
- Final branch review (fable): With fixes. Important x2: doctor store probe ignores store_endpoint (drift between doctor and runtime — the feature's own promise); T3-directed shell fixes were DROPPED (controller error: ledgered "directed into T4" but never carried into T4's dispatch — same failure class as the ledger-before-dispatch slips). Fix wave dispatched (endpoint injection; sh-based shell hang + startup/cleanup prints; hint/plan wording; unreachable short-circuit).
- Carried to Plan 8: reserved workflow id "doctor"; load_config UTF-8 read; concurrent-config-apply pin in the stress test; GKE/EKS untabling needs worker-side identity checks + endpoint/storage-class persistence.

## docs/superpowers/plans/2026-10-10-sk8s-v2-plan-6-images.md [jobs.images]
- Ruling: conda-generated images install sk8s[s3,gcs]==<version> (workers need store backends); explicit base combined with conda deps raises ValueError instead of silent drop. Folded into T2 as directed fixes. Cost if wrong: slightly fatter conda images; stricter API.
- Ruling: guard keeps full `pip install .` (eager sk8s imports need runtime deps); --no-deps declined with comment. Cost if wrong: slower CI legs.

## docs/superpowers/plans/2026-10-10-sk8s-v2-plan-5-services.md [jobs.services]
- Ruling: get_service must verify the sk8s/kind=service label (delete is destructive) — directed fix in T3. Concurrent-serve payload clobber documented as a limitation (no conditional blob writes available), not fixed. Cost if wrong: rare double-serve race remains.
- Carried to Plans 7/8: exclude svc/ prefix from bucket lifecycle rules; worker logging config; readiness probes; LocalCluster typed error for services.

## docs/superpowers/plans/2026-10-10-sk8s-v2-plan-3-stores-cloud.md [jobs.stores]
- Carried forward: (a) nested/in-pod clusters lose store endpoint+creds (load_config doesn't read SK8S_S3_ENDPOINT; store_env not re-exported) — target: P2T4/Plan 8 after P3 merges; (b) SK8S_*-prefix shadow guard for store_env — deliberate policy decision later.
- Final fix wave (2d204c8): dev-extra + docs. Ruling: scoped re-review satisfied by controller's direct verification (fresh venv collects 213 tests) — config/docs-only wave, no code. Merging.

## docs/superpowers/plans/2026-10-10-sk8s-v2-plan-4-volumes.md [jobs.volumes]

## docs/superpowers/plans/2026-10-10-sk8s-v2-plan-2-workflows.md [jobs.workflows]
- Ruling: default close() must not cascade-delete kept results — _keep_any disables auto-delete; kept workflows age out. Cost if wrong: kept workflows linger until retention.
- Ruling: result ConfigMaps get anchor owner refs via SK8S_ANCHOR_NAME/UID env → worker-side owner_ref; scope expansion into spec.py/worker.py authorized. Cost if wrong: broader cross-branch conflict surface with Plan 5's worker.py changes (rebase on merge).
- Ruling: expiry = older-than-retention AND no active jobs (fail-safe skip on list errors; guard applies to older_than_days; workflow= stays unconditional force). Cost if wrong: one job-list per expired candidate.
- Ruling: clean() resolves each anchor's store from its sk8s/store annotation (cached); unresolvable → skip+log. Cost if wrong: slightly slower sweeps.
- Ruling (re-review upheld): force-delete refusing unresolvable stores is correct — "unconditional" means age/liveness, not proceeding blind to where blobs live; store= override is the escape hatch.
- CORRECTION: the final branch review was NOT actually dispatched when ledgered earlier (dispatch lost to an interleaved notification; user caught it). Actually dispatched now (fable, c732671..0a00ff6). Process note: ledger "dispatched" lines must be written AFTER the Agent call returns, not before.
- Scope change (user, 2026-10-10): GKE/EKS tabled — Plan 8 drops the cloud CI matrix; Plan 7 ships local/generic adapters only.
- Ruling: retention stays age-from-creation with liveness guard, conditional on C1 landing (hang becomes typed error); completed-at stamping filed as a real follow-up for Plan 8's list.


## Post-CI follow-ups

- First CI run exposed that a buildable-but-credential-less store lets delete_anchor remove the anchor while the best-effort delete_prefix fails silently, orphaning blobs; this is reviewed-and-accepted best-effort behavior, but it is a candidate for delete-prefix-before-anchor ordering in a future pass.
