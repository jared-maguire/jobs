# sk8s setup: local and GKE, as user and as cluster admin

Config layering (everywhere): defaults < config file < `SK8S_*` env < kwargs.
The file is `~/.config/sk8s/config.toml` unless `SK8S_CONFIG=<path>` — **agents and
CI must always set `SK8S_CONFIG`** (and their own `KUBECONFIG`) so they never
clobber a human's global files. Every `Config` field has an env var
(`SK8S_NAMESPACE`, `SK8S_STORE`, `SK8S_IMAGE`, `SK8S_IMAGE_PREFIX`,
`SK8S_PULL_POLICY`, …) except `store_env`, which is file/kwargs-only.

Namespace resolution in the library: explicit (file/env/kwarg) > kube-context
namespace *if the context sets one* > `"sk8s"`; inside a pod, the pod's own
namespace. (`sk8s config` itself never infers from the context — its
`--namespace` simply defaults to `sk8s`.) Never "default" by default.

Pre-release status: sk8s is not on PyPI and **no worker image is published
anywhere — building your own base is the intended workflow**, not a workaround.
Install with `pip install -e ".[dev]"` (add `,gcs` / `,s3` extras as needed) from
a checkout.

## Local (minikube)

```bash
pip install -e ".[dev]"
minikube addons enable registry
kubectl -n kube-system port-forward --address localhost svc/registry 5000:80 &   # keep running
sk8s config --namespace sk8s --store k8s     # creates ns + SA/Role/RoleBinding, writes config

# Build YOUR base worker image (Python minor must match your client!) and push it:
docker build -f docker/Dockerfile.worker --build-arg PYTHON_VERSION=3.12 \
  -t localhost:5000/sk8s-base . && docker push localhost:5000/sk8s-base
echo 'image = "localhost:5000/sk8s-base"' >> "$SK8S_CONFIG"   # or SK8S_IMAGE

sk8s doctor                                  # all checks PASS before doing anything else
```

- Pods pull what `image` (or `SK8S_IMAGE`) names. `image_prefix` is **not** a
  build mechanism — it only prefixes the *default* image name when `image` is
  unset; on minikube, tag images `localhost:5000/<name>` yourself and set
  `image=`. Docker exempts localhost from TLS; the port-forward must be alive
  when you push.
- Deps beyond the base: `sk8s image build --tag localhost:5000/science --base
  localhost:5000/sk8s-base --pip numpy --push` (see references/api.md for the
  apt/Dockerfile route).
- `pull_policy` default is `IfNotPresent`. The README documents a no-registry
  minikube path using `SK8S_PULL_POLICY=Never` + `minikube image load`; prefer
  the registry — `Never` turns any missing image into `ErrImageNeverPull`.
- The ConfigMap store (`store = "k8s"`) is fine locally; blobs cap at ~950KB per
  object. For bigger results use MinIO (`s3://` + `store_endpoint`) or return
  paths, not payloads.

## GKE — cluster admin: onboarding a team namespace

Prereqs: `gcloud` with `gke-gcloud-auth-plugin`, `docker`, and the
`container`/`artifactregistry` APIs enabled. Everything cloud-side in sk8s is
**plan-first**: `--dry-run` prints the exact `gcloud` commands, `--apply-cloud`
runs them. Full narrative: `docs/gke.md`.

```bash
export KUBECONFIG="$PWD/.kubeconfig-gke"     # isolate; get-credentials switches context!
gcloud container clusters get-credentials CLUSTER --region REGION
export SK8S_CONFIG="$PWD/.sk8s-gke.toml"

# One-time durables sk8s does NOT create: the bucket and the image repo.
gcloud storage buckets create gs://BUCKET --location REGION --uniform-bucket-level-access
gcloud artifacts repositories create REPO --repository-format docker --location REGION
gcloud auth configure-docker REGION-docker.pkg.dev

# Build + push the worker base (one per Python minor version, shared by all teams):
docker build -f docker/Dockerfile.worker --build-arg PYTHON_VERSION=3.12 \
  -t REGION-docker.pkg.dev/PROJECT/REPO/sk8s-base:py3.12 . \
  && docker push REGION-docker.pkg.dev/PROJECT/REPO/sk8s-base:py3.12

sk8s config --provider gke --project PROJECT --region REGION \
  --namespace TEAM_NS --gsa sk8s-worker \
  --store gs://BUCKET/TEAM_NS/ --apply-cloud
sk8s doctor     # red on the first run ≈ IAM propagation; retry after ~60s before debugging
```

`--apply-cloud` creates: namespace + ServiceAccount `sk8s` (annotated for
Workload Identity) + Role + RoleBinding (k8s side); GSA + bucket IAM + the WI
binding (cloud side). Key facts:

- **The Workload Identity binding is namespace-scoped.** Member
  `serviceAccount:PROJECT.svc.id.goog[TEAM_NS/sk8s]` — every new namespace needs
  its own member on the GSA, even when everything else is shared. `--apply-cloud`
  handles it; forgetting it is the #1 red-doctor cause (the in-cluster probe
  names it in the hint).
- **Topology choice** (the docs show only the shared variant; both work): one
  shared GSA + bucket with per-team namespaces and store *prefixes* (simplest;
  teams can read each other's blobs), or per-team GSA + bucket (real isolation,
  more IAM). Prefer shared for one trusting org.
- **Autopilot facts**: no privileged pods (`privileged=True` ⇒ typed
  `JobRejected`), resource requests are mutated upward (never assert exact
  requests), scale-from-zero adds 1–2 min to the first pod. RWX volumes =
  Filestore = **1 TiB minimum ≈ $0.20–0.30/h** — prefer ReadOnlyMany reference
  volumes (references/advanced.md) for read-mostly data.

### Humans (what team members need, beyond the workload setup)

Derived from what the client actually does; exact IAM role names are typical
choices, not gospel:

- **Cluster access**: typically `roles/container.clusterViewer` (anything
  allowing `get-credentials`), plus namespace RBAC granting the `sk8s` Role's
  rule set (get/list/watch/create/delete/patch on jobs, pods, pods/log,
  configmaps, PVCs, deployments, services + read-only events). **Create a
  separate RoleBinding for humans** — do not add subjects to the `sk8s`
  RoleBinding, because the next `sk8s config` re-apply replaces it and drops
  them:
  ```bash
  kubectl -n TEAM_NS create rolebinding sk8s-humans --role=sk8s \
    --group=team-x@YOURDOMAIN   # or repeated --user=...
  ```
- **Store**: the *client* reads and deletes results on `gs://` with the user's
  own ADC (`gcloud auth application-default login`), so members need object
  read/write/delete on the bucket (e.g. `roles/storage.objectAdmin` on it).
- **Images**: `roles/artifactregistry.writer` only for whoever builds images.
- `sk8s doctor` run by a member with only the namespace Role: the role check
  (RBAC read), the namespace check, and the storage-class check are
  cluster-level reads the Role doesn't grant — expect up to three spurious
  failures on a healthy namespace. Admin-run doctor is the authoritative one.

## GKE — user on an already-onboarded namespace

```bash
pip install -e ".[gcs]"                      # from a checkout, pre-release
gcloud auth application-default login        # client-side store access uses YOUR ADC
export KUBECONFIG=...                        # a kubeconfig with the cluster context
export SK8S_CONFIG="$PWD/.sk8s.toml"
```

```toml
# .sk8s.toml — or let the admin's `sk8s config` output be your template
namespace = "TEAM_NS"
store = "gs://BUCKET/TEAM_NS/"
image = "REGION-docker.pkg.dev/PROJECT/REPO/sk8s-base:py3.12"   # must match your Python minor!
```

Then `sk8s doctor`, then submit. On a `gs://` store, a passing in-cluster probe
proves Workload Identity, the image, and the store end-to-end.
