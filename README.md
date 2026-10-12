# sk8s: run Python functions on Kubernetes

sk8s (pronounced "skates") turns a Python function call into a Kubernetes Job and
hands you the result as a plain Python value, or a `concurrent.futures.Future`.

```python
import sk8s

sk8s.run(lambda x: x * 2, 21)                     # 42, computed in a pod
sk8s.map(lambda x: x ** 2, range(1000), batch_size=50)  # 20 Jobs, results in order
```

- **No resident control plane.** The client talks to the Kubernetes API; there is no
  operator, scheduler or database to install. Jobs can submit more Jobs from inside the
  cluster, so whole pipelines are just Python.
- **Futures, not job names.** `submit` returns a `Future`; `map` collects results in
  order; a function that raises re-raises the *same exception type* locally with the
  remote traceback attached.
- **Any picklable value** goes in and comes out (cloudpickle), not just JSON. Results are
  pickled, so the client needs the modules of the types a job returns: return builtins
  (`float`, `int`, `list`, `dict`) for portable results, and note that numpy arrays or
  `numpy.float64` scalars need numpy installed client-side too. A result that cannot be
  unpickled raises `ResultDecodeError` (the job itself succeeded).
- **Detached workflows.** Submit a driver function, close your laptop, and `attach` to it
  later from any process.
- **Services and volumes** are first-class: `serve()` a function as a Deployment behind a
  ClusterIP Service, `create_volume()` a PersistentVolumeClaim and mount it into Jobs.
- **Nothing leaks.** Every object a workflow creates is owned by one "anchor" ConfigMap,
  so deleting a workflow (or letting retention expire it) removes all of it.

> **Status: 2.0.0a0 (pre-release).** Not on PyPI yet and no worker images are published
> yet, so the quickstart below builds the worker image from this checkout. The v1
> documentation lives on the `main` branch and the `v0.1` tag; upgrading is covered in the
> [migration guide](docs/migration-v1-to-v2.md). The quickstart targets minikube; for
> Google Cloud see the [GKE (Autopilot) guide](docs/gke.md). An EKS guide is forthcoming.
>
> **Working with an AI agent?** This repo ships an agent skill at
> [`.claude/skills/sk8s/`](.claude/skills/sk8s/SKILL.md) — setup, API, and advanced
> workflow patterns in agent-ready form. Copy or symlink it into your own project's
> skills directory to make any agent sk8s-fluent.

## Quickstart (about 5 minutes)

You need Python 3.10+, Docker, [minikube](https://minikube.sigs.k8s.io/) and `kubectl`.

This quickstart uses minikube's registry addon, so the workflow is the same one you would
use on a real cluster: `docker build`, `docker push`, and jobs pull the image by name with
the default pull policy. (No registry? See [Building directly into
minikube](#no-registry-building-directly-into-minikube).)

**1. Start a cluster.**

```bash
minikube start
```

**2. Enable the registry addon.** This gives the cluster an image registry that every pod
can pull from as `localhost:5000`, which is what lets you push images from your machine and
have jobs use them:

```bash
minikube addons enable registry
```

**3. Install sk8s** from a checkout (`pip install -e .` installs the `sk8s` command too):

```bash
git clone -b version-2 https://github.com/jared-maguire/jobs.git sk8s && cd sk8s
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

**4. Prepare a namespace.** `sk8s config` creates the `sk8s` namespace with a
ServiceAccount, Role and RoleBinding, and writes `~/.config/sk8s/config.toml`:

```bash
sk8s config --namespace sk8s
sk8s doctor        # five PASS lines when the cluster is ready
```

**5. Reach the registry from your machine.** Forward its port and leave this running
while you push (add `&` to background it, or use a spare terminal):

```bash
kubectl -n kube-system port-forward svc/registry 5000:80 &
```

(A persistent alternative to the port-forward is a `socat` relay from `localhost:5000` to
`$(minikube ip):5000`, run under a supervisor or in a container.)

**6. Build the worker image and push it.** The worker's Python minor version must match
your local interpreter (the payload carries a version check); `docker/Dockerfile.dev`
defaults to Python 3.14. For another version, add a build arg, e.g.
`--build-arg PYTHON_VERSION=3.12`.

```bash
docker build -t localhost:5000/sk8s-base:dev -f docker/Dockerfile.dev .
docker push localhost:5000/sk8s-base:dev
```

**7. Point sk8s at that image.** Either set it for this shell:

```bash
export SK8S_IMAGE=localhost:5000/sk8s-base:dev
```

or put it in `~/.config/sk8s/config.toml` (recommended, since it persists across shells;
`sk8s config` keeps keys it did not write, so this is safe to add at any point):

```toml
image = "localhost:5000/sk8s-base:dev"
```

Nodes cache images by tag and the default pull policy (`IfNotPresent`) will not refetch a
tag a node already has, so when you rebuild the base, push it under a new tag and update
`image`. There is no published default image: until `image` (or `SK8S_IMAGE`, or an
`image_prefix`) is set, `run` / `submit` / `map` raise `sk8s.ImageNotConfigured` straight
away, before creating anything, with the build commands in the message.

**8. Run a function.**

```python
import sk8s

print(sk8s.run(lambda x: x * 2, 21))  # one function, one Kubernetes Job -> 42

fut = sk8s.submit(lambda x: x + 1, 41)  # same, but returns a Future immediately
print(fut.result(timeout=120))          # 42
```

**9. Map over many inputs.** `batch_size` packs several items into each Job, and an item
that raises fails only itself:

```python
import sk8s

print(sk8s.map(lambda x: x ** 2, range(5)))  # [0, 1, 4, 9, 16]

# 100 items, 25 per Job -> 4 Jobs instead of 100
print(sum(sk8s.map(lambda x: x ** 2, range(100), batch_size=25)))  # 328350

try:
    sk8s.run(lambda: 1 / 0)
except ZeroDivisionError as e:  # the remote exception, re-raised as its real type
    print("caught remotely raised:", type(e).__name__)
```

When some items of a `map` fail, every other item is still collected and
`sk8s.MapFailed` lists the failing indices (`e.failures`) and carries every success
(`e.results`, `{index: value}`). Use `submit_map` / `submit_starmap` to get the futures
themselves.

To verify *answers*, not just exit codes, give the job a QC gate and pilot the fan-out. The
check runs in the worker; a rejected result is a typed `sk8s.ExpectationFailed`, and
`pilot=n` runs only the first n items before committing to the rest:

```python
def mapped_over_90(frac):
    return None if frac >= 0.9 else f"only {frac:.0%} mapped"   # None = pass; str = fail

sk8s.map(percent_mapped, samples, pilot=3, expect=mapped_over_90)  # PilotFailed: nothing else submitted
```

**10. Start a detached workflow.** The driver function runs in the cluster as a Job and can
submit its own Jobs. Your process does not need to stay alive:

> **Rule of thumb:** a plain `submit()`/`run()` is tethered to your Python session — if the process dies, the job still completes but its result is stranded. For anything you might walk away from, use `submit_workflow()` and reattach later with `attach(id)`.

```python
import sk8s


def pipeline(n):
    # Runs inside the cluster.
    print("squaring", n, "numbers", flush=True)  # pod stdout is block-buffered
    return sum(sk8s.map(lambda x: x * x, range(n)))


wf = sk8s.submit_workflow(pipeline, 10)  # returns immediately
print("submitted", wf.id)                # e.g. wf-k3x9ab

# Later, from this or any other process (or another machine):
same = sk8s.attach(wf.id)
print(same.status())                     # {'driver': 'active', 'jobs': {...}}
print(same.result(timeout=300))          # 285, then the finished workflow is deleted
```

`sk8s workflows` lists workflows, `sk8s logs <id>` prints a driver's output and
`sk8s kill <id>` deletes one with everything it owns.

**11. Serve a function.** `serve()` runs a function that blocks forever (here an HTTP
server) as a Deployment plus a ClusterIP Service. The URL works inside the cluster, so we
call it from a Job (from your machine, `kubectl port-forward -n sk8s service/hello 8080`):

```python
import http.server
import time
import urllib.request

import sk8s


def hello(port):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello from sk8s")

    http.server.HTTPServer(("", port), Handler).serve_forever()


svc = sk8s.serve(hello, 8080, name="hello", port=8080)
print(svc.url)  # http://hello.sk8s.svc.cluster.local:8080


def call(url):
    for _ in range(60):  # serve() returns before the pod is ready
        try:
            return urllib.request.urlopen(url, timeout=2).read().decode()
        except OSError:
            time.sleep(1)
    raise TimeoutError(url)


print(sk8s.run(call, svc.url))  # hello from sk8s
svc.delete()                    # services live until you delete them
```

**12. Use a volume.** A volume is a PersistentVolumeClaim; a job mounts it with `volumes=`.
**Mounts are read-only unless you ask for read-write**, so a job that writes says so:

| `volumes=` | Meaning |
|---|---|
| `"scratch"` | one read-only mount at `/mnt/scratch` |
| `["a", "b"]` | several read-only mounts, at `/mnt/a` and `/mnt/b` |
| `{"scratch": {"access_mode": "rw", "mountpoint": "/data"}}` | per volume: `access_mode` (`"ro"`, the default, or `"rw"`) and `mountpoint` (default `/mnt/<name>`), both optional |

`keep=True` makes a volume independent of any workflow (otherwise it is deleted with the
workflow that created it):

```python
import sk8s


def write():
    with open("/mnt/scratch/note.txt", "w") as f:
        f.write("written by one job")


def read():
    with open("/mnt/scratch/note.txt") as f:
        return f.read()


sk8s.create_volume("100Mi", name="scratch", keep=True)
sk8s.run(write, volumes={"scratch": {"access_mode": "rw"}})  # writing is explicit
print(sk8s.run(read, volumes="scratch"))  # written by one job (read-only is the default)
sk8s.delete_volume("scratch")
```

A write through a read-only mount fails inside the job with `OSError` (`EROFS`). A plain
volume like this one is `ReadWriteOnce`: one node at a time, so jobs that mount it
read-write run one after another. To fan out many concurrent readers on many nodes (a
genome index, say), create it with `access_modes=["ReadWriteOnce", "ReadOnlyMany"]`; its
read-only mounts are then served from a hidden clone, cut after the last successful writer.
That needs a storage provisioner that really clones volumes (GKE's persistent-disk driver
does; minikube's default `standard` class silently serves an **empty** clone), so it is
described, with a walkthrough, in the [GKE guide](docs/gke.md#6-read-many-volumes).

That is the whole tour. To try the API with no cluster at all, swap in local
subprocess workers (same pipeline, same serialization):

```python
import sk8s

sk8s.configure(cluster=sk8s.LocalCluster())
print(sk8s.run(lambda x: x * 2, 21))  # 42
```

### No registry? Building directly into minikube

If you cannot or would rather not run the registry addon, build the worker image straight
into minikube's image store. The Python minor-version rule from step 6 still applies; pass
`--build-opt=opt=build-arg:PYTHON_VERSION=3.12` for another version (see
[docs/dev-setup.md](docs/dev-setup.md) for the syntax per container runtime).

```bash
minikube image build -t sk8s:dev -f docker/Dockerfile.dev .
minikube image ls | grep sk8s:dev          # minikube can exit 0 even when the build failed
export SK8S_IMAGE=sk8s:dev SK8S_PULL_POLICY=Never
```

**Why `Never`?** A bare tag like `sk8s:dev` names no registry, so Kubernetes resolves it to
`docker.io/library/sk8s:dev`. With the default `IfNotPresent` policy, a local build that
failed silently (see the `grep` above) would not be an error: the node would fall back to
pulling that name from Docker Hub and your function would run inside whatever image happens
to live there, a stranger's image under a name you assumed was yours. `Never` makes the
kubelet use only what is already on the node, so a missing build fails loudly
(`ErrImageNeverPull`) instead. A registry-qualified name such as `localhost:5000/...` does
not have this problem, because it can only resolve to a registry you control, which is why
the main path above needs no override.

`Never` is session state (an environment variable, or `pull_policy` in your config), and it
breaks registry images, which have to be pulled. When you switch to the registry flow,
`unset SK8S_IMAGE SK8S_PULL_POLICY` (and remove any `pull_policy` or `image` line you added
to `config.toml`) before setting `SK8S_IMAGE` as in step 7.

## Custom images

The official worker image has only sk8s and the standard library. To add packages, layer
them on a base image.

### Through the registry

This continues the quickstart: the registry addon is enabled, the step 5 port-forward is
running, and `SK8S_IMAGE` (or `image` in your config) points at your pushed base. A custom
image is a Dockerfile layered on that base, built, pushed and pulled with the same
`localhost:5000/` prefix and the default pull policy:

```bash
# a Dockerfile layered on your base, e.g. FROM localhost:5000/sk8s-base:dev + RUN pip install pysam
docker build -t localhost:5000/pysam-worker:1 .
docker push localhost:5000/pysam-worker:1
```

```python
import sk8s

print(sk8s.run(lambda: __import__("pysam").__version__, image="localhost:5000/pysam-worker:1"))
```

Pods resolve `localhost:5000` through the addon's node-local proxy, so no pull-policy
override is needed. Setting `image_prefix = "localhost:5000/"` in
`~/.config/sk8s/config.toml` prepends that registry to the official worker image name
(used only when `image` is not set; with neither, sk8s raises `ImageNotConfigured`), the same way you would point a real cluster at an ECR
or GAR mirror prefix.

### From Python

The same machinery is a small API, so a script can build its own worker image:

```python
from sk8s import images

df = images.generate_dockerfile(base="localhost:5000/sk8s-base:dev", pip=["pysam"])
print(df)
# FROM localhost:5000/sk8s-base:dev
# RUN pip install --no-cache-dir pysam
```

`images.build` runs `docker build` (feeding it the Dockerfile on stdin) and returns the
tag. With `push=True` it is the one-call version of the registry workflow above:

```python
tag = images.build("localhost:5000/pysam-worker:1", base="localhost:5000/sk8s-base:dev", pip=["pysam"], push=True)

# or bring your own Dockerfile text
tag = images.build("localhost:5000/pysam-worker:1", dockerfile=df)
images.push(tag)   # push separately, if you built without push=True
```

`docker build` runs on your host Docker daemon, which pulls the base through the same
`localhost:5000` port-forward you use for pushing, so keep it running. (A base built with
`minikube image build` is invisible to that daemon.) `images.default_base()` returns the
base used when you omit `base=`: the configured worker image, so an exported `SK8S_IMAGE`
(or `image` in your config) is honored, and after the quickstart that is your registry
base. Every function that shells out takes
an optional `runner=` callable (default `subprocess.run`) so tests can inject a fake.
`conda=[...]` is also accepted, but that path installs sk8s from PyPI and will not work
until the first PyPI release.

Then use the image, for one run or for every run:

```python
import sk8s

print(sk8s.run(lambda: __import__("pysam").__version__, image=tag))
sk8s.configure(image=tag)
```

### On a real cluster: `sk8s image build`

`sk8s image build --tag TAG [--base IMG] --pip PKG [--push]` generates the Dockerfile and
runs `docker build` (and `docker push` with `--push`), so use a tag your cluster can pull.
When `--base` is omitted the base is the configured worker image, so an exported
`SK8S_IMAGE` also serves as the default base. `--base` only applies to pip dependencies.
`--conda` builds from miniforge and installs sk8s from PyPI at your exact version, so it
needs the first PyPI release.

## Cleanup and lifecycle

A normal `sk8s.run` / `map` session deletes its own bookkeeping when the process exits.
If a process dies, or exits with unfinished jobs, its workflow ages out instead: finished
Jobs are reaped by Kubernetes after an hour (`ttl_seconds_after_finished`), and
`sk8s clean` (also run opportunistically by every worker at start-up) deletes workflows
older than `retention_days` (default 7) that have no active Jobs. Services and kept volumes
are never touched by either. Details in [docs/conventions.md](docs/conventions.md).

## Result stores

Payloads and results travel through a blob store, chosen by the `store` setting
(`sk8s config --store ...`, `SK8S_STORE`, or `sk8s.configure(store=...)`):

| `store` | Backend | Notes |
|---|---|---|
| `k8s` (default) | One ConfigMap per blob in your namespace | Zero setup. Capped at about 950 KB per blob (`BlobTooLarge`); use an object store for bigger payloads or results. |
| `s3://bucket[/prefix]` | S3 or any S3-compatible service | `pip install "sk8s[s3]"`; credentials from the boto3 default chain. Set `store_endpoint` (or `SK8S_S3_ENDPOINT`) for MinIO and friends. |
| `gs://bucket[/prefix]` | Google Cloud Storage | `pip install "sk8s[gcs]"`; credentials from Application Default Credentials. |
| `local:<dir>` | A directory | For `LocalCluster` and tests; pods cannot see it. |

The worker image needs the matching library (the project's worker Dockerfile installs
both extras). To run the S3 store against MinIO in minikube, see the MinIO section of
[docs/dev-setup.md](docs/dev-setup.md). Results are unpickled by the client, so only point
sk8s at stores you trust.

## Configuration

Settings layer as: defaults, then `~/.config/sk8s/config.toml` (set `SK8S_CONFIG=<path>` to
read and write a different file; this changes which file, not the layering), then `SK8S_*` environment
variables (`SK8S_NAMESPACE`, `SK8S_IMAGE`, `SK8S_STORE`, ...), then keyword arguments
(`sk8s.configure(namespace=...)`, `Cluster(config, **overrides)`). The fields are listed in
`sk8s.Config`. The namespace is the explicit setting if there is one, else the active kube
context's namespace if it names one, else `sk8s`.

## The `sk8s` command

| Command | What it does |
|---|---|
| `sk8s config [--namespace NS] [--store URL] [--provider local\|generic\|gke] [--rwx-storage-class C [--rwx-provisioner P]] [--dry-run]` | Create the namespace, ServiceAccount, Role and RoleBinding; write the client config (including the StorageClass used for `ReadWriteMany` volumes: derived on `gke`, named by `--rwx-storage-class` on `generic`). `--dry-run` prints the plan only. |
| `sk8s doctor [--json]` | Check cluster reachability, namespace, ServiceAccount, permissions and store; non-zero exit if any check fails. |
| `sk8s workflows [--json]` | List workflows in the namespace (id, detached, age, driver phase). |
| `sk8s explain WORKFLOW [--json]` | Everything about one workflow in one report: driver, jobs with stuck pods explained, volumes, store blobs, rough cost, and what is blocking progress. Exit 0 even when unhealthy; 1 if the workflow does not exist. Same data as `sk8s.explain(id)`. |
| `sk8s logs WORKFLOW` | Print a workflow driver's logs. |
| `sk8s kill WORKFLOW` | Delete a workflow and everything it owns. |
| `sk8s clean [--older-than 7d] [--json]` | Delete expired workflows (`7d`, `36h`, or bare days). `--json` lists what was deleted and what was kept, with the reason. |
| `sk8s image build --tag TAG [--base IMG] [--pip PKG] [--conda PKG] [--channel CH] [--push]` | Build a worker image with extra dependencies layered on the official base image (published at release time) or on `--base IMG` (pip only). See [Custom images](#custom-images). |
| `sk8s image push --tag TAG` | `docker push` an image. |
| `sk8s shell [--image IMG]` | Open an interactive shell in a pod (needs `kubectl`). |
| `sk8s --version` | Print the version. |

## Documentation

- [Migrating from v1](docs/migration-v1-to-v2.md): every v1 call and its v2 replacement,
  and the behaviour changes to watch for.
- [Conventions](docs/conventions.md): labels, reserved workflow ids, blob key layout, and
  who owns and deletes what.
- [Developer setup](docs/dev-setup.md): minikube, the integration test suites, MinIO.
- [Worked example](examples/README.md): NA12878/BRCA2 reads-to-VCF on GKE — a detached,
  QC-gated pipeline, re-runnable verbatim.

Licensed under the MIT License.
