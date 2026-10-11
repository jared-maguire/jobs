# Conventions: labels, ids, keys and lifecycle

Reference for the names sk8s puts on Kubernetes objects and in blob stores, and the rules
for who owns and deletes what. Everything here is what the code does today; nothing is a
promise beyond that. See also the [README](../README.md), the
[migration guide](migration-v1-to-v2.md) and [dev-setup.md](dev-setup.md).

- [Vocabulary](#vocabulary)
- [Labels](#labels)
- [Annotations](#annotations)
- [Object names](#object-names)
- [Reserved workflow ids](#reserved-workflow-ids)
- [Blob key layout](#blob-key-layout)
- [Ownership and retention](#ownership-and-retention)
- [Object-store lifecycle rules and `svc/`](#object-store-lifecycle-rules-and-svc)

## Vocabulary

- **Workflow**: everything one client session (or one detached driver) submits. Each has an
  id like `wf-k3x9ab`.
- **Anchor**: one ConfigMap per workflow, created on its first submission. Jobs, blob
  ConfigMaps and workflow-owned volumes carry an `ownerReference` to it, so deleting the
  anchor lets Kubernetes garbage-collect the rest.
- **Blob**: a payload (the pickled function and arguments) or a result, stored in the
  configured store under a key (see [Blob key layout](#blob-key-layout)).

## Labels

`app.kubernetes.io/managed-by: sk8s` is on **every** object sk8s creates: anchors, Jobs and
their pods, blob ConfigMaps, PersistentVolumeClaims, Deployments, Services, shell Jobs, and
the namespace (when `sk8s config` creates it), ServiceAccount, Role and RoleBinding made by
`sk8s config`. sk8s watches Jobs by this
label, and `delete_volume` refuses to delete a PVC that lacks it.

`sk8s/kind` says what an object is. Objects that are simply "a Job of some workflow" or "a
blob of some workflow" carry no `sk8s/kind`, only `sk8s/workflow-id`.

| `sk8s/kind` | Object | Also labeled | Deleted by |
|---|---|---|---|
| `anchor` | The workflow anchor ConfigMap `sk8s-anchor-<wf-id>` | `sk8s/workflow-id=<wf-id>` | Client `close()` (default policy), `sk8s clean` / `sk8s.clean()`, `sk8s kill`, `WorkflowHandle.result()` / `.kill()` |
| `service` | The Deployment, Service and pods of `serve()`, and the service's payload ConfigMap | `sk8s/service=<name>` | `Service.delete()` only |
| `volume` | A PVC created inside a workflow (`create_volume(..., keep=False)`) | `sk8s/workflow-id=<wf-id>` | Its workflow's anchor (ownerReference), or `delete_volume` |
| `volume-keep` | A PVC created with `keep=True` | none | `delete_volume` only |
| `shell` | The Job behind `sk8s shell` | none | Its own 4-hour deadline plus the Job TTL, or `kubectl delete job` |

`sk8s/workflow-id=<wf-id>` appears on: the anchor, the workflow's Jobs and their pods, its
blob ConfigMaps, and its owned (`volume`) PVCs. It is deliberately **absent** from services,
service payloads and kept volumes, which is what keeps them out of every workflow-scoped
selector (`sk8s clean`, `kill`, `status`).

Selectors worth knowing:

```bash
kubectl get cm -n sk8s -l sk8s/kind=anchor                       # all workflows
kubectl get all,cm,pvc -n sk8s -l sk8s/workflow-id=wf-k3x9ab     # one workflow
kubectl get deploy,svc -n sk8s -l sk8s/kind=service              # services
kubectl get pvc -n sk8s -l sk8s/kind=volume-keep                 # volumes that outlive workflows
```

## Annotations

On an anchor:

| Annotation | Meaning |
|---|---|
| `sk8s/created-at` | UTC ISO-8601 creation time; retention is measured from it |
| `sk8s/retention-days` | The `retention_days` setting at creation (default 7) |
| `sk8s/detached` | `"true"` for `submit_workflow`, else `"false"` |
| `sk8s/driver-job` | Name of the detached driver Job (empty until recorded, and for non-detached workflows) |
| `sk8s/store` | The store setting in force (`k8s`, `s3://...`, ...); `clean` opens this store to delete the workflow's blobs |
| `sk8s/store-prefix` | Key prefix of the workflow's blobs (its workflow id) |

On a blob ConfigMap: `sk8s/key` holds the original blob key, because the ConfigMap *name*
is a lossy sanitization of it.

## Object names

| Object | Name |
|---|---|
| Workflow id | `wf-` plus six random characters of `a-z0-9` |
| Anchor | `sk8s-anchor-<wf-id>` |
| Job | `<name_prefix>-<six random characters>`; the prefix defaults to `job`, and detached drivers use `wfdriver` |
| Blob ConfigMap | `sk8s-` plus the lowercased key with every run of characters outside `a-z0-9-` replaced by `-` (for example `sk8s-wf-k3x9ab-job-q1w2e3-payload`) |
| Generated volume | `vol-` plus six random characters; explicit names must be DNS-1123 labels |
| Service | your `name`; a DNS-1123 label that starts with a letter, at most 63 characters |
| ServiceAccount, Role, RoleBinding | `sk8s` (the ServiceAccount name is the `service_account` setting) |

## Reserved workflow ids

`svc` and `doctor` can never be workflow ids. Creating a `Cluster` with
`workflow_id="svc"` or `"doctor"`, or running with `SK8S_WORKFLOW_ID` set to either (which
is how a worker pod adopts its workflow), raises `ValueError`. Generated ids (`wf-xxxxxx`)
cannot collide.

The reason is the blob key layout below: the first key segment is the workflow id, and
`svc/` and `doctor/` are the first segments of *non-workflow* keys. A workflow named `svc`
would share a key space, and a `delete_prefix("svc")` when it was deleted, with every
service's payload; `doctor` would mix up with the probe keys `sk8s doctor` writes.

## Blob key layout

Keys are `/`-separated. The first segment decides what a blob belongs to.

| Key | Written by | Removed when |
|---|---|---|
| `<wf-id>/<job-name>/payload` | The client, at submit | The job's future resolves (result, error or cancel), or the submit fails |
| `<wf-id>/<job-name>/result` | The worker, when the function finishes | The result is fetched (unless `keep_results=True`), the job is cancelled, or the workflow is deleted |
| `svc/<service-name>/payload` | `serve()` | `Service.delete()` (or `serve()` rolling back its own failed start) |
| `doctor/probe-<12 hex>` | `sk8s doctor`, as a write/read/delete round trip | Immediately, by doctor itself (even on failure, best effort) |

How a key maps to an actual location depends on the store:

| Store | Location of key `K` |
|---|---|
| `k8s` | ConfigMap `sk8s-<sanitized K>` in the namespace; URI `k8s://<namespace>/<name>` |
| `s3://bucket/prefix` | Object `prefix/K` in `bucket`; URI `s3://bucket/prefix/K` |
| `gs://bucket/prefix` | Object `prefix/K` in `bucket`; URI `gs://bucket/prefix/K` |
| `local:<dir>` | File `<dir>/K` |

In the `k8s` store the first key segment also becomes labels: a `svc/<name>/...` key gets
`sk8s/kind=service` and `sk8s/service=<name>`; any other key gets `sk8s/workflow-id=<first
segment>` (so a doctor probe is briefly labeled `sk8s/workflow-id=doctor`).
`delete_prefix(p)` removes `p` and everything under it by whole segments: `wf-a` removes
`wf-a/...` and never `wf-ab/...`. An empty prefix is refused.

## Ownership and retention

**Owned by the workflow anchor** (ownerReference, so they die with it): the workflow's Jobs
(controller reference), its blob ConfigMaps (`k8s` store only), and PVCs created with
`keep=False`. Object-store blobs cannot carry owner references; instead the anchor records
`sk8s/store` and `sk8s/store-prefix`, and every sk8s code path that deletes an anchor also
deletes that prefix from the store.

**Owned by nothing**: services (Deployment, Service, pod, payload blob) and `keep=True`
volumes. No workflow label, no ownerReference, never selected by `clean`. They go away only
through `Service.delete()` and `delete_volume()`.

**When a workflow goes away:**

| Event | Effect |
|---|---|
| Client process exits normally | `Cluster.close()` deletes the anchor **only if** this client created it, it is not detached, every future it issued has resolved, and no job used `keep_results`. Otherwise the workflow stays and ages out. |
| `submit_workflow` | The workflow is detached: the submitting process never deletes it. The driver Job's result is kept until you fetch it. |
| `WorkflowHandle.result()` | On success (with the default `delete=True`) deletes the whole workflow; on failure keeps it for inspection. |
| `WorkflowHandle.kill()`, `sk8s kill WF` | Deletes the workflow unconditionally, running or not. |
| `sk8s clean [--older-than D]`, `sk8s.clean()` | Deletes workflows older than their own `sk8s/retention-days` (or `D`) that have **no active Job**. |
| Worker start-up | Each job-mode worker runs the same sweep over its namespace (never its own workflow; `SK8S_SWEEP=0` turns it off). Service pods do not sweep. |
| Job finishes | Kubernetes removes the Job and its pods after `ttl_seconds_after_finished` (default 3600 s). |

`clean` never deletes an anchor younger than 300 seconds, whatever the limit (this closes
the window before a new workflow's first Job exists and makes `retention_days=0` safe), and
it skips an anchor whenever it cannot prove the workflow idle (annotations unreadable,
unknown store, failed Job listing). The one exception is `clean(workflow=<id>)`, which
deletes that workflow even if it is live. `clean` only ever selects ConfigMaps labeled
`sk8s/kind=anchor`, so nothing else in the namespace is at risk.

Invariants the design holds to (the integration suites exercise the kill, cascade,
kept-volume and service-delete paths): a payload never outlives its job; a result
lives until it is fetched, kept, or expired, whichever comes first; services and kept
volumes outlive everything except an explicit delete.

## Object-store lifecycle rules and `svc/`

sk8s does not configure bucket lifecycle (expiration) rules today. If you add one as a
backstop for blobs leaked by crashed clients, **do not let it expire `svc/`**.

A service's payload (`<prefix>/svc/<name>/payload`) belongs to no workflow and is read
again by the pod every time it starts: a restart, a scale-up or a rollout. If the payload
expires, the Deployment starts crash-looping. The same retention that is right for workflow
blobs is wrong for it.

Neither S3 nor GCS lifecycle filters can say "everything except this prefix", but they do
match raw key prefixes, and every generated workflow id starts with `wf-`. So scope the
rule to the workflow keys rather than to the whole store prefix:

| Store setting | Lifecycle rule prefix |
|---|---|
| `s3://bucket/sk8s` or `gs://bucket/sk8s` | `sk8s/wf-` |
| `s3://bucket` or `gs://bucket` (no prefix) | `wf-` |

That covers every `wf-<id>/...` blob and leaves `svc/` (and `doctor/`, which is removed
anyway) alone. Pick an expiry comfortably longer than your `retention_days` (default 7), so
the lifecycle rule is only a backstop and never races a detached workflow that is still
within its retention. Workflows whose id you chose yourself (`Cluster(workflow_id=...)`,
`attach`) do not start with `wf-` unless you named them so. Alternatively, keep services in
a store prefix of their own that no rule touches. The `k8s` store needs none of this: its
service payload ConfigMaps have no owner and are never selected by `clean`.
