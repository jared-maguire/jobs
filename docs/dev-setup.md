# Developer setup (minikube)

This page is for people hacking on sk8s itself: running the integration suites,
building worker images, and testing against MinIO. If you just want to *use*
sk8s on minikube, the [README quickstart](../README.md#quickstart-about-5-minutes) is shorter
(it uses the registry addon and the default pull policy; this page's `minikube image build`
tags are the test suite's convention instead, see [Running the suites](#running-the-suites)).
See also: [migration guide](migration-v1-to-v2.md) and
[conventions reference](conventions.md) (labels, workflow ids, blob keys).

Contents: [Namespace](#namespace) - [Cluster and image](#cluster-and-image) -
[Legacy default-namespace RBAC](#legacy-default-namespace-rbac) -
[Running the suites](#running-the-suites) -
[S3 store tests (MinIO)](#s3-store-integration-tests-minio)

## Namespace

sk8s runs in a dedicated namespace, **`sk8s`**, and never falls back to
`default`. When nothing names a namespace, it is resolved as: explicit setting
(config file, `SK8S_NAMESPACE`, or a kwarg) > the active kube context's
namespace, when that context explicitly sets one > `sk8s`. Inside a worker pod
the pod's own namespace always wins.

`sk8s config` creates the namespace (plus the ServiceAccount, Role and
RoleBinding) and records it in `~/.config/sk8s/config.toml` (or the file named by
`SK8S_CONFIG`, handy for throwaway clusters and agents); `--namespace`
defaults to `sk8s` (or the namespace you already configured). `sk8s doctor`
tells you to run `sk8s config` if any of that is missing:

```
sk8s config --dry-run   # show the plan
sk8s config             # apply it and write the client config
sk8s doctor
```

## Cluster and image

1. `minikube start`. Make sure `kubectl config current-context` is `minikube`
   before running anything below.
2. Build the dev worker image inside minikube. The worker's Python minor
   version must match your local interpreter (the serialize handshake rejects
   mismatches); the Dockerfile defaults to 3.14 and takes a build arg:

   ```
   minikube image build -t sk8s-dev:latest -f docker/Dockerfile.dev .
   ```

   For a different Python, pass the build arg. This is the buildctl (containerd
   runtime) form, verified with minikube 1.38 and what CI uses (CI pins
   `container-runtime: containerd`):

   ```
   minikube image build -t sk8s-dev:latest -f docker/Dockerfile.dev \
     --build-opt=opt=build-arg:PYTHON_VERSION=3.12 .
   ```

   A minikube on the docker runtime may instead accept the
   `--build-opt=build-arg=PYTHON_VERSION=3.12` spelling (`minikube profile list` shows
   your runtime).

   The raw `minikube image build` can exit 0 even when the build fails (for example an
   unresolvable `FROM`, or a `-f` Dockerfile outside the build context), so check
   `minikube image ls | grep sk8s-dev` after building.

   Two more `minikube image` caveats. Every `minikube image build` leaks a context
   tarball at `/tmp/build.*.tar` (multi-GB if `.venv` is in the context, which the
   repo `.dockerignore` now excludes), so clean `/tmp` periodically or prefer the
   registry route below. And `minikube image rm` removes by image ID, so tags that
   share an ID (for example an identical rebuild under a new tag) vanish together.

   **Registry route (recommended for custom images).** The README quickstart already
   enables the addon and starts the port-forward; if you skipped it, enable the addon, keep
   a port-forward running while you push, then use plain docker:

   ```bash
   minikube addons enable registry
   kubectl -n kube-system port-forward svc/registry 5000:80 &
   docker build -t localhost:5000/pysam-worker:1 . && docker push localhost:5000/pysam-worker:1
   ```

   Jobs then use `image="localhost:5000/pysam-worker:1"` with the default pull policy
   (pods reach `localhost:5000` through the addon's node-local proxy). See
   [Custom images](../README.md#custom-images) in the README.

## Legacy default-namespace RBAC

The integration suites predate `sk8s config` and provision the **`default`**
namespace by hand, so they pin `namespace="default"` explicitly (they do not
follow the `sk8s` default). For everything else use `sk8s config`.

```
kubectl create serviceaccount sk8s
kubectl create role sk8s --verb=get,list,watch,create,delete,patch \
  --resource=jobs,pods,pods/log,configmaps,persistentvolumeclaims,deployments.apps,services
kubectl create rolebinding sk8s --role=sk8s --serviceaccount=default:sk8s
```

`deployments.apps` and `services` (and the `patch` verb, used by
`Service.scale()`) are needed by `sk8s.serve()`; `persistentvolumeclaims` by
volumes. If your cluster already has an older role, re-apply it:

```
kubectl create role sk8s --verb=get,list,watch,create,delete,patch \
  --resource=jobs,pods,pods/log,configmaps,persistentvolumeclaims,deployments.apps,services \
  --dry-run=client -o yaml | kubectl replace -f -
```

## Running the suites

```
SK8S_INTEGRATION=1 pytest tests/integration -v
```

Most suites default to their own feature-specific image tag (built the same
way as above, with a different `-t`) and have an environment override. The suites
configure `pull_policy="Never"` themselves for these bare local tags (the test suite's
local-image convention, built with `minikube image build` and no registry); it is
distinct from the README quickstart, which pushes to the registry addon and uses the
default pull policy. The README's [no-registry aside](../README.md#no-registry-building-directly-into-minikube)
explains why `Never` is the right policy for bare local tags:

| Suite | Default image tag | Override |
|---|---|---|
| `test_minikube.py` (core run/map) | `sk8s-dev:latest` | `SK8S_TEST_IMAGE` |
| `test_workflows.py` | `sk8s-dev-workflows:latest` | `SK8S_WORKFLOW_TEST_IMAGE` |
| `test_services.py` | `sk8s-dev-services:latest` | `SK8S_SERVICES_TEST_IMAGE` |
| `test_volumes.py` | `sk8s-dev-volumes:latest` | `SK8S_VOLUMES_TEST_IMAGE` |
| `test_config_cli.py` | `sk8s-dev-config:latest` | `SK8S_CONFIG_TEST_IMAGE` |
| `test_store_s3_minio.py` | `sk8s-dev-stores:latest` | `SK8S_TEST_IMAGE` |
| `test_stress.py` | `sk8s-dev-cidocs:latest` | `SK8S_STRESS_TEST_IMAGE` |

Suite notes:

- **Workflows.** The workflow suite has jobs submit jobs, so the in-pod worker
  must run *this* branch's code (tag that build `sk8s-dev-workflows:latest`). It
  uses the same `sk8s` ServiceAccount RBAC as above (jobs and configmaps:
  get/list/watch/create/delete/patch).
- **Services.** Uses its own tag, built the same way as the dev image.
- **Volumes.** Needs `persistentvolumeclaims` in the Role (already in the
  command above) plus a default StorageClass (minikube's `standard`).
- **Config / doctor / CLI.** Needs no hand-made RBAC: it creates and deletes its
  own namespaces (`sk8s-it-cfg*`) with `sk8s config`'s machinery, and runs the
  CLI with a temp `HOME` so your real `~/.config/sk8s/config.toml` is untouched.
- **Stress.** `tests/integration/test_stress.py` is marked `stress` as well as
  `integration`; CI excludes it (`-m "integration and not stress"`).

## S3 store integration tests (MinIO)

`tests/integration/test_store_s3_minio.py` runs the S3 store against a MinIO
deployed in the cluster by `tests/integration/minio.py` (Deployment + Service
`minio` in the `default` namespace, default `minioadmin` credentials; created on
first use and reused afterwards, never torn down automatically). The client
reaches it through a `kubectl port-forward svc/minio` that the fixture starts and
stops; worker pods reach it via `minio.default.svc.cluster.local:9000` and get
their credentials from `Config.store_env`. `store_env` values land in plaintext in the Job spec and the config file, so use it for dev/MinIO credentials only; production should use IRSA / Workload Identity or Secret-sourced env.

Two endpoint variables control the S3 endpoint and are easy to confuse.
`SK8S_STORE_ENDPOINT` is the config-layered setting (file < env < kwargs) that
the client uses and that is forwarded to worker pods as `Config.store_endpoint`.
`SK8S_S3_ENDPOINT` is the highest-precedence runtime override on both sides: it
wins over `Config.store_endpoint` in the client, and it is also the variable
workers receive in their Job env.

1. Build a worker image that includes the `s3` extra (boto3). Use a feature
   tag so you do not clobber `sk8s-dev:latest`:

   ```
   minikube image build -t sk8s-dev-stores:latest -f docker/Dockerfile.dev .
   ```
2. Run the tests (the image defaults to `sk8s-dev-stores:latest`; override with
   `SK8S_TEST_IMAGE`):

   ```
   SK8S_INTEGRATION=1 pytest tests/integration/test_store_s3_minio.py -v
   ```

Notes: `minio/minio` is no longer published on Docker Hub or quay.io, so the
helper uses `cgr.dev/chainguard/minio:latest` (override with
`SK8S_TEST_MINIO_IMAGE`). To remove it: `kubectl delete deploy,svc minio`.
