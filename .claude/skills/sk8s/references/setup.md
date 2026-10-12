# sk8s setup: local and GKE, as user and as cluster admin

Config layering (everywhere): defaults < config file < `SK8S_*` env < kwargs.
The file is `~/.config/sk8s/config.toml` unless `SK8S_CONFIG=<path>` — **agents and
CI must always set `SK8S_CONFIG`** (and their own `KUBECONFIG`) so they never
clobber a human's global files. Every `Config` field has an env var
(`SK8S_NAMESPACE`, `SK8S_STORE`, `SK8S_IMAGE`, `SK8S_IMAGE_PREFIX`,
`SK8S_PULL_POLICY`, …) except `store_env`, which is file/kwargs-only.

Namespace resolution: explicit (file/env/kwarg) > kube-context namespace *if the
context sets one* > `"sk8s"`. Never "default" by default.

## Local (minikube)

```bash
pip install -e ".[dev]"                      # or plain `pip install sk8s` once released
minikube addons enable registry
kubectl -n kube-system port-forward --address localhost svc/registry 5000:80 &   # keep running
sk8s config --namespace sk8s --store k8s     # creates ns + SA/Role/RoleBinding, writes config
sk8s doctor                                  # all checks PASS before doing anything else
```

- Worker images on minikube go through the registry: build with
  `image_prefix = "localhost:5000/"` (or tag `localhost:5000/<name>` yourself),
  `--push`, and pods pull `localhost:5000/<name>`. Docker exempts localhost from
  TLS; the port-forward must be alive when you push.
- The **stock worker image has no scientific stack** and its Python minor version
  must match the client's. First real task on any new setup is usually an image
  build (see references/api.md).
- `pull_policy` default is `IfNotPresent`. Never set `Never` unless you fully
  understand minikube's image store; it turns a missing image into
  `ErrImageNeverPull` (typed, but avoidable).
- The ConfigMap store (`store = "k8s"`) is fine locally; blobs cap at ~950KB per
  object. For bigger results use MinIO (`s3://` + `store_endpoint`) or return
  paths, not payloads.

## GKE — cluster admin: onboarding a team namespace

Everything cloud-side is **plan-first**: run with `--dry-run` to see the exact
`gcloud` commands, then `--apply-cloud` to run them. Full narrative: `docs/gke.md`.

```bash
export KUBECONFIG="$PWD/.kubeconfig-gke"     # isolate; get-credentials switches context!
gcloud container clusters get-credentials CLUSTER --region REGION
export SK8S_CONFIG="$PWD/.sk8s-gke.toml"
sk8s config --provider gke --project PROJECT --region REGION \
  --namespace TEAM_NS --gsa sk8s-worker \
  --store gs://BUCKET/TEAM_NS/ --apply-cloud
sk8s doctor     # red on the first run ≈ IAM propagation; retry after ~60s before debugging
```

What that creates: the namespace + ServiceAccount `sk8s` + Role + RoleBinding
(k8s side), and GSA + bucket IAM + Workload Identity binding (cloud side). Key
facts the docs bury:

- **The Workload Identity binding is namespace-scoped.** Member
  `serviceAccount:PROJECT.svc.id.goog[TEAM_NS/sk8s]` — every new namespace needs
  its own member on the GSA, even when everything else is shared. `--apply-cloud`
  handles it; forgetting it is the #1 red-doctor cause (the in-cluster probe
  names it in the hint).
- **Topology choice** (undocumented; both work): one shared GSA + bucket with
  per-team namespaces and store *prefixes* (simplest, teams can read each other's
  blobs), or per-team GSA + bucket (real isolation, more IAM to manage). Prefer
  shared for one trusting org, per-team GSA when teams must not read each other's
  results.
- **Images**: one Artifact Registry repo, one worker image per Python minor
  version, shared by all teams — image builds are per-dependency-set, not
  per-team. Nodes pull via the default compute SA (usually works; org policy can
  break it — `sk8s config` prints the fix binding).
- **Autopilot facts**: no privileged pods (`privileged=True` ⇒ typed
  `JobRejected`), resource requests are mutated upward (never assert exact
  requests), scale-from-zero adds 1–2 min to the first pod. RWX volumes =
  Filestore = **1 TiB minimum ≈ $0.30/h** — prefer ReadOnlyMany reference volumes
  (references/advanced.md) for read-mostly data.

### Humans (what team members need, beyond the workload setup)

Derived from what the client actually does; grant at whatever granularity your
org uses:

- **Cluster access**: `roles/container.clusterViewer` (or any role allowing
  `get-credentials`), plus a RoleBinding of the namespace's existing `sk8s` Role
  to the human users/group — the client needs exactly those verbs (create jobs,
  read pods/logs, configmaps, PVCs, deployments, services).
- **Store**: the *client* reads results from `gs://` with the user's own ADC
  (`gcloud auth application-default login`), so members need object
  read/write/delete on the bucket (e.g. `roles/storage.objectAdmin` on it).
- **Images**: `roles/artifactregistry.writer` only for whoever builds images.
- `sk8s doctor`'s role-permissions check reads the Role object itself (RBAC
  read); a member without RBAC read sees that one check fail even on a healthy
  namespace — it needs cluster RBAC-read or an admin-run doctor.

## GKE — user on an already-onboarded namespace

```bash
pip install "sk8s[gcs]"                      # the gcs extra, or [s3] for s3://
gcloud auth application-default login        # client-side store access uses YOUR ADC
export KUBECONFIG=...                        # a kubeconfig with the cluster context
export SK8S_CONFIG="$PWD/.sk8s.toml"
```

```toml
# .sk8s.toml — or let the admin's `sk8s config` output be your template
namespace = "TEAM_NS"
store = "gs://BUCKET/TEAM_NS/"
image = "REGION-docker.pkg.dev/PROJECT/REPO/IMAGE:TAG"   # must match your Python minor!
```

Then `sk8s doctor`, then submit. If doctor's in-cluster probe passes, Workload
Identity, the image, and the store are all proven end-to-end.
