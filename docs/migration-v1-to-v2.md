# Migrating from sk8s v1 to v2

v2 is a rewrite, not a patch release. The idea is the same (call a function, get a
Kubernetes Job), but the API is built on `concurrent.futures`, results are pickled instead
of JSON, and everything a run creates is tracked and cleaned up. This page maps every v1
call to its v2 replacement and lists the behaviour changes that will bite if you port
call-for-call.

"v1" here means the code on the `main` branch (tag `v0.1`). Every row below was checked
against the v2 code in this repository.

- [Behaviour changes to read first](#behaviour-changes-to-read-first)
- [API mapping](#api-mapping)
- [Job options](#job-options)
- [Configuration](#configuration)
- [Command line](#command-line)
- [Removed with no replacement](#removed-with-no-replacement)

## Behaviour changes to read first

1. **`run` blocks and returns the value.** In v1, `run(f, x)` returned the *job name* unless
   you passed `asynchro=False`. In v2, `sk8s.run(f, x)` waits and returns `f(x)`. The
   non-blocking call is `sk8s.submit(f, x)`, which returns a `JobFuture` (a real
   `concurrent.futures.Future`). `wait()` and the `asynchro` flag are gone.

   Passing a v1 flag does **not** raise: any keyword argument that is not a job option
   (see [Job options](#job-options)) is forwarded to *your function*. So
   `sk8s.run(f, 1, asynchro=False)` calls `f(1, asynchro=False)` in the pod, and a v1
   spelling such as `imagePullPolicy="Never"` is silently handed to `f` too. Port the
   names, do not just delete the call sites' output handling.

2. **Results are pickled, not JSON.** v1 ran `json.dumps(func())` and read the pod's
   stdout, so results had to be JSON-serializable, tuples came back as lists, and integer
   dict keys came back as strings. v2 uses cloudpickle in both directions, so any picklable
   value round-trips exactly, `(1, {2: 3})` stays a tuple holding a dict with an int key, and
   your function is free to print (stdout is just the pod log, readable with
   `future.logs()`). Two consequences:
   - The client **unpickles** results, so only use a result store you trust.
   - The client also needs the modules of the types a job returns. Prefer builtins
     (`float`, `int`, `list`, `dict`) for portable results; numpy arrays, `numpy.float64`
     and similar require the library installed client-side too, otherwise `.result()`
     raises `ResultDecodeError` even though the job succeeded.
   - The default `k8s` store keeps each payload and result in a ConfigMap, which caps a
     blob at about 950 KB (`BlobTooLarge`). Configure an `s3://` or `gs://` store for large
     data (see the README's [Result stores](../README.md#result-stores)).

3. **Remote exceptions re-raise as themselves.** v1 raised
   `RuntimeError("Jobs 0 1 0 ... failed")`. In v2, if your function raises
   `ValueError("bad")`, the caller sees a `ValueError("bad")` whose `__cause__` is a
   `sk8s.RemoteTraceback` holding the remote traceback. If the exception class cannot be
   rebuilt locally you get `sk8s.RemoteException` (with `.type_name`, `.message`,
   `.tb_text`). Infrastructure problems have their own types, all subclasses of
   `sk8s.Sk8sError`: `PodFailed`, `ImagePullError`, `JobTimeout` (also a `TimeoutError`),
   `JobRejected` (the API server refused to create the Job — an admission denial such as
   `privileged=True` on GKE Autopilot, an RBAC `403`, or an invalid spec; in v1 these
   escaped as raw `kubernetes.client.ApiException`s), `StoreError`, `BlobNotFound`,
   `BlobTooLarge` and `VersionMismatch`. `map` raises
   `sk8s.MapFailed` after collecting every item; `e.failures` is a list of
   `(index, exception)`.

4. **The namespace is no longer whatever `kubectl` says.** v1 always used the kube
   context's namespace (so `default` unless you had pointed the context elsewhere). v2 resolves
   it as: an explicit setting (config file, `SK8S_NAMESPACE`, or a keyword argument), else
   the active kube context's namespace *if that context names one*, else the dedicated
   `sk8s` namespace. It never silently falls back to `default`. Note that minikube writes
   `namespace: default` into its context, so on minikube you get `default` until you run
   `sk8s config`, which creates the `sk8s` namespace and records it in the config file.

5. **Lifecycle is automatic.** v1's `wait()` deleted jobs; nothing else was tracked. In v2
   every run belongs to a *workflow* whose objects are owned by one anchor ConfigMap. A
   normal client deletes its workflow on exit; a crashed or detached workflow ages out after
   `retention_days` (default 7), and finished Jobs are reaped by Kubernetes after
   `ttl_seconds_after_finished` (default one hour). Results are deleted once fetched unless
   `keep_results=True`. See [conventions.md](conventions.md).

6. **`kubectl` is no longer used** (except by `sk8s shell`). v2 talks to the API with the
   Python Kubernetes client, so there are no `kubectl apply` calls, no Jinja templates and
   no YAML. The install no longer pulls in pandas, jinja2, dill, pyyaml, flask, pymongo or
   pytest; the runtime dependencies are `kubernetes` and `cloudpickle`.

7. **Python versions must match.** The worker image's Python minor version must equal the
   client's (a `VersionMismatch` is raised otherwise). v2 requires Python 3.10 or newer.

8. **Image pull policy default changed.** v1 defaulted to `Never` (it assumed a local
   image); v2 defaults to `IfNotPresent`. For locally built images set `pull_policy="Never"`
   (or `SK8S_PULL_POLICY=Never`).

## API mapping

| v1 | v2 | Notes |
|---|---|---|
| `run(f, *args)` (returns a job name) | `submit(f, *args)` | Returns a `JobFuture`; `.result(timeout=...)`, `.cancel()`, `.logs()`, `.done()`, plus everything on `concurrent.futures.Future`. |
| `run(f, *args, asynchro=False)` | `run(f, *args)` | Blocks, returns the value. `timeout=` bounds the wait and raises `JobTimeout`. |
| `wait(job)` / `wait([jobs])` | `fut.result()` / `[f.result() for f in futs]` | Futures are real `concurrent.futures` futures, so `as_completed` and `wait` from the standard library work. |
| `wait(job, delete=False)` | `submit(f, keep_results=True)` | Not identical: v2 keeps the *result blob* after it is fetched. The finished Job itself is always reaped after `ttl_seconds_after_finished` (default one hour). |
| `map(f, xs)` | `map(f, xs)` | Blocks, results in input order. Raises `MapFailed` if any item failed (after collecting all of them). One Job per item unless `batch_size` is given. |
| `map(f, xs, asynchro=True)` | `submit_map(f, xs)` | Returns a list of futures in input order. |
| `starmap(f, xs)` | `starmap(f, xs)` | Calls `f(*item)`. |
| `starmap(f, xs, asynchro=True)` | `submit_starmap(f, xs)` | |
| `map(..., chunk_size=N)` | (no equivalent, not needed) | v1's `chunk_size` only controlled how many Job specs went into one `kubectl apply`. v2 submits through the API directly. A `chunk_size=` keyword would now be forwarded to `f`. |
| `chunked_map(f, xs, size=N)` | `map(f, xs, batch_size=N)` | Packs N items into each Job. An item that raises fails only that item. Results come back flat and in order (no `chunked_wait`). |
| `chunked_starmap(f, xs, size=N)` | `starmap(f, xs, batch_size=N)` | |
| `chunked_map(..., asynchro=True)` + `chunked_wait` | `submit_map(f, xs, batch_size=N)` | Per-item futures; cancelling one cancels its whole batch Job. |
| (no equivalent) | `map(..., max_concurrent=K)` | At most K Jobs unresolved at once. |
| `run(workflow, asynchro=False, timeout=...)` as a driver Job | `submit_workflow(workflow, ...)` | Returns a `WorkflowHandle` immediately. `sk8s.attach(wf.id)` reconnects from any process; `.status()`, `.result(timeout=...)`, `.kill()`. |
| `get_job_statuses()` (pandas DataFrame) | `WorkflowHandle.status()` and `sk8s workflows` | `status()` returns `{"driver": phase, "jobs": {"active": n, "succeeded": n, "failed": n}}`. |
| `wipe_namespace()`, `python -m sk8s clean_namespace` | `sk8s.clean()`, `sk8s clean`, `sk8s kill WF` | v2 only deletes workflows it created (expired ones, or one you name); it never wipes a namespace. |
| `get_current_namespace()`, `set_namespace(ns)` | `sk8s config --namespace NS`, `SK8S_NAMESPACE`, `sk8s.configure(namespace=NS)` | See [behaviour change 4](#behaviour-changes-to-read-first). |
| `service(f, *args, name=, ports=[...], timeout=)` (returns a name, waits until ready) | `serve(f, *args, name=, port=, replicas=1, image=None)` | `name` and `port` are required keyword-only and there is exactly one port. Returns a `Service` handle (`.url`, `.logs()`, `.scale(n)`, `.delete()`) **without** waiting for readiness. Extra keyword arguments go to `f`. |
| `shutdown_service(name)` | `svc.delete()` or `get_service(name).delete()` | Removes the Deployment, Service and payload blob. |
| `forward(service, remote, local)` | `kubectl port-forward -n NS service/NAME PORT` | sk8s no longer shells out for this. `svc.url` is the in-cluster DNS name. |
| `create_volume(size, name=None, accessModes=["ReadWriteMany"], **kw)` | `create_volume(size, *, name=None, access_modes=("ReadWriteOnce",), storage_class=None, keep=False)` | Returns the PVC name, as before. Generated names are `vol-xxxxxx` (was `volume-xxxxx`). The default access mode is now `ReadWriteOnce` on the cluster's default StorageClass; ask for RWX explicitly with `access_modes=["ReadWriteMany"], storage_class="..."`. Unless `keep=True` the volume is deleted with its workflow. |
| `delete_volume(volume)` | `delete_volume(name)` | A no-op if absent; refuses PVCs sk8s did not create; does not wait for pods that still mount it. |
| (no equivalent) | `with scratch_volume("10Gi") as name:` | Creates a volume and deletes it on exit. |
| `run(..., volumes="name" \| ["a", "b"] \| {"name": "/path"})` | same | Shapes unchanged; a bare name still mounts at `/mnt/<name>`. |
| `docker_build(...)` | `sk8s image build --tag T [--pip P] [--conda C] [--channel CH] [--push]`, or `sk8s.images.build(...)` | `import sk8s.images` first; it is not loaded by `import sk8s`. See [Images](#images). |
| `docker_build(..., dryrun=True)` | `sk8s.images.generate_dockerfile(...)` | Returns the Dockerfile text. |
| `docker_build(ancestor=...)` | `sk8s.images.generate_dockerfile(base=...)` then `images.build(tag, dockerfile=text)` | Or write your own Dockerfile. `base` cannot be combined with conda dependencies. |
| `docker_build(additional_config=..., extra_options=...)` | pass your own `dockerfile=` to `images.build` | |
| `docker_build_jobs_image()`, `python -m sk8s containers` | the published image `ghcr.io/jared-maguire/sk8s:<version>-py<minor>` | Built by the release workflow and chosen automatically; for development, build `docker/Dockerfile.dev` ([dev-setup.md](dev-setup.md)). |
| `docker_push(tag)` | `sk8s image push --tag T`, `sk8s.images.push(T)` | |
| `sk8s.util.in_pod()` | `sk8s.k8s.in_pod()` | |
| `run(f, ..., test=True)` (run in-process) | `sk8s.configure(cluster=sk8s.LocalCluster())` | Same pipeline and serialization, with local subprocess workers instead of pods. Job options are accepted and ignored. |

### Futures instead of job names

```python
import concurrent.futures

import sk8s

futs = sk8s.submit_map(lambda x: x * 2, range(5))   # was: map(..., asynchro=True) + wait
for fut in concurrent.futures.as_completed(futs):
    print(fut.job_name, fut.result())

try:
    sk8s.map(lambda x: 1 // x, [1, 0, 2])            # was: RuntimeError("Jobs ... failed")
except sk8s.MapFailed as e:
    print(e.failures)                                # [(1, ZeroDivisionError(...))]
```

### Images

`sk8s image build` layers your dependencies on the official worker image (or, with
`--conda`, builds from miniforge with the client's Python and the matching sk8s release):

```python
from sk8s import images

print(images.generate_dockerfile(pip=["numpy"]))
# FROM ghcr.io/jared-maguire/sk8s:<sk8s version>-py<your Python minor version>
# RUN pip install --no-cache-dir numpy
```

The official images are published when a `v2*` tag is pushed, so a pre-release checkout
has no published base image to build on; use `docker/Dockerfile.dev` (see the README
quickstart) until the first release.

## Job options

These keyword arguments are recognised by `run`, `submit`, `map`, `starmap`, `submit_map`,
`submit_starmap` and `submit_workflow`. Everything else you pass goes to your function.

| v1 keyword | v2 keyword | Notes |
|---|---|---|
| `image` | `image` | Default: the official image for this version (`image_prefix` is prepended to its name). |
| `requests`, `limits` | `requests`, `limits` | Dicts, e.g. `{"cpu": "2", "memory": "4Gi"}`. |
| `volumes` | `volumes` | See above. |
| `privileged` | `privileged` | |
| `imagePullPolicy` | `pull_policy` | |
| `backoffLimit` | `backoff_limit` | Default `0`. |
| `serviceAccountName` | `service_account` | |
| `name="job-{s}"` | `name_prefix="job"` | A random six-character suffix is always appended. |
| `timeout` | `timeout` | `run` / `map` / `starmap` only (`submit` discards it; pass it to `future.result()`). |
| (none) | `keep_results` | Keep the result blob after it is fetched. |
| (none) | `namespace` | Accepted, but must equal the cluster's namespace; per-job namespaces are not supported yet. |
| (none) | `batch_size`, `max_concurrent` | Fan-out controls for the `map` family only. |
| `job_template`, `dryrun`, `state`, `config`, `export_config`, `test` | removed | Jobs are built from Kubernetes client models, there is no template to override. Memoization (`state`) is gone. |

## Configuration

| | v1 | v2 |
|---|---|---|
| File | `~/.sk8s/config.json` (JSON, created automatically with local-cluster defaults) | `~/.config/sk8s/config.toml` (TOML, written by `sk8s config`) |
| Old file | | **Not read.** Re-run `sk8s config` and re-enter any settings you need. |
| Layering | the file only | defaults, then the file, then `SK8S_<FIELD>` environment variables, then keyword arguments (`sk8s.configure(...)`, `Cluster(**overrides)`) |

| v1 key | v2 setting |
|---|---|
| `docker_image_prefix` | `image_prefix` (prepended to the default image name), or set `image` outright |
| `docker_default_pull_policy` | `pull_policy` (default now `IfNotPresent`) |
| `service_account_name` | `service_account` (default `sk8s`) |
| `result_obs_prefix` (`s3://...`) | `store = "s3://bucket/prefix"` (plus `store_endpoint` for S3-compatible services); `gs://` also works |
| `default_storageclass`, `default_readwritemany_storageclass` | pass `storage_class=` to `create_volume` |
| `docker_build_default_push_policy`, `ecr_create_repo_on_push` | removed; pass `push=True` / `--push` explicitly |
| (none) | `namespace`, `store`, `store_env`, `ttl_seconds_after_finished` (3600), `retention_days` (7), `keep_results` (false) |

The v2 fields are exactly those of `sk8s.Config`. `store_env` (a TOML table of extra
environment variables for worker pods, such as MinIO credentials) comes from the file or
keyword arguments only, never from `SK8S_*` variables.

## Command line

`python -m sk8s` no longer exists (the package has no `__main__`); the `sk8s` command is
installed by `pip`. Flags are double-dashed (`--dry-run`, not `-dryrun`).

| v1 | v2 |
|---|---|
| `python -m sk8s config-cluster -dryrun` | `sk8s config --dry-run` |
| `python -m sk8s config-cluster -apply` / `config-local` | `sk8s config [--namespace NS]` |
| `python -m sk8s config-cluster -check` | `sk8s doctor` |
| `python -m sk8s config-gke ...`, `config-eks ...` | `sk8s config --provider generic` creates the namespace and RBAC on any cluster. On GKE use `sk8s config --provider gke --project P --region R --gsa NAME` (prints the `gcloud` Workload Identity plan; `--apply-cloud` runs it); the full walkthrough is [gke.md](gke.md). An EKS profile is not available yet (`--provider eks` is rejected as "tabled"); an EKS guide is forthcoming. |
| `python -m sk8s containers [-push] [-dev_mode]` | `sk8s image build --tag T --pip P [--push]` for custom images; `docker/Dockerfile.dev` for development builds |
| `python -m sk8s clean_namespace` | `sk8s clean [--older-than 7d]`, `sk8s kill WORKFLOW` |
| `python -m sk8s shell -image I -volumes ... -privileged ...` | `sk8s shell [--image I]` (the volume, privileged and service-account flags are gone) |
| `python -m sk8s wait -job J` | none: jobs are not addressed by name; use `sk8s.attach(workflow_id).result()` |
| `python -m sk8s kubewatch`, `dashboard` | `sk8s workflows`, `sk8s logs WORKFLOW`, or `kubectl` |

New in v2: `sk8s workflows`, `sk8s logs`, `sk8s kill`, `sk8s clean`, `sk8s doctor`,
`sk8s image push`.

## Removed with no replacement

- Workflow-state memoization (`sk8s.state`, the `state=` argument).
- The MongoDB, Kafka and key-value-store helpers (`create_mongo_db`, `kvs_service`, the
  Kafka deployment template). Run them as ordinary containers and reach them from jobs, or
  `serve()` your own function.
- The terminal dashboard and `kubewatch`.
- Job and service YAML templates (`job_template=`, `template=`) and `dryrun` output.
- Cloud-specific setup code for GKE and EKS (GKE is now `sk8s config --provider gke`; EKS is tabled; see the command-line table above).
