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

   The services suite uses its own feature-specific image tag, built the same
   way as step 3 (`-t sk8s-dev-services:latest`); the existing suite can be
   pointed at any tag with `SK8S_TEST_IMAGE`.
