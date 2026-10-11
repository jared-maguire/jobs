# Developer setup (minikube)

1. `minikube start`
2. Service account + RBAC (temporary until `sk8s config` lands in the
   cluster-config feature). Make sure `kubectl config current-context` is
   `minikube` first:

   ```
   kubectl create serviceaccount sk8s
   kubectl create role sk8s --verb=get,list,watch,create,delete,patch \
     --resource=jobs,pods,pods/log,configmaps,persistentvolumeclaims,deployments.apps,services
   kubectl create rolebinding sk8s --role=sk8s --serviceaccount=default:sk8s
   ```

   `deployments.apps` and `services` (and the `patch` verb, used by
   `Service.scale()`) are needed by `sk8s.serve()`. If your cluster already has
   the older role, re-apply it:

   ```
   kubectl create role sk8s --verb=get,list,watch,create,delete,patch \
     --resource=jobs,pods,pods/log,configmaps,persistentvolumeclaims,deployments.apps,services \
     --dry-run=client -o yaml | kubectl replace -f -
   ```
3. Build the dev worker image inside minikube. The worker's Python minor
   version must match your local interpreter (the serialize handshake rejects
   mismatches); the Dockerfile defaults to 3.14 and takes a build arg:

   ```
   minikube image build -t sk8s-dev:latest -f docker/Dockerfile.dev .
   # other Python: minikube image build --build-opt=build-arg=PYTHON_VERSION=3.12 ...
   ```
4. Run the suite:

   ```
   SK8S_INTEGRATION=1 pytest tests/integration -v
   ```

   The workflow suite (`tests/integration/test_workflows.py`) has jobs submit
   jobs, so the in-pod worker must run *this* branch's code. Tag that build
   `sk8s-dev-workflows:latest` (override with `SK8S_WORKFLOW_TEST_IMAGE`) and run
   the older suite against it with `SK8S_TEST_IMAGE=sk8s-dev-workflows:latest`.
   Workflows use the same `sk8s` ServiceAccount RBAC as above (jobs and
   configmaps: get/list/watch/create/delete/patch).

   The services suite uses its own feature-specific image tag, built the same
   way as step 3 (`-t sk8s-dev-services:latest`); the existing suite can be
   pointed at any tag with `SK8S_TEST_IMAGE`.

   The volumes suite (`tests/integration/test_volumes.py`) uses
   `sk8s-dev-volumes:latest` (override with `SK8S_VOLUMES_TEST_IMAGE`) and needs
   `persistentvolumeclaims` in the Role (already in the command above) plus a default
   StorageClass (minikube's `standard`).

## S3 store integration tests (MinIO)

`tests/integration/test_store_s3_minio.py` runs the S3 store against a MinIO
deployed in the cluster by `tests/integration/minio.py` (Deployment + Service
`minio` in the current namespace, default `minioadmin` credentials; created on
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
