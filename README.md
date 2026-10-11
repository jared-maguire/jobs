# sk8s — Run Python Functions on Kubernetes

sk8s (pronounced "skates") runs Python functions as Kubernetes jobs. Version 2 is a ground-up rebuild in progress on this branch: a futures-based API, pluggable result stores, and detached workflows. Only the pieces that exist on this branch are documented here; expect this README to grow as v2 lands.

## Quickstart

```python
import sk8s  # pip install -e .  (cluster setup: docs/dev-setup.md, minikube)

print(sk8s.run(lambda x: x * 2, 21))               # 42, run as a Kubernetes job
fut = sk8s.submit(lambda x: x + 1, 41)             # returns a concurrent.futures.Future
print(fut.result(timeout=120), sk8s.map(lambda x: x ** 2, range(5)))
```

`sk8s.LocalCluster` runs the same pipeline with local subprocesses instead of a cluster. For a development cluster, see [docs/dev-setup.md](docs/dev-setup.md).

The v1 documentation lives on the `main` branch and the `v0.1` tag.
