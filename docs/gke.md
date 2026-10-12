# Running sk8s on GKE (Autopilot)

This guide takes you from "I have a Google Cloud project" to "`sk8s.map` runs on a GKE
Autopilot cluster", with the worker image in Artifact Registry, results in a Cloud
Storage bucket, and the workers authenticating through Workload Identity (no key files).

It was written by running every command and snippet below, in order, against a real
Autopilot cluster (GKE 1.36, `us-central1`), once on an existing setup and once from
scratch in a throwaway namespace/bucket/registry. Output blocks marked `text` are real
output, trimmed only where it says so, with the project, account and local paths replaced by
placeholders (`my-project`, `you@example.com`, `/path/to/sk8s`). IDs, ages and timings will
differ for you.

Contents: [1. Prerequisites](#1-prerequisites) -
[2. Worker image](#2-worker-image) -
[3. Bucket, identity and `sk8s config`](#3-bucket-identity-and-sk8s-config) -
[4. `sk8s doctor`](#4-sk8s-doctor) - [5. First jobs](#5-first-jobs) -
[6. Read-many volumes](#6-read-many-volumes) - [7. Autopilot notes](#7-autopilot-notes) - [8. What it costs](#8-what-it-costs) -
[9. Known behavior: raw anchor deletion](#9-known-behavior-deleting-an-anchor-by-hand) -
[10. Teardown](#10-teardown)

> **Money first.** Almost everything here costs pennies. The one thing that does not is
> a **Filestore** instance (ReadWriteMany volumes): a 1 TiB minimum, billed every hour it
> exists. sk8s never creates one unless you ask for a `ReadWriteMany` volume. See
> [What it costs](#8-what-it-costs) before you try it.

## 1. Prerequisites

You need:

- a Google Cloud project with billing enabled, and `gcloud`, `kubectl`, `docker` and the
  `gke-gcloud-auth-plugin` (`gcloud components install gke-gcloud-auth-plugin`, or your
  package manager's) on your machine;
- an sk8s checkout (Python 3.10 or newer) with the `gcs` extra, run from the checkout root
  (the worker image is built from it):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[gcs]"
```

The commands below use these variables, and the later `gcloud` commands follow your
`gcloud` default project rather than passing `--project` each time. To target a different
project, run `gcloud config set project <id>` first instead of editing `PROJECT` — editing
only the variable would point sk8s at one project while `gcloud` creates the repository
and bucket in another. Everything else is a name you choose:

```bash
export PROJECT=$(gcloud config get project 2>/dev/null)
export REGION=us-central1
export CLUSTER=autopilot-cluster-1
export NAMESPACE=sk8s
export BUCKET=$PROJECT-sk8s
export GSA=sk8s-worker
export AR_REPO=sk8s
```

Log in and enable the APIs. Enabling an API that is already on is a no-op, so this is
safe to re-run (`file.googleapis.com` is only needed if you will ever use a
`ReadWriteMany` volume, and enabling it costs nothing):

```bash skip
gcloud auth login
gcloud auth application-default login   # the sk8s client library reads these credentials
gcloud services enable container.googleapis.com artifactregistry.googleapis.com file.googleapis.com
```

Check what is on:

```bash
gcloud services list --enabled 2>/dev/null | grep -E "^(container|artifactregistry|file)\.googleapis"
```

```text
artifactregistry.googleapis.com      Artifact Registry API
container.googleapis.com             Kubernetes Engine API
file.googleapis.com                  Cloud Filestore API
```

### An Autopilot cluster

If you do not have one, create it (a few minutes; a cluster costs money while it exists,
see [What it costs](#8-what-it-costs)). Autopilot clusters have Workload Identity on by
default, which sk8s relies on:

```bash skip
gcloud container clusters create-auto $CLUSTER --region $REGION
```

If you already have a cluster, skip that. Either way, confirm what you have, including the
Workload Identity pool (`<project>.svc.id.goog`):

```bash
gcloud container clusters list 2>/dev/null
gcloud container clusters describe $CLUSTER --region $REGION --format='value(workloadIdentityConfig.workloadPool)' 2>/dev/null
```

```text
NAME                 LOCATION     MASTER_VERSION      MASTER_IP      MACHINE_TYPE   NODE_VERSION        NUM_NODES  STATUS   STACK_TYPE
autopilot-cluster-1  us-central1  1.36.4-gke.1495000  203.0.113.10  ek-standard-8  1.36.4-gke.1247000  3          RUNNING  IPV4
my-project.svc.id.goog
```

### Credentials, without disturbing your other clusters

`gcloud container clusters get-credentials` **writes the cluster into your kubeconfig
and makes it the current context**. If you also use minikube or another cluster, the next
plain `kubectl` or `sk8s` command anywhere on your machine will talk to GKE. The same goes
for `sk8s config`, which writes `~/.config/sk8s/config.toml`: that file is global to your
user.

To keep this cluster to yourself, point both at files of their own *before* running
anything else that touches the cluster (every later command in this guide assumes you did;
the two files are untracked files in your checkout):

```bash
export KUBECONFIG="$PWD/.kubeconfig-gke"
export SK8S_CONFIG="$PWD/.sk8s-gke.toml"
```

Anything you run with these exported leaves `~/.kube/config` and
`~/.config/sk8s/config.toml` alone. (`unset` them, or open a new shell, to go back.
Prefer the global files? Just skip these two lines; the commands below are identical.)

Now fetch credentials. With `KUBECONFIG` set as above this creates the file (or updates
the cluster's entry if it exists), and re-running is harmless:

```bash
gcloud container clusters get-credentials $CLUSTER --region $REGION
kubectl config current-context
```

```text
Fetching cluster endpoint and auth data.
kubeconfig entry generated for autopilot-cluster-1.
gke_my-project_us-central1_autopilot-cluster-1
```

## 2. Worker image

Jobs run in a worker image that contains sk8s. There are no published images yet, so you
build one from your checkout and push it to Artifact Registry. First the repository and
Docker's credentials for it. `create` on an existing repository fails with
`ALREADY_EXISTS` and changes nothing; `configure-docker` is idempotent too:

```bash
gcloud artifacts repositories create $AR_REPO --repository-format=docker --location=$REGION
gcloud auth configure-docker $REGION-docker.pkg.dev
```

```text
Create request issued for: [sk8s]
Waiting for operation [projects/my-project/locations/us-central1/operations/...] to complete...
......done.
Created repository [sk8s].
...
Adding credentials for: us-central1-docker.pkg.dev
```

(On a re-run the first command prints
`ERROR: (gcloud.artifacts.repositories.create) ALREADY_EXISTS: the repository already exists`,
and `configure-docker` says `gcloud credential helpers already registered correctly.`)

`configure-docker` adds a `credHelpers` entry to `~/.docker/config.json`. That is the one
global file this guide asks `gcloud` to touch, and it only affects `docker` pushes to
`*-docker.pkg.dev`.

**The image name and Python version are not arbitrary.** When you submit a job, sk8s
pickles your function with `cloudpickle`, and the worker refuses a payload from a
different Python minor version (the serialize handshake; you get a `VersionMismatch`
rather than a mysterious unpickling error). To make the right image easy to find, sk8s's
default image name is
`<image_prefix>ghcr.io/jared-maguire/sk8s:<sk8s version>-py<client Python major.minor>`,
and `sk8s config --provider gke` (next section) sets `image_prefix` to your Artifact
Registry repo. So you build with the same Python version as your client, and tag the image
exactly that way:

```bash
PYV=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SK8S_VERSION=$(python -c 'import sk8s; print(sk8s.__version__)')
IMAGE=$REGION-docker.pkg.dev/$PROJECT/$AR_REPO/ghcr.io/jared-maguire/sk8s:$SK8S_VERSION-py$PYV
echo $IMAGE
docker build -f docker/Dockerfile.worker --build-arg PYTHON_VERSION=$PYV -t $IMAGE .
docker push $IMAGE
```

```text
us-central1-docker.pkg.dev/my-project/sk8s/ghcr.io/jared-maguire/sk8s:2.0.0a0-py3.14
...
2.0.0a0-py3.14: digest: sha256:... size: 2059
```

`Dockerfile.worker` installs sk8s with the `s3` and `gcs` extras, so the worker can reach
a `gs://` store. If you rebuild after changing sk8s, remember an Artifact Registry tag is
**mutable and shared**: re-pushing it changes what everyone pulling that tag gets, while a
node that already cached the old digest keeps running it (nodes use the default
`IfNotPresent` pull policy). Autopilot nodes are short-lived, so this rarely bites, but
the clean route is a fresh tag — push as `...:<version>-r2` and point sk8s at it with
`image = "..."` in your config file (or `SK8S_IMAGE`); `pull_policy="Always"` is the blunt
alternative.

Nodes pull from the repository with the cluster's node service account — by default the
project's default compute service account, whose standard grants can read same-project
repositories (this worked out of the box in the runs for this guide, but an org policy
that trims those grants can change it). If pulls are denied, or for a repository in
another project, `sk8s config` prints the exact
`gcloud artifacts repositories add-iam-policy-binding` you need.

## 3. Bucket, identity and `sk8s config`

Results (and the pickled functions) travel through a bucket. Create one with uniform
bucket-level access. As before, an existing bucket produces an error and no change:

```bash
gcloud storage buckets create gs://$BUCKET --location $REGION --uniform-bucket-level-access
```

```text
Creating gs://my-project-sk8s/...
```

(Re-run: `ERROR: (gcloud.storage.buckets.create) HTTPError 409: Your previous request to
create the named bucket succeeded and you already own it.`)

Bucket names are global; `$PROJECT-sk8s` is a convention that is unlikely to collide.

### The plan

`sk8s config --provider gke` prepares the namespace *and* the Google side. It always
prints the Google Cloud commands it would run; add `--dry-run` to apply nothing at all:

```bash
sk8s config --provider gke --project $PROJECT --region $REGION --repo $AR_REPO \
  --namespace $NAMESPACE --store gs://$BUCKET/sk8s/ --gsa $GSA --dry-run
```

```text
ensure Namespace sk8s
ensure ServiceAccount sk8s in sk8s
annotate ServiceAccount sk8s in sk8s: iam.gke.io/gcp-service-account=sk8s-worker@my-project.iam.gserviceaccount.com
create-or-replace Role sk8s in sk8s: get/list/watch/create/delete/patch on pods,pods/log,configmaps,persistentvolumeclaims,services [core]
create-or-replace Role sk8s in sk8s: get/list/watch/create/delete/patch on jobs [batch]
create-or-replace Role sk8s in sk8s: get/list/watch/create/delete/patch on deployments [apps]
create-or-replace Role sk8s in sk8s: get/list/watch on events [core]
create-or-replace RoleBinding sk8s in sk8s: Role sk8s -> ServiceAccount sk8s

cloud plan (not run):
# create Google service account sk8s-worker@my-project.iam.gserviceaccount.com
gcloud iam service-accounts create sk8s-worker --project my-project --display-name 'sk8s worker'
# grant roles/storage.objectAdmin on gs://my-project-sk8s to sk8s-worker@my-project.iam.gserviceaccount.com
gcloud storage buckets add-iam-policy-binding gs://my-project-sk8s --member serviceAccount:sk8s-worker@my-project.iam.gserviceaccount.com --role roles/storage.objectAdmin --project my-project
# let Kubernetes ServiceAccount sk8s/sk8s act as sk8s-worker@my-project.iam.gserviceaccount.com
gcloud iam service-accounts add-iam-policy-binding sk8s-worker@my-project.iam.gserviceaccount.com --role roles/iam.workloadIdentityUser --member 'serviceAccount:my-project.svc.id.goog[sk8s/sk8s]' --project my-project
# note: Artifact Registry repo us-central1-docker.pkg.dev/my-project/sk8s/: nodes need roles/artifactregistry.reader (...). Not run automatically.
# note: IAM is eventually consistent: if the Workload Identity binding reports the GSA doesn't exist yet, wait ~30s and re-run (steps are idempotent)
```

The three Google commands are the whole identity story:

1. a **Google service account** (GSA) for workers to act as;
2. `roles/storage.objectAdmin` for it on **this bucket only** (`--bucket-role` changes the
   role);
3. a **Workload Identity** binding that lets the Kubernetes ServiceAccount `sk8s/sk8s`
   impersonate the GSA. The Kubernetes ServiceAccount carries the annotation
   `iam.gke.io/gcp-service-account=<gsa>`, which is what makes pods using it get the GSA's
   credentials from the metadata server.

> **The Workload Identity member is namespace-scoped.** The member
> `serviceAccount:<project>.svc.id.goog[sk8s/sk8s]` names namespace `sk8s`. A second
> namespace (say `team-b`) needs its own member on the GSA; running
> `sk8s config --provider gke --namespace team-b --gsa ... --apply-cloud` adds it. The
> bucket and GSA are shared.

### Apply it

`--apply-cloud` runs the printed commands as well as creating the Kubernetes objects. The
steps are safe to repeat: the GSA is reported `already done`, and the two IAM bindings
are add-if-missing, so re-running converges instead of failing:

```bash
sk8s config --provider gke --project $PROJECT --region $REGION --repo $AR_REPO \
  --namespace $NAMESPACE --store gs://$BUCKET/sk8s/ --gsa $GSA --apply-cloud
```

```text
cloud: create Google service account sk8s-worker@my-project.iam.gserviceaccount.com
Created service account [sk8s-worker].
cloud: grant roles/storage.objectAdmin on gs://my-project-sk8s to sk8s-worker@my-project.iam.gserviceaccount.com
bindings:
...
- members:
  - serviceAccount:sk8s-worker@my-project.iam.gserviceaccount.com
  role: roles/storage.objectAdmin
...
cloud: let Kubernetes ServiceAccount sk8s/sk8s act as sk8s-worker@my-project.iam.gserviceaccount.com
Updated IAM policy for serviceAccount [sk8s-worker@my-project.iam.gserviceaccount.com].
bindings:
- members:
  - serviceAccount:my-project.svc.id.goog[sk8s/sk8s]
  role: roles/iam.workloadIdentityUser
...
note: Artifact Registry repo us-central1-docker.pkg.dev/my-project/sk8s/: nodes need roles/artifactregistry.reader (...). Not run automatically.
Namespace sk8s: created
ServiceAccount sk8s: created
Role sk8s: created
RoleBinding sk8s: created
wrote /path/to/sk8s/.sk8s-gke.toml
```

(On a re-run the GSA line ends `: already done` and the Kubernetes objects report
`exists` / `unchanged`.)

**If this is the first time the service account is created, the next step can fail.** IAM
is eventually consistent, and the bucket binding may run before the new account is
visible to it (this is real output from a from-scratch run, with a throwaway account name;
another from-scratch run did not hit it):

```text
cloud: create Google service account sk8s-tdocs@my-project.iam.gserviceaccount.com
Created service account [sk8s-tdocs].
cloud: grant roles/storage.objectAdmin on gs://my-project-sk8s-tdocs to sk8s-tdocs@my-project.iam.gserviceaccount.com
ERROR: (gcloud.storage.buckets.add-iam-policy-binding) HTTPError 400: Service account sk8s-tdocs@my-project.iam.gserviceaccount.com does not exist.
sk8s: error: gcloud step failed (exit 1): gcloud storage buckets add-iam-policy-binding ...
```

`sk8s config` stops there, before creating the Kubernetes objects or writing the config
file. Nothing is half-applied that the command cannot pick up again: wait about 30 seconds
and run the same command again. It reports the account as `already done` and carries on.
(This is what the "`sk8s config` plan" note about eventual consistency means.)

Without `--apply-cloud` (and without `--dry-run`) sk8s creates the Kubernetes objects,
writes the config, and *prints* the Google commands for you or your platform team to run;
that is the right mode when you lack IAM permissions.

The config file it wrote is small and plain. Edit it by hand if you like; `sk8s config`
keeps keys it did not write:

```bash
cat "$SK8S_CONFIG"
```

```text
namespace = "sk8s"
store = "gs://my-project-sk8s/sk8s/"
image_prefix = "us-central1-docker.pkg.dev/my-project/sk8s/"
```

`SK8S_CONFIG=<path>` is also the way to give CI or an agent its own configuration (a
different namespace, store or cluster) without touching the config of the person who owns
the machine. Settings layer as: defaults, the config file, `SK8S_*` environment variables,
then keyword arguments.

## 4. `sk8s doctor`

On a brand-new setup this first run may be **red** for a minute or two while IAM catches
up; see [IAM takes a minute](#iam-takes-a-minute) before you debug anything.

```bash
sk8s doctor
```

```text
PASS  cluster reachable
PASS  namespace exists
PASS  service account exists
PASS  role permissions
PASS  store writable
PASS  storage class exists
PASS  worker identity (in-cluster store probe)
```

The first six checks run from your machine. The last one is the one that matters on GKE,
and it is the only check that cannot be done from your laptop: doctor runs a tiny **job in
the cluster**, as the `sk8s` ServiceAccount, with your worker image, and that pod writes a
probe object to your store, reads it back, and deletes it. A pass proves the whole chain
at once: the image pulled from Artifact Registry, the pod got the GSA's credentials
through Workload Identity, and the GSA can read **and write** the bucket. (The probe's
objects live under `doctor/` in the store and are removed again.)

Because it starts a pod, it can take a while the first time: on a quiet Autopilot cluster
the first pod waits for a node to be provisioned and the image pulled. In live runs during
this project's GKE bring-up that was up to about two minutes for the very first pod after idle, and
15 to 30 seconds when nodes were warm.

### What failure looks like

The classic failure is a namespace with no Workload Identity member. To see it, configure
a second namespace *without* `--apply-cloud` (so Google is untouched) and run doctor
there. This is exactly what a forgotten `--apply-cloud` for a new namespace gives you. The
demo uses its own config file via `SK8S_CONFIG`, so your real configuration is not
changed:

```bash
export DEMO_NS=$NAMESPACE-demo
SK8S_CONFIG="$PWD/.sk8s-demo.toml" sk8s config --provider gke --project $PROJECT --region $REGION --repo $AR_REPO \
  --namespace $DEMO_NS --store gs://$BUCKET/$DEMO_NS/ --gsa $GSA > /dev/null
SK8S_CONFIG="$PWD/.sk8s-demo.toml" sk8s doctor 2>&1 | head -8 | cut -c1-420
```

```text
PASS  cluster reachable
PASS  namespace exists
PASS  service account exists
PASS  role permissions
PASS  store writable
PASS  storage class exists
FAIL  worker identity (in-cluster store probe)
      a pod using ServiceAccount 'sk8s' cannot use store 'gs://my-project-sk8s/sk8s-demo/': this is usually Workload Identity misconfiguration. Check (1) the ServiceAccount carries annotation iam.gke.io/gcp-service-account=<gsa> (sk8s config --provider gke --gsa ...); (2) that GSA has roles/iam.workloadIdentityUser for member serviceAccount:<project>.svc.id.goog[sk8s-demo/sk8s]; (3) the GSA has roles/storage.objectAdmin on the bucket; (4) Workload Identity is enabled on the cluster and node pool. 'sk8s config --provider gke ... --apply-cloud' sets up 1-3. (probe error: PodFailed: job doctor-9zi6nm failed: ...
```

(The full output continues with the failed pod's log: a long Python traceback that ends in
`google.auth.exceptions.RefreshError: ... Status: 403 Response: ... Unable to generate access token; IAM returned 403 Forbidden: Permission 'iam.serviceAccounts.getAccessToken' denied on resource (or it may not exist).`)

Read it from the top: the first six checks are fine (the Kubernetes side is healthy), the
pod ran, and the worker could not get a token for the GSA. The hint lists the four things
to check, in the order people actually break them; `--apply-cloud` fixes 1 to 3, and (4)
is on by default for Autopilot. A different signature you may meet is
`IAM returned 404 ... Gaia id not found for email <gsa>`: the GSA itself does not exist
yet (step 1 of the plan never ran).

Fix it by re-running `sk8s config` for that namespace with `--apply-cloud` (only the new
namespace's member is added), then wait for IAM to catch up:

```bash
SK8S_CONFIG="$PWD/.sk8s-demo.toml" sk8s config --provider gke --project $PROJECT --region $REGION --repo $AR_REPO \
  --namespace $DEMO_NS --store gs://$BUCKET/$DEMO_NS/ --gsa $GSA --apply-cloud 2>&1 | grep -E "^cloud|^Updated"
for i in 1 2 3 4 5 6; do SK8S_CONFIG="$PWD/.sk8s-demo.toml" sk8s doctor >/dev/null 2>&1 && break; sleep 30; done
SK8S_CONFIG="$PWD/.sk8s-demo.toml" sk8s doctor | tail -1
```

```text
cloud: create Google service account sk8s-worker@my-project.iam.gserviceaccount.com: already done
cloud: grant roles/storage.objectAdmin on gs://my-project-sk8s to sk8s-worker@my-project.iam.gserviceaccount.com
cloud: let Kubernetes ServiceAccount sk8s-demo/sk8s act as sk8s-worker@my-project.iam.gserviceaccount.com
Updated IAM policy for serviceAccount [sk8s-worker@my-project.iam.gserviceaccount.com].
PASS  worker identity (in-cluster store probe)
```

Then remove the demo (the namespace, its member on the GSA, and its config file):

```bash
kubectl delete namespace $DEMO_NS
gcloud iam service-accounts remove-iam-policy-binding $GSA@$PROJECT.iam.gserviceaccount.com --role roles/iam.workloadIdentityUser --member "serviceAccount:$PROJECT.svc.id.goog[$DEMO_NS/sk8s]" --project $PROJECT 2>&1 | grep -A3 "^bindings"
rm -f "$PWD/.sk8s-demo.toml"
```

```text
namespace "sk8s-demo" deleted
bindings:
- members:
  - serviceAccount:my-project.svc.id.goog[sk8s/sk8s]
  role: roles/iam.workloadIdentityUser
```

### IAM takes a minute

IAM is eventually consistent. Right after `--apply-cloud` creates or changes a binding,
doctor may still fail with the same 403 `getAccessToken` signature even though the
binding is correct. Wait a minute and re-run. In the runs for this guide, fixing the
namespace above went from red (5 s after the apply) to green within two minutes, the
very first setup needed one retry about 60 seconds later, and in the from-scratch run
the first `sk8s doctor` right after `--apply-cloud` was red with exactly this signature.
If it is still red after five minutes, the signature is real; follow the hint.

## 5. First jobs

Run each of these as a script (they use your `SK8S_CONFIG`, so the same shell that ran
doctor). Warm nodes make a job take roughly 10 to 20 seconds; a cold node adds the scale-up time
described in [Autopilot notes](#7-autopilot-notes).

A single job, then a future:

```python
# first_job.py
import sk8s

print(sk8s.run(lambda x: x * 2, 21))  # one function, one Kubernetes Job -> 42

fut = sk8s.submit(lambda x: x + 1, 41)  # same, but returns a Future immediately
print(fut.result(timeout=300))          # 42
```

```bash
python first_job.py
```

```text
42
42
```

`map` fans out over many inputs; `batch_size` packs several items into each Job, and an item
that raises fails only itself:

```python
# first_map.py
import sk8s

print(sk8s.map(lambda x: x ** 2, range(5)))  # [0, 1, 4, 9, 16]

# 100 items, 25 per Job -> 4 Jobs instead of 100
print(sum(sk8s.map(lambda x: x ** 2, range(100), batch_size=25)))  # 328350
```

```bash
python first_map.py
```

```text
[0, 1, 4, 9, 16]
328350
```

A **detached workflow**: the driver runs in the cluster and submits its own jobs, so your
laptop can go to sleep. The driver talks to the bucket with Workload Identity too (a job
that submits jobs needs nothing extra):

```python
# first_workflow.py
import sk8s


def pipeline(n):
    # Runs inside the cluster.
    print("squaring", n, "numbers", flush=True)  # pod stdout is block-buffered
    return sum(sk8s.map(lambda x: x * x, range(n)))


wf = sk8s.submit_workflow(pipeline, 10)  # returns immediately
print("submitted", wf.id)                # e.g. wf-k3x9ab

# Later, from this or any other process:
same = sk8s.attach(wf.id)
print(same.status())                     # {'driver': 'active', 'jobs': {...}}
print(same.result(timeout=300))          # 285, then the finished workflow is deleted
```

```bash
python first_workflow.py
```

```text
submitted wf-7qyo1c
{'driver': 'active', 'jobs': {'active': 1, 'succeeded': 0, 'failed': 0}}
285
```

`sk8s workflows`, `sk8s logs <id>` and `sk8s kill <id>` list, read and delete workflows
from the command line. A detached driver's log is only what it prints (`sk8s logs` and `sk8s explain` show an empty log as "driver produced no output"), so `print(..., flush=True)` a line per stage from the driver: it is the cheapest progress channel there is, and the first thing `sk8s explain` shows (its last log lines). Services (`sk8s.serve`) and `ReadWriteOnce` volumes
(`sk8s.create_volume`) work on GKE as they do anywhere; the README tour covers them, and
the integration suites run against this setup. Volumes that many pods on many nodes read
at once are [section 6](#6-read-many-volumes). Check that the session left nothing
behind:

```bash
sk8s workflows
kubectl -n $NAMESPACE get jobs,pods,configmaps,pvc
gcloud storage ls -r gs://$BUCKET/ 2>/dev/null
```

```text
no workflows in namespace sk8s
NAME                         DATA   AGE
configmap/kube-root-ca.crt   1      80m
gs://my-project-sk8s/:
```

## 6. Read-many volumes

A `ReadWriteOnce` volume (the default) attaches to one node at a time, so only the pods on
that node can use it. For the case where you populate a big reference once (a genome index,
a model) and then run **many readers at the same time, on many nodes**, sk8s offers a
volume that is written once and read by any number of pods, at persistent-disk prices: no
Filestore, no 1 TiB minimum. Ask for it when you create the volume (its access modes are fixed then):

```python skip
sk8s.create_volume("500Gi", name="index", access_modes=["ReadWriteOnce", "ReadOnlyMany"])
```

The rest of this section is that volume's life, run in order against the same cluster as
the earlier sections (the commands assume the `NAMESPACE` and `SK8S_CONFIG` of
[section 1](#credentials-without-disturbing-your-other-clusters)).

### How it works

Every `volumes=` mount says how it mounts the volume, and **the default is read-only**:

| `volumes=` | Meaning |
|---|---|
| `"index"` | one read-only mount at `/mnt/index` |
| `["index", "refs"]` | several read-only mounts |
| `{"index": {"access_mode": "rw", "mountpoint": "/data"}}` | `access_mode` is `"ro"` (default) or `"rw"`; `mountpoint` defaults to `/mnt/<name>`; both are optional |

A volume created with both `ReadWriteOnce` and `ReadOnlyMany` is a *logical* volume made of
two persistent disks that sk8s keeps in step:

- the **primary**, an ordinary `ReadWriteOnce` disk. `rw` mounts use it directly, so writing
  works exactly as it does for any other volume (one writer node at a time);
- a **clone** of the primary, a hidden `ReadOnlyMany` claim named `<volume>-ro-<hash>`
  (label `sk8s/kind=volume-clone`). Every read-only mount uses it, from any number of nodes.
  sk8s creates it when the first read-only job is submitted and replaces it after a `rw`
  job has completed.

The clone exists because GKE's persistent-disk driver cannot provision a `ReadOnlyMany`
disk from scratch, nor one claim that is both `ReadWriteOnce` and `ReadOnlyMany`; the
documented way to get a many-reader disk is to clone a populated one. You never name the
clone, and `delete_volume` removes it with the volume.

### Populate, then fan out

Create the volume and populate it with one read-write job. The volume is `keep=True` so it
outlives the script; the job writes eight 1 MiB files and a manifest of their digests:

```python
# rox_populate.py
import sk8s


def build_index():
    # Runs in the cluster, with the volume mounted read-write at /mnt/index.
    import hashlib, json, pathlib, random

    manifest = {}
    for i in range(8):
        data = random.Random(i).randbytes(1024 * 1024)
        pathlib.Path(f"/mnt/index/part-{i}.bin").write_bytes(data)
        manifest[f"part-{i}.bin"] = hashlib.sha256(data).hexdigest()
    pathlib.Path("/mnt/index/manifest.json").write_text(json.dumps(manifest))
    return len(manifest)


sk8s.create_volume("10Gi", name="index", keep=True, access_modes=["ReadWriteOnce", "ReadOnlyMany"])
print(sk8s.run(build_index, volumes={"index": {"access_mode": "rw"}}))  # 8 files written
```

```bash
python rox_populate.py
kubectl -n $NAMESPACE get pvc -L sk8s/kind
```

```text
8
NAME    STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   VOLUMEATTRIBUTESCLASS   AGE   KIND
index   Bound    pvc-92ddf0d2-00d8-4c0e-8300-28471de41b03   10Gi       RWO            standard-rwo   <unset>                 41s   volume-keep
```

(20 to 40 seconds, depending on whether a node is already warm.) There is no clone yet: nothing has read the volume.
Now the readers. This script mounts the volume read-only in four jobs, each of which
checks every file against the manifest and then stays alive for three minutes so that all
four overlap (nodes come up at different times, and a reader only starts once its node has). Each asks for five CPUs, which is more than half of an Autopilot node, so **no
two readers can share a node**: the fan-out has to span nodes, which is the point. (A real
reader would ask for what it needs, and Kubernetes decides placement.)

```python
# rox_fanout.py
import sk8s


def check(i):
    # Runs in the cluster, with the volume mounted READ-ONLY at /mnt/index.
    import hashlib, json, pathlib, socket, time

    manifest = json.loads(pathlib.Path("/mnt/index/manifest.json").read_text())
    good = sum(
        hashlib.sha256(pathlib.Path(f"/mnt/index/{name}").read_bytes()).hexdigest() == digest
        for name, digest in manifest.items()
    )
    time.sleep(180)  # a real job would work from the index here; this keeps all four alive together, even while Autopilot is still adding nodes
    return socket.gethostname(), good, len(manifest)


# volumes="index" is a read-only mount at /mnt/index. Five CPUs per reader means no two fit on one
# node, so this fan-out has to span nodes (an ordinary reader would ask for much less).
for pod, good, total in sk8s.map(check, range(4), volumes="index", requests={"cpu": "5"}):
    print(pod, good, "of", total, "files verified")
```

Run it in the background and look at the cluster while the readers are running:

```bash
python rox_fanout.py > fanout.out &
sleep 20
kubectl -n $NAMESPACE wait pod -l app.kubernetes.io/managed-by=sk8s --for=condition=Ready --timeout=600s
kubectl -n $NAMESPACE get pods -o wide
kubectl -n $NAMESPACE get pvc -L sk8s/kind,sk8s/clone-of -o wide
kubectl get volumeattachments
kubectl -n $NAMESPACE get pod -l app.kubernetes.io/managed-by=sk8s -o jsonpath='{.items[0].spec.containers[0].resources}{"\n"}'
wait
cat fanout.out
```

```text
pod/job-k7g3eu-6q9wt condition met
pod/job-o0nw4f-7t8wn condition met
pod/job-vilyp6-dtps2 condition met
pod/job-ygja8g-knw8d condition met
NAME               READY   STATUS    RESTARTS   AGE     IP              NODE                                           NOMINATED NODE   READINESS GATES
job-k7g3eu-6q9wt   1/1     Running   0          2m48s   10.27.129.195   gk3-autopilot-cluster-1-pool-1-06d9103d-8496   <none>           <none>
job-o0nw4f-7t8wn   1/1     Running   0          2m47s   10.27.129.140   gk3-autopilot-cluster-1-pool-1-06d9103d-4gdz   <none>           <none>
job-vilyp6-dtps2   1/1     Running   0          2m46s   10.27.129.67    gk3-autopilot-cluster-1-pool-1-06d9103d-s25d   <none>           <none>
job-ygja8g-knw8d   1/1     Running   0          2m49s   10.27.129.3     gk3-autopilot-cluster-1-pool-1-06d9103d-456w   <none>           <none>
NAME                STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   VOLUMEATTRIBUTESCLASS   AGE     VOLUMEMODE   KIND           CLONE-OF
index               Bound    pvc-92ddf0d2-00d8-4c0e-8300-28471de41b03   10Gi       RWO            standard-rwo   <unset>                 3m34s   Filesystem   volume-keep    
index-ro-f0d15941   Bound    pvc-39e9f8ef-ab1d-49c4-bfdd-c0bd14fe289a   10Gi       ROX            standard-rwo   <unset>                 2m50s   Filesystem   volume-clone   index
NAME                                                                   ATTACHER                PV                                         NODE                                           ATTACHED   AGE
csi-3cebb529...1fd8   pd.csi.storage.gke.io   pvc-39e9f8ef-ab1d-49c4-bfdd-c0bd14fe289a   gk3-autopilot-cluster-1-pool-1-06d9103d-456w   true       89s
csi-aede580b...9978   pd.csi.storage.gke.io   pvc-39e9f8ef-ab1d-49c4-bfdd-c0bd14fe289a   gk3-autopilot-cluster-1-pool-1-06d9103d-s25d   true       102s
csi-c81cdf81...496a   pd.csi.storage.gke.io   pvc-39e9f8ef-ab1d-49c4-bfdd-c0bd14fe289a   gk3-autopilot-cluster-1-pool-1-06d9103d-4gdz   true       2m37s
csi-ee709362...5753   pd.csi.storage.gke.io   pvc-39e9f8ef-ab1d-49c4-bfdd-c0bd14fe289a   gk3-autopilot-cluster-1-pool-1-06d9103d-8496   true       75s
{"limits":{"ephemeral-storage":"1Gi"},"requests":{"cpu":"5","ephemeral-storage":"1Gi","memory":"5Gi"}}
job-ygja8g-knw8d 8 of 8 files verified
job-k7g3eu-6q9wt 8 of 8 files verified
job-o0nw4f-7t8wn 8 of 8 files verified
job-vilyp6-dtps2 8 of 8 files verified
```

(Trimmed: the attachment names are shortened; the `get pods` and `get pvc` ages are the
moment of the command, and `wait` returned after the last reader was up. Your names, IPs and
times will differ.)

What it shows. Four readers ran **at the same time on four different nodes**; the
`volumeattachments` list has four attachments of the one clone's disk (`pvc-39e9f8ef-...`),
one per node; and every reader read all eight files and got the digests the writer
recorded. In the claim list `index` is the `ReadWriteOnce` primary and `index-ro-f0d15941`
the `ReadOnlyMany` clone that all four mounted. The jsonpath line is what a reader's pod
really got: Autopilot filled in 5 GiB of memory to go with the 5 CPUs (and an ephemeral-storage
request) and you pay for it ([Autopilot notes](#7-autopilot-notes)). The clone was
created in about 5 seconds (the clone claim went `Pending` to `Bound` as the first reader was
scheduled). The readers were all up after about 2 minutes 50 seconds (the `AGE` column), nearly
all of it Autopilot adding four nodes big enough for 5-CPU pods; a fan-out of small readers on
a cluster that needs fewer new nodes is up in about a minute and a half.

### Point-in-time reads

**A read-only mount shows the volume as of the last `rw` job that completed.** That is the
contract, and it follows from the clone: a reader does not see a writer's changes until the
writer has finished, and does not see them live afterwards either; the *next* read-only
job after a writer completes gets a new clone.

```python
# rox_update.py
import sk8s


def publish(version):
    import pathlib

    pathlib.Path("/mnt/index/VERSION").write_text(version)


def read_version():
    import pathlib

    return pathlib.Path("/mnt/index/VERSION").read_text()


def try_to_write():
    import pathlib

    pathlib.Path("/mnt/index/oops").write_text("x")


sk8s.run(publish, "v1", volumes={"index": {"access_mode": "rw"}})
print(sk8s.run(read_version, volumes="index"))  # v1
sk8s.run(publish, "v2", volumes={"index": {"access_mode": "rw"}})
print(sk8s.run(read_version, volumes="index"))  # v2: a new clone, taken once the writer had finished
try:
    sk8s.run(try_to_write, volumes="index")
except OSError as e:
    print("read-only:", e)
```

```bash
python rox_update.py
```

```text
v1
v2
read-only: [Errno 30] Read-only file system: '/mnt/index/oops'
```

The rules, stated plainly:

- Readers see the volume as of the last `rw` job that **succeeded**. A writer that is still
  running is invisible. A **failed** writer never becomes a generation of its own — but any
  bytes it wrote before failing are on the primary, so the *next* clone can contain its
  partial output. A **cancelled** writer (its Job deleted mid-run) is treated as completed
  and does become the generation — sk8s prefers a needless re-clone over a stale read.
- **A clone is never taken while a writer is running** (a copy of a disk that is mounted
  read-write is a dirty copy). A read-only job submitted while a `rw` job is running
  therefore *waits inside `submit()`* for the writer to finish, up to
  `volume_wait_seconds` (300), then raises `sk8s.VolumeWaitTimeout`. So
  `submit(writer)` followed straight by `map(readers)` blocks until the writer is done; call
  `writer.result()` first if you would rather see the writer's own error.
- A read-only job on a volume that no `rw` job has ever completed on raises
  `sk8s.VolumeNotPopulated` (a clone of a blank disk is not a volume anyone can mount).
- A write through a read-only mount fails in your function with `OSError` (`EROFS`), as
  above. It is the kernel refusing, not a hang.
- The clone for the previous generation is deleted once no job uses it. A client that
  closes (or exits) removes the superseded clones of the volumes it read; anything it had to
  leave (a clone an active job still holds) is removed by the next read-only submit or
  reader end on that volume, or at the latest by `delete_volume`.

### What it costs

The clone is a **second persistent disk of the same size as the volume**, billed at the
full provisioned size (not the data in it) from the moment it exists, and a replaced clone
is deleted after its readers finish, so there are two clones only briefly after a writer.
A 500 GiB index is therefore two 500 GiB disks (1,000 GiB of `pd-balanced`, which is on the
order of $100 a month at list prices) while it is in use; a 100 GiB index is about $20. The
alternative for many readers, a `ReadWriteMany` Filestore volume, is $150 to $220 a month
at its 1 TiB minimum whatever you store ([What it costs](#8-what-it-costs)). Readers add
nothing: any number of pods attach one clone. The disk is billed until you
`delete_volume` it (or its workflow ends, for a `keep=False` volume); see
[Teardown](#teardown-of-a-read-many-volume).

Cloning is quick: the 10 GiB clone above was `Bound` about 5 seconds after its first reader
was scheduled. Larger disks take longer and the claim just stays `Pending` while the
provisioner works; sk8s only fails it (`sk8s.VolumeProvisioningFailed`, with the
provisioner's event text) if `ProvisioningFailed` events persist for
`volume_provision_grace_seconds` (300), which is the kind of thing a quota or a
rate-limited source disk looks like. Cloning of terabyte-scale disks was not measured.

### Zone pinning

A clone is created in the zone of its source disk, and a disk can only be attached to nodes
in its own zone. The readers of a volume are therefore all in **one zone**, the zone the
primary disk was provisioned in (the zone of the node that first ran a job on it, since
the default class binds when a pod is first scheduled). Autopilot adds nodes in that zone as
needed, so a fan-out still spans as many nodes as it wants, but not several zones. The
disks from the run above:

```bash
gcloud compute disks list --filter="zone~$REGION" --format='table(name,zone.basename(),sizeGb,sourceDisk.basename())'
```

```text
NAME                                      ZONE           SIZE_GB  SOURCE_DISK
pvc-00e71f53-baf2-4012-8725-40e7d04196ce  us-central1-b  10       pvc-92ddf0d2-00d8-4c0e-8300-28471de41b03
pvc-92ddf0d2-00d8-4c0e-8300-28471de41b03  us-central1-b  10
```

(Primary and clone: same zone, and the clone records its source. Other disks in the
project are omitted here.)

### Writers queue; a wedge is cut off

Only the clone is many-reader. The primary is `ReadWriteOnce`, and two `rw` jobs on
different nodes cannot hold it at once. Two cases, because Kubernetes treats them
the same and sk8s must not:

**A second writer waits its turn.** Kubernetes leaves the second pod in
`ContainerCreating` (event `FailedAttachVolume ... Waiting for detach ... Volume is already
used by pod(s) ...`) until the first pod is done, then attaches it, about 30 seconds
later. sk8s waits too:

```python
# rox_queue.py
import time

import sk8s


def hold(seconds):
    import pathlib, time

    pathlib.Path("/mnt/index/queued").write_text("x")
    time.sleep(seconds)


rw = {"index": {"access_mode": "rw"}}
t0 = time.time()
first = sk8s.submit(hold, 120, volumes=rw, requests={"cpu": "5"})
second = sk8s.submit(hold, 1, volumes=rw, requests={"cpu": "5"})  # lands on another node: it waits
first.result()
print(f"first done after {time.time() - t0:.0f}s")
second.result()
print(f"second done after {time.time() - t0:.0f}s")
```

```bash
python rox_queue.py
```

```text
first done after 143s
second done after 170s
```

**A fan-out of writers, or a reader fleet forgotten on the primary, never finishes.**
Kubernetes has no timeout here: the blocked pods wait for ever. sk8s gives a *live* holder
`volume_attach_grace_seconds` (default 300) to finish, and then fails the blocked job with
`sk8s.VolumeAttachBlocked`, naming the holder and quoting the event, and deletes it. To see
it without waiting five minutes, shorten the grace for one run: a holder takes the volume for
seven minutes, and two more writers, each too big to share its node, try to start:

```python
# rox_wedge.py
import os
import subprocess

import sk8s


def hold(seconds):
    import pathlib, time

    pathlib.Path("/mnt/index/holder").write_text("x")
    time.sleep(seconds)


rw = {"index": {"access_mode": "rw"}}
holder = sk8s.submit(hold, 420, volumes=rw, requests={"cpu": "5"})
# Wait until the holder's pod is running, so it is the one that has the disk.
subprocess.run(
    ["kubectl", "-n", os.environ["NAMESPACE"], "wait", "pod", "-l", f"job-name={holder.job_name}",
     "--for=condition=Ready", "--timeout=600s"],
    check=True, capture_output=True,
)
# Two more writers, each too big to share the holder's node: they cannot attach the disk.
late = [sk8s.submit(hold, 1, volumes=rw, requests={"cpu": "5"}) for _ in range(2)]
for f in late:
    try:
        f.result()
        print(f.job_name, "ran")
    except sk8s.VolumeAttachBlocked as e:
        print(e)
holder.result()
print(holder.job_name, "ran to completion")
```

```bash
SK8S_VOLUME_ATTACH_GRACE_SECONDS=60 python rox_wedge.py
```

```text
job job-vy8is8 failed: cannot start: its volume 'index' (PVC 'index') is still held by job-f8v08b-brhpq on another node, and a ReadWriteOnce disk attaches to one node at a time. Kubernetes reports FailedAttachVolume: Waiting for detach for volume "pvc-92ddf0d2-00d8-4c0e-8300-28471de41b03" Volume is already used by pod(s) job-f8v08b-brhpq
This wait has no timeout of its own. Either wait for job-f8v08b-brhpq to finish and resubmit, or run these jobs one at a time. To fan out many concurrent readers, create the volume with access_modes=["ReadWriteOnce", "ReadOnlyMany"] and mount it read-only (the default): readers are then served from a read-only clone that any number of nodes can attach.
job job-i5j1h4 failed: cannot start: its volume 'index' (PVC 'index') is still held by job-f8v08b-brhpq on another node, and a ReadWriteOnce disk attaches to one node at a time. Kubernetes reports FailedAttachVolume: Waiting for detach for volume "pvc-92ddf0d2-00d8-4c0e-8300-28471de41b03" Volume is already used by pod(s) job-f8v08b-brhpq
This wait has no timeout of its own. Either wait for job-f8v08b-brhpq to finish and resubmit, or run these jobs one at a time. To fan out many concurrent readers, create the volume with access_modes=["ReadWriteOnce", "ReadOnlyMany"] and mount it read-only (the default): readers are then served from a read-only clone that any number of nodes can attach.
job-f8v08b ran to completion
```

The holder ran to completion; the two blocked writers were failed and deleted (no `Pending`
pod is left behind). The error fires about the grace plus up to a minute of probe delay
after the pod was blocked. The three knobs, settable in the config file, as `SK8S_*`
environment variables or as keyword arguments, are:

| Setting | Default | What it bounds |
|---|---|---|
| `volume_wait_seconds` | 300 | a read-only submit waiting for a running `rw` job to finish (`VolumeWaitTimeout`) |
| `volume_attach_grace_seconds` | 300 | a pod blocked behind a live holder of the `ReadWriteOnce` disk before `VolumeAttachBlocked`; make it longer than the longest writer you are willing to queue behind |
| `volume_provision_grace_seconds` | 300 | the clone staying `Pending` with `ProvisioningFailed` events before `VolumeProvisioningFailed` |

### The silent-empty-clone hazard on other clusters

> **A StorageClass whose provisioner does not implement volume cloning can hand you an
> empty volume with no error.** The read-many mechanism creates a claim with a
> `dataSource`, and a provisioner that ignores `dataSource` simply provisions a blank disk
> and says nothing. minikube's default `standard` class (`k8s.io/minikube-hostpath`) does
> exactly that: the clone claim binds, the reader mounts it read-only, and every read finds
> **no data**. GKE's persistent-disk driver (`pd.csi.storage.gke.io`, the `standard-rwo`
> and `premium-rwo` classes) implements cloning and is what this guide ran on. sk8s cannot
> detect the difference from inside a namespace. Before trusting read-many volumes on any
> other cluster, run the populate-then-read walkthrough above and check that the readers see
> your data; the repository's lifecycle test
> (`test_clone_lifecycle_point_in_time_and_supersede`) skips itself on provisioners it
> does not know to clone.

### Teardown of a read-many volume

`delete_volume` removes the volume and its clone(s). A claim waits for pods that still mount
it before it goes (a clone whose readers have finished disappears when their pods do), and
the disks go a little after the claims. Check the claims and the project's disks; the
disks are named `pvc-<uid>` (not `sk8s-*`), so compare against the list from before:

```bash
python -c 'import sk8s; sk8s.delete_volume("index")'
kubectl -n $NAMESPACE get pvc
for i in $(seq 30); do [ -z "$(gcloud compute disks list --filter="zone~$REGION" --format='value(name)')" ] && break; sleep 5; done  # the disks go a little after the claims
gcloud compute disks list --filter="zone~$REGION" --format='table(name,zone.basename(),sizeGb,sourceDisk.basename())'
```

```text
No resources found in sk8s namespace.
```

(The two remaining disks are not sk8s's.) A volume that was never deleted keeps both disks
billing: `kubectl get pvc -A -l sk8s/kind` lists every sk8s volume and clone.

## 7. Autopilot notes

Autopilot manages the nodes, so a few things differ from minikube.

**Cold starts.** Autopilot scales nodes with demand and can scale them to nothing. A job
submitted to a quiet cluster waits for a node to be provisioned and the ~95 MB image to
be pulled onto it: plan for **one to two minutes** on the first job after idle, and
roughly 10 to 20 seconds for jobs after that. sk8s waits for as long as you let it: with
no `timeout`, `run(...)` and `future.result()` wait indefinitely, so if you do pass one,
give a cold start room (a timeout of 30 seconds will fire on a cold cluster; a few
minutes will not). A `Pending`
pod is not an error. To see why it waits, `kubectl -n sk8s describe pod <name>` shows the
scale-up events.

**Resource requests are rewritten.** Autopilot sets and adjusts every container's
resource requests (it rounds to its own allowed values and adds ephemeral storage). What
you asked for in `requests=` is a starting point, not necessarily what you get. Do not
assert exact values from a job's spec. To see what a pod really got, ask the pod while it
runs:

```python
# resources.py
import time

import sk8s

fut = sk8s.submit(time.sleep, 25, name_prefix="resources", requests={"cpu": "100m", "memory": "64Mi"})
fut.result(timeout=300)
```

```bash
python resources.py &
sleep 10
kubectl -n $NAMESPACE wait pod -l app.kubernetes.io/managed-by=sk8s --for=condition=Ready --timeout=300s
kubectl -n $NAMESPACE get pod -l app.kubernetes.io/managed-by=sk8s -o jsonpath='{.items[0].spec.containers[0].resources}{"\n"}'
wait
```

```text
pod/resources-8yp5b8-sq2lx condition met
{"limits":{"ephemeral-storage":"1Gi"},"requests":{"cpu":"100m","ephemeral-storage":"1Gi","memory":"103Mi"}}
```

(The script asked for 64Mi of memory; the pod got 103Mi, plus an ephemeral-storage request
and limit that nobody mentioned.) You pay for what Autopilot allocates, not what you requested.
Memory `limits` still work: a job that exceeds its limit is `OOMKilled` and sk8s reports
it as such.

**No privileged pods.** Autopilot refuses privileged containers. sk8s turns the API
server's refusal into a typed error, `sk8s.JobRejected`, with the server's own message
and a hint, instead of a raw Kubernetes exception:

```python
# privileged.py
import sk8s

try:
    sk8s.run(lambda: "hi", privileged=True)
except sk8s.JobRejected as e:
    print(e)
```

```bash
python privileged.py
```

```text
the cluster rejected job job-ke8s35 (HTTP 400): admission webhook "warden-validating.common-webhooks.networking.gke.io" denied the request: GKE Warden rejected the request because it violates one or more constraints.
Violations details: {"[denied by autogke-disallow-privilege]":["container worker is privileged; not allowed in Autopilot"]}
Requested by user: 'you@example.com', groups: 'system:authenticated'.
privileged=True was requested; managed clusters such as GKE Autopilot refuse privileged containers. Drop privileged=True or use a cluster that allows it.
```

It fails fast (a few seconds), and nothing is left behind: the pickled payload is deleted
before the error reaches you. `JobRejected` also covers an RBAC `403` or an invalid spec;
`e.status` and `e.message` carry the HTTP status and the server's text. See the
[migration guide](migration-v1-to-v2.md) for the full list of sk8s error types.

**Workload Identity is per namespace and per ServiceAccount.** Jobs run as the
`sk8s` ServiceAccount in the namespace sk8s is configured for, and so does a workflow
driver that submits jobs itself, so nested jobs reach the bucket with no extra setup.

**Volumes and Autopilot.** The [read-many walkthrough](#6-read-many-volumes) turned up three
Autopilot details. Requests are rewritten for memory too: asking for `cpu: 5` and no memory
gave each reader 5 GiB, which is what you are billed for. A disk is created when the first pod
that uses it is scheduled (the default classes wait for a consumer), so a volume's zone is
the zone of the node that first ran a job on it, and a clone's readers are pinned to that zone.
And Autopilot refuses a pod that uses pod anti-affinity unless it requests at least 500m of
CPU (`autogke-pod-limit-constraints`); sk8s has no anti-affinity option, so this only matters if
you add one yourself, as the live tests for this feature did to force readers onto different nodes.

## 8. What it costs

Rough, us-central1, list prices at the time of writing; check the
[GKE](https://cloud.google.com/kubernetes-engine/pricing),
[Filestore](https://cloud.google.com/filestore/pricing) and
[Storage](https://cloud.google.com/storage/pricing) pricing pages for current numbers.

| Item | Cost shape | In practice |
|---|---|---|
| Autopilot cluster management fee | about $0.10 per cluster-hour | one cluster per billing account is covered by the monthly free-tier credit |
| Autopilot pods | per vCPU/memory/storage-second the pod is allocated | a measured data point: the integration stress test (**85 pods** over about 3.4 minutes) came to well under **$0.10** (an estimate from pod counts and a ~0.5 vCPU / 2 GiB default, not billing data) |
| Artifact Registry | storage per GiB-month beyond a small free tier | the worker image is about 95 MB: pennies or free |
| Cloud Storage bucket | storage per GiB-month plus operations | blobs are deleted as workflows finish; an idle empty bucket costs nothing |
| `ReadWriteOnce` volumes (`create_volume`) | a persistent disk (PD) per GiB-month while the claim exists | pennies; the disk goes with the claim, and a `keep=True` volume lives until you `delete_volume()` it |
| Read-many volumes (`access_modes=["ReadWriteOnce", "ReadOnlyMany"]`) | **two** persistent disks of the volume's size: the volume and its read-only clone, each billed at full size | about twice the PD price of the same size; no Filestore. See [Read-many volumes](#6-read-many-volumes) |
| Idle | no pods | sk8s runs nothing between jobs, so apart from the management fee above and the pennies of stored data, an idle setup costs nothing |
| **Filestore (`ReadWriteMany`)** | **about $0.20 to $0.30 per hour, from the moment the instance exists** | **1 TiB minimum (Basic HDD tier). One test run, about 6.5 minutes of existence, cost about $0.025. Left running, it is about $150 to $220 a month.** |

### Filestore: create late, delete fast

A `ReadWriteMany` volume is provisioned by GKE's Filestore CSI driver as a **real
Filestore instance**, and the smallest one (the Basic HDD tier behind the
`standard-rwx` StorageClass) is 1 TiB, whatever size you request. It starts billing
as soon as it exists (it appeared 4 seconds after the claim was created in the test),
and stops when it is deleted, which happens only after the claim is deleted and takes a
few minutes more (about 3.5 minutes in the test). sk8s never asks for one on its own; you
get one only if you create a volume with `access_modes=("ReadWriteMany",)` and a Filestore
storage class.
The repository's opt-in integration test does exactly that, once:

```text
SK8S_GKE_RWX=1 pytest tests/integration/test_volumes.py -k rwx   # creates a Filestore instance
```

Rules of thumb: create it only when you are about to use it; use `keep=False` (the
default) so it goes away with its workflow, or `delete_volume()` it explicitly the moment
you are done; and **verify** it is gone rather than assuming:

```bash
gcloud filestore instances list
kubectl get pvc -A
```

```text
Listed 0 items.
No resources found
```

Instances created by the CSI driver are named `pvc-<uuid>` (not `sk8s-*`), so a `grep sk8s`
will not find them; the empty list above is the check that matters.

## 9. Known behavior: deleting an anchor by hand

Every workflow is owned by one anchor ConfigMap (`sk8s-anchor-<id>`); deleting the anchor
deletes the workflow's Jobs and, in the default `k8s` store, its blobs, by Kubernetes
garbage collection. **In a `gs://` store, the blobs are objects in your bucket, and
Kubernetes cannot delete bucket objects.** sk8s records the store and prefix on the
anchor and deletes them itself, but only on its own code paths: `sk8s kill`,
`WorkflowHandle.result()` / `kill()`, `sk8s clean`, `sk8s.anchors.delete_anchor` and
client teardown. If you remove the anchor with `kubectl delete configmap`, the Jobs go and the
bucket objects stay.

Seeing it. Submit a detached workflow and let its driver finish without fetching the
result, so its blobs are still in the bucket:

```python
# detached.py
import sk8s


def pipeline(n):
    return sum(sk8s.map(lambda x: x * x, range(n)))


wf = sk8s.submit_workflow(pipeline, 10)
print(wf.id)
```

```bash
WF=$(python detached.py)
for i in $(seq 60); do sk8s workflows | grep "$WF" | grep -q succeeded && break; sleep 5; done
sk8s workflows
gcloud storage ls -r gs://$BUCKET/sk8s/$WF/ 2>/dev/null
```

```text
ID         DETACHED  AGE  DRIVER
wf-cwfne4  yes       44s  succeeded
gs://my-project-sk8s/sk8s/wf-cwfne4/:

gs://my-project-sk8s/sk8s/wf-cwfne4/wfdriver-tqogla/:
gs://my-project-sk8s/sk8s/wf-cwfne4/wfdriver-tqogla/payload
gs://my-project-sk8s/sk8s/wf-cwfne4/wfdriver-tqogla/result
```

Now delete the anchor the way a hurried `kubectl` user would. The workflow disappears
from sk8s's view, `sk8s clean` finds nothing to do (the anchor it would read the store
prefix from is gone), and the objects are still there:

```bash
kubectl -n $NAMESPACE delete configmap sk8s-anchor-$WF
sk8s workflows
sk8s clean
gcloud storage ls -r gs://$BUCKET/sk8s/$WF/ 2>/dev/null
```

```text
configmap "sk8s-anchor-wf-cwfne4" deleted
no workflows in namespace sk8s
nothing to clean
gs://my-project-sk8s/sk8s/wf-cwfne4/:

gs://my-project-sk8s/sk8s/wf-cwfne4/wfdriver-tqogla/:
gs://my-project-sk8s/sk8s/wf-cwfne4/wfdriver-tqogla/payload
gs://my-project-sk8s/sk8s/wf-cwfne4/wfdriver-tqogla/result
```

Remove the orphan yourself (this is the "backstop" case in
[conventions](conventions.md#object-store-lifecycle-rules-and-svc)):

```bash
gcloud storage rm -r gs://$BUCKET/sk8s/$WF
```

The supported way to delete a workflow removes everything. `sk8s kill` takes the anchor,
the Jobs and the bucket objects together:

```bash
WF=$(python detached.py)
for i in $(seq 60); do sk8s workflows | grep "$WF" | grep -q succeeded && break; sleep 5; done
sk8s kill $WF
gcloud storage ls -r gs://$BUCKET/sk8s/$WF/ 2>&1 | tail -1
```

```text
killed wf-o9geyc
ERROR: (gcloud.storage.ls) One or more URLs matched no objects.
```

sk8s configures **no lifecycle (expiration) rules** on your bucket. If you want a
backstop for blobs leaked by crashed clients or by hand-deleted anchors, add one
yourself, scoped to `sk8s/wf-` and **never** to `svc/` (a service's payload is read again
every time its pod restarts). The exact rules and why are in
[conventions.md](conventions.md#object-store-lifecycle-rules-and-svc).

## 10. Teardown

Two kinds of things exist after this guide.

**Transient**, created by your jobs and already cleaned up by sk8s when workflows finish:
Jobs and pods, workflow anchors, blobs in the bucket, `keep=False` volumes (and their read-only
clones). `keep=True` volumes are not transient: delete them with `sk8s.delete_volume`. To confirm,
or to sweep up after a crashed session:

```bash
sk8s clean --older-than 0
sk8s workflows
kubectl -n $NAMESPACE get all,configmaps,pvc
kubectl get pv
gcloud storage ls -r gs://$BUCKET/ 2>/dev/null
gcloud filestore instances list
```

Note that `sk8s clean` keeps a safety floor: anchors younger than five minutes are left
alone, and so is any workflow that still has active jobs — so right after a crash it can
print `nothing to clean` while the anchors are still there. Wait out the floor and re-run,
or remove a specific workflow immediately with `sk8s kill <id>`.

Anything left in the namespace besides `kube-root-ca.crt` and the default ServiceAccount
is yours to look at; a workflow still running is deleted by `sk8s kill <id>`.

**Durable**, which cost almost nothing to keep, so most people never remove them. If you
are done with all of it, this removes everything this guide created. It does **not**
touch the GKE cluster. Run it from the shell where the variables are exported, the
namespace first (so nothing is using the identity), and note it is destructive:

```bash
kubectl delete namespace $NAMESPACE
gcloud iam service-accounts delete $GSA@$PROJECT.iam.gserviceaccount.com --project $PROJECT --quiet
gcloud storage rm -r gs://$BUCKET
gcloud artifacts repositories delete $AR_REPO --location $REGION --project $PROJECT --quiet
rm -f "$PWD/.kubeconfig-gke" "$PWD/.sk8s-gke.toml"  # the guide's two local files — skip if you used your global files
```

Deleting the GSA also drops the Workload Identity bindings on it. To retire just one
namespace and keep the shared GSA, bucket and repository, remove that namespace's member
instead, then delete the namespace:

```bash skip
gcloud iam service-accounts remove-iam-policy-binding $GSA@$PROJECT.iam.gserviceaccount.com --role roles/iam.workloadIdentityUser --member "serviceAccount:$PROJECT.svc.id.goog[team-b/sk8s]" --project $PROJECT
kubectl delete namespace team-b
```

Delete the cluster itself only if it was created for this (`gcloud container clusters
delete $CLUSTER --region $REGION`): Autopilot clusters are cheap when idle, but they are
not free.
