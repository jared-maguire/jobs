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
- **Any picklable value** goes in and comes out (cloudpickle), not just JSON.
- **Detached workflows.** Submit a driver function, close your laptop, and `attach` to it
  later from any process.
- **Services and volumes** are first-class: `serve()` a function as a Deployment behind a
  ClusterIP Service, `create_volume()` a PersistentVolumeClaim and mount it into Jobs.
- **Nothing leaks.** Every object a workflow creates is owned by one "anchor" ConfigMap,
  so deleting a workflow (or letting retention expire it) removes all of it.

> **Status: 2.0.0a0 (pre-release).** Not on PyPI yet and no worker images are published
> yet, so the quickstart below builds the worker image from this checkout. The v1
> documentation lives on the `main` branch and the `v0.1` tag; upgrading is covered in the
> [migration guide](docs/migration-v1-to-v2.md). Cloud-specific guides (GKE, EKS) are
> forthcoming; the quickstart targets minikube.

## Quickstart (about 5 minutes)

You need Python 3.10+, Docker, [minikube](https://minikube.sigs.k8s.io/) and `kubectl`.

**1. Start a cluster.**

```bash
minikube start
```

**2. Install sk8s** from a checkout (`pip install -e .` installs the `sk8s` command too):

```bash
git clone -b version-2 https://github.com/jared-maguire/jobs.git sk8s && cd sk8s
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

**3. Build the worker image** into minikube. The worker's Python minor version must match
your local interpreter (the payload carries a version check); `docker/Dockerfile.dev`
defaults to Python 3.14. For another version, pass a build arg, e.g.
`--build-opt=opt=build-arg:PYTHON_VERSION=3.12` (see [docs/dev-setup.md](docs/dev-setup.md)
for the syntax per minikube container runtime).

```bash
minikube image build -t sk8s:dev -f docker/Dockerfile.dev .
export SK8S_IMAGE=sk8s:dev SK8S_PULL_POLICY=Never   # use that image, never pull it
```

**4. Prepare a namespace.** `sk8s config` creates the `sk8s` namespace with a
ServiceAccount, Role and RoleBinding, and writes `~/.config/sk8s/config.toml`:

```bash
sk8s config --namespace sk8s
sk8s doctor        # five PASS lines when the cluster is ready
```

**5. Run a function.**

```python
import sk8s

print(sk8s.run(lambda x: x * 2, 21))  # one function, one Kubernetes Job -> 42

fut = sk8s.submit(lambda x: x + 1, 41)  # same, but returns a Future immediately
print(fut.result(timeout=120))          # 42
```

**6. Map over many inputs.** `batch_size` packs several items into each Job, and an item
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
`sk8s.MapFailed` lists the failing indices (`e.failures`). Use `submit_map` /
`submit_starmap` to get the futures themselves.

**7. Start a detached workflow.** The driver function runs in the cluster as a Job and can
submit its own Jobs. Your process does not need to stay alive:

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

**8. Serve a function.** `serve()` runs a function that blocks forever (here an HTTP
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

**9. Use a volume.** A volume is a PersistentVolumeClaim mounted at `/mnt/<name>`.
`keep=True` makes it independent of any workflow (otherwise it is deleted with the
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
sk8s.run(write, volumes="scratch")      # mounted at /mnt/scratch
print(sk8s.run(read, volumes="scratch"))  # written by one job
sk8s.delete_volume("scratch")
```

That is the whole tour. To try the API with no cluster at all, swap in local
subprocess workers (same pipeline, same serialization):

```python
import sk8s

sk8s.configure(cluster=sk8s.LocalCluster())
print(sk8s.run(lambda x: x * 2, 21))  # 42
```

## Custom images

The official worker image has only sk8s and the standard library. To add packages, layer
them on a base image.

### Recommended: use the minikube registry addon

A local registry makes minikube behave like a real cluster: plain `docker build` and
`docker push`, and jobs pull by name with the default pull policy.

```bash
minikube addons enable registry
kubectl -n kube-system port-forward svc/registry 5000:80 &   # keep this running while you push

# a Dockerfile layered on your base image, e.g. FROM sk8s:dev + RUN pip install pysam
docker build -t localhost:5000/pysam-worker:1 .
docker push localhost:5000/pysam-worker:1
```

```python
import sk8s

print(sk8s.run(lambda: __import__("pysam").__version__, image="localhost:5000/pysam-worker:1"))
```

Pods resolve `localhost:5000` through the addon's node-local proxy, so no pull-policy
override is needed. Setting `image_prefix = "localhost:5000/"` in
`~/.config/sk8s/config.toml` prepends that registry to the default worker image name,
the same way you would point a real cluster at an ECR or GAR mirror prefix.

### Fallback: build straight into minikube (no registry)

Build on the local base image from the quickstart:

```bash
sk8s image build --tag pysam-worker:1 --base sk8s:dev --pip pysam --minikube
```

The CLI checks that the image really landed in minikube's image store and fails loudly if
it did not. Then point a run, or every run, at it:

```python
import sk8s

print(sk8s.run(lambda: __import__("pysam").__version__, image="pysam-worker:1"))
sk8s.configure(image="pysam-worker:1")  # or SK8S_IMAGE=pysam-worker:1
```

(With `SK8S_PULL_POLICY=Never` exported as in the quickstart, minikube uses the local
image as is.) When `--base` is omitted the base is the configured worker image, so an
exported `SK8S_IMAGE` also serves as the default base. For PyTorch use `--pip torch`; the
resulting image is large, so expect a slow first build. `--base` only applies to pip
dependencies. `--conda` builds from miniforge and installs sk8s from PyPI at your exact
version, so it needs the first PyPI release. On a real cluster, build with `--push`
instead of `--minikube` and use a tag your cluster can pull.

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

Settings layer as: defaults, then `~/.config/sk8s/config.toml`, then `SK8S_*` environment
variables (`SK8S_NAMESPACE`, `SK8S_IMAGE`, `SK8S_STORE`, ...), then keyword arguments
(`sk8s.configure(namespace=...)`, `Cluster(config, **overrides)`). The fields are listed in
`sk8s.Config`. The namespace is the explicit setting if there is one, else the active kube
context's namespace if it names one, else `sk8s`.

## The `sk8s` command

| Command | What it does |
|---|---|
| `sk8s config [--namespace NS] [--store URL] [--provider local\|generic] [--dry-run]` | Create the namespace, ServiceAccount, Role and RoleBinding; write the client config. `--dry-run` prints the plan only. |
| `sk8s doctor` | Check cluster reachability, namespace, ServiceAccount, permissions and store; non-zero exit if any check fails. |
| `sk8s workflows` | List workflows in the namespace (id, detached, age, driver phase). |
| `sk8s logs WORKFLOW` | Print a workflow driver's logs. |
| `sk8s kill WORKFLOW` | Delete a workflow and everything it owns. |
| `sk8s clean [--older-than 7d]` | Delete expired workflows (`7d`, `36h`, or bare days). |
| `sk8s image build --tag TAG [--base IMG] [--pip PKG] [--conda PKG] [--channel CH] [--push \| --minikube]` | Build a worker image with extra dependencies layered on the official base image (published at release time) or on `--base IMG` (pip only). See [Custom images](#custom-images). |
| `sk8s image push --tag TAG` | `docker push` an image. |
| `sk8s shell [--image IMG]` | Open an interactive shell in a pod (needs `kubectl`). |
| `sk8s --version` | Print the version. |

## Documentation

- [Migrating from v1](docs/migration-v1-to-v2.md): every v1 call and its v2 replacement,
  and the behaviour changes to watch for.
- [Conventions](docs/conventions.md): labels, reserved workflow ids, blob key layout, and
  who owns and deletes what.
- [Developer setup](docs/dev-setup.md): minikube, the integration test suites, MinIO.

Licensed under the MIT License.
