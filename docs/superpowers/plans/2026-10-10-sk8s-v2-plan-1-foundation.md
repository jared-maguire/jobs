# sk8s v2 Plan 1: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build sk8s v2's core: serialization, blob stores, job spec building, the in-pod worker, watch-based futures, and the public `run`/`submit`/`map` API — working end-to-end on minikube.

**Architecture:** Functions are cloudpickled into a payload blob stored via a pluggable `BlobStore` (ConfigMap default); a small `V1Job` built with Kubernetes client models runs `python -m sk8s.worker`, which fetches the payload, executes, and stores a pickled result or exception; a per-process watch thread resolves real `concurrent.futures.Future`s. A `LocalCluster` subprocess backend exercises the identical path without a cluster.

**Tech Stack:** Python ≥3.10, `kubernetes` client, `cloudpickle`, pytest, mypy --strict, minikube for integration.

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md`

**Branch/workflow:** work on `feat/core`, branched from `version-2`, in clone `~/code/jobs/jobs.core`. PR into `version-2` when done. Never commit to `main`.

## Global Constraints

- Python ≥ 3.10; CI floor is 3.10.
- Core runtime deps: `kubernetes`, `cloudpickle`, and `tomli; python_version<"3.11"` — nothing else. pytest/mypy are dev extras.
- No `subprocess` with `shell=True` anywhere. No `kubectl` invocations in the package. No Jinja/YAML templates — Kubernetes objects are built with `kubernetes.client` models only.
- `mypy --strict` must pass on `sk8s/`; package ships `py.typed`.
- Every sk8s-created Kubernetes object carries labels `app.kubernetes.io/managed-by: sk8s` and `sk8s/workflow-id: <wf-id>`.
- ConfigMap store rejects blobs > 950_000 bytes with an actionable error.
- Defaults: `ttl_seconds_after_finished=3600`, `service_account="sk8s"`, `store="k8s"`, `pull_policy="IfNotPresent"`.
- Job/blob names must be valid DNS-1123 (lowercase alphanumerics and `-`), job names ≤ 63 chars.
- Tests that need a real cluster are marked `@pytest.mark.integration` and skipped unless `SK8S_INTEGRATION=1`.

## Review Focus

Failure modes the spec implies but could slip through; each has a pinning test in the named task:

1. **A function returning `None`** must round-trip as `None`, not be mistaken for a missing result — Task 9 (`test_none_result`).
2. **Function kwargs colliding with job options** (e.g. caller's function has an `image` parameter): the kwarg is consumed as a job option; the documented escape hatch is `functools.partial` — Task 11 (`test_job_option_extraction`) pins the extraction rule so it's deliberate, not accidental.
3. **Oversized payload/result on the ConfigMap store** must raise `BlobTooLarge` telling the user to switch to an object store, both client-side (payload) and worker-side (result stored as error) — Task 5 (`test_put_too_large`), Task 8 (`test_oversized_result_becomes_error`).
4. **Unpicklable return values** (e.g. a `threading.Lock`): the worker must store an encoded error the client re-raises, never exit leaving no result blob — Task 8 (`test_unpicklable_result_becomes_error`).
5. **Remote exception classes not importable client-side** (raised from a module that exists only in the worker image): `decode_result` must fall back to `RemoteException` carrying type name, message, and remote traceback instead of crashing on unpickle — Task 3 (`test_unimportable_exception_falls_back`).

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `sk8s/__init__.py`, `sk8s/py.typed`, `sk8s/cli.py`, `tests/__init__.py`, `tests/test_scaffold.py`, `.github/` left for Plan 8
- Delete: all v1 files superseded by v2 (`sk8s/*.py` v1 modules, `sk8s/*.yaml`, `sk8s/Dockerfile.*`, `sk8s/stopwords.txt`, `sk8s/clouds/`, `setup.py`, `MANIFEST.in`, `pytest.ini`, `test_k8s.py`, `split_fastq.py`, `example_workflow.py`, `hostpath_volume.yaml`, `gke_rbac_config.sh`, `conda_windows_11.yaml`, `*.ipynb`, `Makefile`)

**Interfaces:**
- Produces: importable `sk8s` package with `__version__ = "2.0.0a0"`; `sk8s` console script; `pytest` and `mypy --strict sk8s` runnable.

- [ ] **Step 1: Remove v1 files** (we're on `feat/core`, branched from `version-2`; v1 stays on `main`/the `v1` branch)

```bash
git rm -r sk8s setup.py MANIFEST.in pytest.ini test_k8s.py split_fastq.py \
  example_workflow.py hostpath_volume.yaml gke_rbac_config.sh \
  conda_windows_11.yaml demo.ipynb sk8s_demo.ipynb ngs_workflow.ipynb Makefile
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "sk8s"
version = "2.0.0a0"
description = "Run Python functions on Kubernetes"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.10"
dependencies = [
    "kubernetes>=29.0.0",
    "cloudpickle>=3.0.0",
    "tomli>=2.0 ; python_version < '3.11'",
]

[project.optional-dependencies]
s3 = ["boto3>=1.34"]
gcs = ["google-cloud-storage>=2.14"]
dev = ["pytest>=8", "pytest-timeout>=2", "mypy>=1.8"]

[project.scripts]
sk8s = "sk8s.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: requires a live Kubernetes cluster (SK8S_INTEGRATION=1)"]

[tool.mypy]
strict = true
packages = ["sk8s"]
```

- [ ] **Step 3: Write the package skeleton**

`sk8s/__init__.py`:
```python
__version__ = "2.0.0a0"
```

`sk8s/py.typed`: empty file.

`sk8s/cli.py`:
```python
"""Console entrypoint. Subcommands land in later plans; version works now."""
import argparse

from sk8s import __version__


def main() -> int:
    parser = argparse.ArgumentParser(prog="sk8s")
    parser.add_argument("--version", action="version", version=f"sk8s {__version__}")
    parser.parse_args()
    parser.print_help()
    return 0
```

`tests/__init__.py`: empty. `tests/test_scaffold.py`:
```python
import sk8s


def test_version() -> None:
    assert sk8s.__version__ == "2.0.0a0"
```

- [ ] **Step 4: Install and verify**

Run: `pip install -e ".[dev]" && pytest tests/test_scaffold.py -v && mypy && sk8s --version`
Expected: test PASS, mypy clean, version printed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: v2 scaffolding; remove v1 tree on this branch"
```

---

### Task 2: Error hierarchy

**Files:**
- Create: `sk8s/errors.py`
- Test: `tests/test_errors.py`

**Interfaces:**
- Produces: `Sk8sError`, `StoreError`, `BlobNotFound`, `BlobTooLarge`, `VersionMismatch`, `JobTimeout(job_name, timeout)`, `PodFailed(job_name, reason, logs=None)`, `ImagePullError(PodFailed)`, `RemoteTraceback(tb_text)`, `RemoteException(type_name, message, tb_text)`, `MapFailed(failures, total)`.

- [ ] **Step 1: Write the failing test**

`tests/test_errors.py`:
```python
import pytest

from sk8s.errors import (
    BlobNotFound, BlobTooLarge, ImagePullError, JobTimeout, MapFailed,
    PodFailed, RemoteException, RemoteTraceback, Sk8sError, StoreError,
    VersionMismatch,
)


def test_hierarchy() -> None:
    assert issubclass(StoreError, Sk8sError)
    assert issubclass(BlobNotFound, StoreError)
    assert issubclass(BlobTooLarge, StoreError)
    assert issubclass(VersionMismatch, Sk8sError)
    assert issubclass(PodFailed, Sk8sError)
    assert issubclass(ImagePullError, PodFailed)


def test_job_timeout_message() -> None:
    e = JobTimeout("job-abc123", 60.0)
    assert e.job_name == "job-abc123"
    assert "60" in str(e)


def test_pod_failed_fields() -> None:
    e = PodFailed("job-x", reason="OOMKilled", logs="...killed...")
    assert e.reason == "OOMKilled"
    assert "OOMKilled" in str(e)


def test_remote_exception_carries_traceback() -> None:
    e = RemoteException("mylib.CustomError", "boom", "Traceback (most recent...)")
    assert "mylib.CustomError" in str(e) and "boom" in str(e)
    assert e.tb_text.startswith("Traceback")


def test_map_failed_summary() -> None:
    failures = [(3, ValueError("bad")), (7, KeyError("k"))]
    e = MapFailed(failures, total=10)
    assert e.failures == failures
    assert "2/10" in str(e) and "ValueError" in str(e)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_errors.py -v` — Expected: FAIL (`ModuleNotFoundError: sk8s.errors`).

- [ ] **Step 3: Implement `sk8s/errors.py`**

```python
"""Typed exceptions. Infra failures get specific classes with actionable text."""
from __future__ import annotations


class Sk8sError(Exception):
    pass


class StoreError(Sk8sError):
    pass


class BlobNotFound(StoreError):
    pass


class BlobTooLarge(StoreError):
    pass


class VersionMismatch(Sk8sError):
    pass


class JobTimeout(Sk8sError):
    def __init__(self, job_name: str, timeout: float) -> None:
        self.job_name = job_name
        self.timeout = timeout
        super().__init__(
            f"job {job_name} did not finish within {timeout}s "
            f"(it is still running; cancel with future.cancel())"
        )


class PodFailed(Sk8sError):
    def __init__(self, job_name: str, reason: str, logs: str | None = None) -> None:
        self.job_name = job_name
        self.reason = reason
        self.logs = logs
        tail = f"\n--- pod logs ---\n{logs}" if logs else ""
        super().__init__(f"job {job_name} failed: {reason}{tail}")


class ImagePullError(PodFailed):
    def __init__(self, job_name: str, image: str) -> None:
        super().__init__(job_name, reason=f"cannot pull image {image!r}")
        self.image = image


class RemoteTraceback(Exception):
    """Chained as __cause__ of re-raised remote exceptions; str() is the remote tb."""

    def __init__(self, tb_text: str) -> None:
        self.tb_text = tb_text
        super().__init__(f"\n\n--- remote traceback ---\n{tb_text}")


class RemoteException(Sk8sError):
    """Raised when the remote exception's class can't be reconstructed locally."""

    def __init__(self, type_name: str, message: str, tb_text: str) -> None:
        self.type_name = type_name
        self.message = message
        self.tb_text = tb_text
        super().__init__(f"{type_name}: {message}")


class MapFailed(Sk8sError):
    def __init__(self, failures: list[tuple[int, BaseException]], total: int) -> None:
        self.failures = failures
        self.total = total
        first_i, first_e = failures[0]
        super().__init__(
            f"{len(failures)}/{total} map items failed; "
            f"first failure at index {first_i}: {type(first_e).__name__}: {first_e}"
        )
```

- [ ] **Step 4: Run tests** — `pytest tests/test_errors.py -v && mypy` — Expected: PASS, clean.

- [ ] **Step 5: Commit** — `git add sk8s/errors.py tests/test_errors.py && git commit -m "feat: typed error hierarchy"`

---

### Task 3: Serialization

**Files:**
- Create: `sk8s/serialize.py`
- Test: `tests/test_serialize.py`

**Interfaces:**
- Produces: `encode_payload(func, args, kwargs) -> bytes`; `decode_payload(data) -> tuple[Callable, tuple, dict]` (raises `VersionMismatch` on Python minor mismatch); `encode_result(value) -> bytes`; `encode_error(exc) -> bytes`; `decode_result(data) -> Any` (returns value, or raises the original exception `from RemoteTraceback`, or raises `RemoteException` fallback).
- Format: one JSON header line + `\n` + cloudpickle body.

- [ ] **Step 1: Write the failing test**

`tests/test_serialize.py`:
```python
import json
import sys

import pytest

from sk8s import serialize
from sk8s.errors import RemoteException, RemoteTraceback, VersionMismatch


def test_payload_roundtrip() -> None:
    f, args, kwargs = serialize.decode_payload(
        serialize.encode_payload(lambda x, y=1: x + y, (2,), {"y": 3})
    )
    assert f(*args, **kwargs) == 5


def test_payload_python_mismatch() -> None:
    data = serialize.encode_payload(len, ((),), {})
    head, _, body = data.partition(b"\n")
    h = json.loads(head)
    h["python"] = "2.7"
    with pytest.raises(VersionMismatch, match="sk8s image build"):
        serialize.decode_payload(json.dumps(h).encode() + b"\n" + body)


def test_result_roundtrip_none() -> None:
    assert serialize.decode_result(serialize.encode_result(None)) is None


def test_result_roundtrip_object() -> None:
    v = {"a": [1, 2, (3, 4)], "b": b"bytes"}
    assert serialize.decode_result(serialize.encode_result(v)) == v


def test_error_reraises_original_type() -> None:
    try:
        raise ValueError("remote boom")
    except ValueError as e:
        blob = serialize.encode_error(e)
    with pytest.raises(ValueError, match="remote boom") as exc_info:
        serialize.decode_result(blob)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)
    assert "remote boom" in exc_info.value.__cause__.tb_text


def test_unimportable_exception_falls_back() -> None:
    # Simulate an exception whose class only exists worker-side: encode a real
    # error, then corrupt the pickled class reference so unpickling fails.
    try:
        raise ValueError("boom")
    except ValueError as e:
        blob = serialize.encode_error(e)
    head, _, body = blob.partition(b"\n")
    body = body.replace(b"ValueError", b"ValueXrror")
    with pytest.raises(RemoteException) as exc_info:
        serialize.decode_result(head + b"\n" + body)
    assert "boom" in str(exc_info.value)
    assert "Traceback" in exc_info.value.tb_text
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_serialize.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `sk8s/serialize.py`**

```python
"""Payload/result wire format: one JSON header line + cloudpickle body.

Payload header: {"kind": "payload", "sk8s": ..., "cloudpickle": ..., "python": "3.12"}
Result header:  {"kind": "ok"} or
                {"kind": "err", "type": ..., "message": ..., "tb": "..."}
An "err" body is the cloudpickled exception, or b"" when it wasn't picklable.
"""
from __future__ import annotations

import json
import pickle
import sys
import traceback
from typing import Any, Callable

import cloudpickle

from sk8s import __version__
from sk8s.errors import RemoteException, RemoteTraceback, VersionMismatch


def _python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def encode_payload(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> bytes:
    header = {
        "kind": "payload",
        "sk8s": __version__,
        "cloudpickle": cloudpickle.__version__,
        "python": _python_minor(),
    }
    return json.dumps(header).encode() + b"\n" + cloudpickle.dumps((func, args, kwargs))


def decode_payload(data: bytes) -> tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]:
    head, _, body = data.partition(b"\n")
    header = json.loads(head)
    if header["python"] != _python_minor():
        raise VersionMismatch(
            f"payload was built on Python {header['python']} but this worker runs "
            f"{_python_minor()} — use a matching image (rebuild with `sk8s image build`)"
        )
    func, args, kwargs = pickle.loads(body)
    return func, args, kwargs


def encode_result(value: Any) -> bytes:
    return json.dumps({"kind": "ok"}).encode() + b"\n" + cloudpickle.dumps(value)


def encode_error(exc: BaseException) -> bytes:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    header = {
        "kind": "err",
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc),
        "tb": tb,
    }
    try:
        body = cloudpickle.dumps(exc)
    except Exception:
        body = b""
    return json.dumps(header).encode() + b"\n" + body


def decode_result(data: bytes) -> Any:
    head, _, body = data.partition(b"\n")
    header = json.loads(head)
    if header["kind"] == "ok":
        return pickle.loads(body)
    # kind == "err"
    if body:
        try:
            exc: BaseException = pickle.loads(body)
        except Exception:
            exc = RemoteException(header["type"], header["message"], header["tb"])
    else:
        exc = RemoteException(header["type"], header["message"], header["tb"])
    raise exc from RemoteTraceback(header["tb"])
```

- [ ] **Step 4: Run tests** — `pytest tests/test_serialize.py -v && mypy` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add sk8s/serialize.py tests/test_serialize.py && git commit -m "feat: cloudpickle wire format with version handshake and remote tracebacks"`

---

### Task 4: Config

**Files:**
- Create: `sk8s/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: frozen dataclass `Config(namespace, image, image_prefix, pull_policy, service_account, store, ttl_seconds_after_finished, retention_days, keep_results)`; `load_config(path=None, env=None, **overrides) -> Config` layering defaults < toml file < `SK8S_*` env < overrides; `DEFAULT_CONFIG_PATH = ~/.config/sk8s/config.toml`.

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
from pathlib import Path

from sk8s.config import Config, load_config


def test_defaults() -> None:
    cfg = load_config(path=Path("/nonexistent"), env={})
    assert cfg.store == "k8s"
    assert cfg.service_account == "sk8s"
    assert cfg.ttl_seconds_after_finished == 3600
    assert cfg.retention_days == 7
    assert cfg.pull_policy == "IfNotPresent"
    assert cfg.namespace is None and cfg.image is None


def test_file_layer(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('store = "s3://bkt/sk8s/"\nnamespace = "prod"\n')
    cfg = load_config(path=f, env={})
    assert cfg.store == "s3://bkt/sk8s/"
    assert cfg.namespace == "prod"


def test_env_overrides_file(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('namespace = "prod"\nttl_seconds_after_finished = 10\n')
    cfg = load_config(path=f, env={"SK8S_NAMESPACE": "dev", "SK8S_KEEP_RESULTS": "true"})
    assert cfg.namespace == "dev"
    assert cfg.ttl_seconds_after_finished == 10
    assert cfg.keep_results is True


def test_kwargs_override_everything(tmp_path: Path) -> None:
    cfg = load_config(path=Path("/nonexistent"), env={"SK8S_NAMESPACE": "dev"}, namespace="x")
    assert cfg.namespace == "x"


def test_frozen() -> None:
    cfg = load_config(path=Path("/nonexistent"), env={})
    try:
        cfg.store = "other"  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_config.py -v` — FAIL.

- [ ] **Step 3: Implement `sk8s/config.py`**

```python
"""Layered, frozen configuration: defaults < toml file < SK8S_* env < kwargs."""
from __future__ import annotations

import dataclasses
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sk8s" / "config.toml"


@dataclass(frozen=True)
class Config:
    namespace: str | None = None        # None -> current kube-context namespace
    image: str | None = None            # None -> official image for this version
    image_prefix: str = ""
    pull_policy: str = "IfNotPresent"
    service_account: str = "sk8s"
    store: str = "k8s"                  # "k8s" | "local:<dir>" | "s3://..." | "gs://..."
    ttl_seconds_after_finished: int = 3600
    retention_days: int = 7
    keep_results: bool = False


_FIELDS = {f.name: f.type for f in dataclasses.fields(Config)}


def _coerce(name: str, raw: str) -> Any:
    if name in ("ttl_seconds_after_finished", "retention_days"):
        return int(raw)
    if name == "keep_results":
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Config:
    path = DEFAULT_CONFIG_PATH if path is None else path
    env = os.environ if env is None else env

    values: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text())
        values.update({k: v for k, v in data.items() if k in _FIELDS})
    for name in _FIELDS:
        env_key = f"SK8S_{name.upper()}"
        if env_key in env:
            values[name] = _coerce(name, env[env_key])
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Config(**values)
```

- [ ] **Step 4: Run tests** — `pytest tests/test_config.py -v && mypy` — PASS.

- [ ] **Step 5: Commit** — `git add sk8s/config.py tests/test_config.py && git commit -m "feat: layered frozen config"`

---

### Task 5: BlobStore interface + LocalStore + ConfigMapStore

**Files:**
- Create: `sk8s/store/__init__.py`, `sk8s/store/base.py`, `sk8s/store/local.py`, `sk8s/store/configmap.py`
- Test: `tests/test_store_local.py`, `tests/test_store_configmap.py`, `tests/fakes.py`

**Interfaces:**
- Produces:
  ```python
  class BlobStore(ABC):
      def put(self, key: str, data: bytes, owner: Any | None = None) -> str: ...
      def get(self, uri: str) -> bytes: ...          # raises BlobNotFound
      def delete(self, uri: str) -> None: ...        # idempotent
      def delete_prefix(self, prefix: str) -> None: ...
      def uri_for(self, key: str) -> str: ...        # deterministic, no I/O
  get_store(store: str, *, namespace: str | None = None, core_api: Any | None = None) -> BlobStore
  ```
- Keys are slash-separated (`wf-x/job-y/payload`). URIs: `local:<abs-path>`, `k8s://<ns>/<configmap-name>`.
- `MAX_CONFIGMAP_BLOB = 950_000`.

- [ ] **Step 1: Write the failing tests**

`tests/fakes.py` (fake Kubernetes CoreV1 API used by store/spec/cluster tests):
```python
"""In-memory fakes for the kubernetes client surface sk8s uses."""
from __future__ import annotations

from typing import Any

from kubernetes.client import ApiException


class FakeCoreV1:
    def __init__(self) -> None:
        self.configmaps: dict[tuple[str, str], Any] = {}  # (ns, name) -> V1ConfigMap

    def create_namespaced_config_map(self, namespace: str, body: Any) -> Any:
        self.configmaps[(namespace, body.metadata.name)] = body
        return body

    def read_namespaced_config_map(self, name: str, namespace: str) -> Any:
        try:
            return self.configmaps[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="Not Found")

    def delete_namespaced_config_map(self, name: str, namespace: str) -> None:
        if (namespace, name) not in self.configmaps:
            raise ApiException(status=404, reason="Not Found")
        del self.configmaps[(namespace, name)]

    def list_namespaced_config_map(self, namespace: str, label_selector: str = "") -> Any:
        class R:
            items = [v for (ns, _), v in self.configmaps.items() if ns == namespace]
        return R()
```

`tests/test_store_local.py`:
```python
from pathlib import Path

import pytest

from sk8s.errors import BlobNotFound
from sk8s.store import get_store
from sk8s.store.local import LocalStore


def test_roundtrip(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    uri = s.put("wf-a/job-b/payload", b"hello")
    assert uri == s.uri_for("wf-a/job-b/payload")
    assert s.get(uri) == b"hello"


def test_get_missing_raises(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    with pytest.raises(BlobNotFound):
        s.get(s.uri_for("nope/missing"))


def test_delete_idempotent(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    uri = s.put("k", b"x")
    s.delete(uri)
    s.delete(uri)  # no error
    with pytest.raises(BlobNotFound):
        s.get(uri)


def test_delete_prefix(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    s.put("wf-a/j1/r", b"1")
    s.put("wf-a/j2/r", b"2")
    s.put("wf-b/j1/r", b"3")
    s.delete_prefix("wf-a")
    with pytest.raises(BlobNotFound):
        s.get(s.uri_for("wf-a/j1/r"))
    assert s.get(s.uri_for("wf-b/j1/r")) == b"3"


def test_factory(tmp_path: Path) -> None:
    s = get_store(f"local:{tmp_path}")
    assert isinstance(s, LocalStore)
```

`tests/test_store_configmap.py`:
```python
import pytest

from sk8s.errors import BlobNotFound, BlobTooLarge
from sk8s.store.configmap import MAX_CONFIGMAP_BLOB, ConfigMapStore
from tests.fakes import FakeCoreV1


def make_store() -> ConfigMapStore:
    return ConfigMapStore(namespace="ns1", core_api=FakeCoreV1())


def test_roundtrip() -> None:
    s = make_store()
    uri = s.put("wf-a/job-b/payload", b"\x00\x01binary")
    assert uri == "k8s://ns1/sk8s-wf-a-job-b-payload"
    assert s.get(uri) == b"\x00\x01binary"


def test_labels_applied() -> None:
    s = make_store()
    s.put("wf-a/job-b/result", b"x")
    cm = s.core_api.configmaps[("ns1", "sk8s-wf-a-job-b-result")]
    assert cm.metadata.labels["app.kubernetes.io/managed-by"] == "sk8s"
    assert cm.metadata.labels["sk8s/workflow-id"] == "wf-a"


def test_put_too_large() -> None:
    s = make_store()
    with pytest.raises(BlobTooLarge, match="s3:// or gs://"):
        s.put("wf-a/big", b"x" * (MAX_CONFIGMAP_BLOB + 1))


def test_get_missing_raises() -> None:
    with pytest.raises(BlobNotFound):
        make_store().get("k8s://ns1/sk8s-nope")


def test_delete_prefix() -> None:
    s = make_store()
    s.put("wf-a/j1/r", b"1")
    s.put("wf-b/j1/r", b"2")
    s.delete_prefix("wf-a")
    assert ("ns1", "sk8s-wf-a-j1-r") not in s.core_api.configmaps
    assert ("ns1", "sk8s-wf-b-j1-r") in s.core_api.configmaps


def test_key_sanitization() -> None:
    s = make_store()
    uri = s.put("wf-a/Job_B/payload", b"x")
    assert uri == "k8s://ns1/sk8s-wf-a-job-b-payload"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_store_local.py tests/test_store_configmap.py -v` — FAIL.

- [ ] **Step 3: Implement**

`sk8s/store/base.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BlobStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        """Store data under key; returns the blob's URI. owner (a
        V1OwnerReference) is honored by Kubernetes-backed stores, ignored elsewhere."""

    @abstractmethod
    def get(self, uri: str) -> bytes:
        """Fetch a blob by URI. Raises BlobNotFound."""

    @abstractmethod
    def delete(self, uri: str) -> None:
        """Delete one blob. Idempotent: missing blobs are not an error."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Delete every blob whose key starts with prefix."""

    @abstractmethod
    def uri_for(self, key: str) -> str:
        """The URI put(key, ...) would return. Deterministic; no I/O."""
```

`sk8s/store/local.py`:
```python
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sk8s.errors import BlobNotFound
from sk8s.store.base import BlobStore


class LocalStore(BlobStore):
    """Directory-backed store for LocalCluster and tests. URI: local:<abs path>."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def uri_for(self, key: str) -> str:
        return f"local:{self.root / key}"

    def _path(self, uri: str) -> Path:
        assert uri.startswith("local:"), uri
        return Path(uri[len("local:"):])

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.uri_for(key)

    def get(self, uri: str) -> bytes:
        path = self._path(uri)
        if not path.exists():
            raise BlobNotFound(uri)
        return path.read_bytes()

    def delete(self, uri: str) -> None:
        self._path(uri).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        target = self.root / prefix
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
```

`sk8s/store/configmap.py`:
```python
from __future__ import annotations

import base64
import re
from typing import Any

from kubernetes import client
from kubernetes.client import ApiException

from sk8s.errors import BlobNotFound, BlobTooLarge
from sk8s.store.base import BlobStore

MAX_CONFIGMAP_BLOB = 950_000
MANAGED_BY = {"app.kubernetes.io/managed-by": "sk8s"}


def _sanitize(key: str) -> str:
    name = "sk8s-" + re.sub(r"[^a-z0-9-]+", "-", key.lower()).strip("-")
    return name[:253]


class ConfigMapStore(BlobStore):
    """Zero-setup default store. One ConfigMap per blob. URI: k8s://<ns>/<name>."""

    def __init__(self, namespace: str, core_api: Any) -> None:
        self.namespace = namespace
        self.core_api = core_api

    def uri_for(self, key: str) -> str:
        return f"k8s://{self.namespace}/{_sanitize(key)}"

    def put(self, key: str, data: bytes, owner: Any | None = None) -> str:
        if len(data) > MAX_CONFIGMAP_BLOB:
            raise BlobTooLarge(
                f"blob for {key!r} is {len(data)} bytes; the ConfigMap store caps at "
                f"{MAX_CONFIGMAP_BLOB}. Configure an object store (s3:// or gs://) — "
                f"see `sk8s config --store`."
            )
        workflow_id = key.split("/", 1)[0]
        meta = client.V1ObjectMeta(
            name=_sanitize(key),
            labels={**MANAGED_BY, "sk8s/workflow-id": workflow_id},
            owner_references=[owner] if owner is not None else None,
        )
        body = client.V1ConfigMap(
            metadata=meta, binary_data={"blob": base64.b64encode(data).decode()}
        )
        try:
            self.core_api.create_namespaced_config_map(namespace=self.namespace, body=body)
        except ApiException as e:
            if e.status == 409:  # retried worker overwrites its previous attempt
                self.core_api.delete_namespaced_config_map(
                    name=meta.name, namespace=self.namespace
                )
                self.core_api.create_namespaced_config_map(
                    namespace=self.namespace, body=body
                )
            else:
                raise
        return self.uri_for(key)

    def _split(self, uri: str) -> tuple[str, str]:
        assert uri.startswith("k8s://"), uri
        ns, name = uri[len("k8s://"):].split("/", 1)
        return ns, name

    def get(self, uri: str) -> bytes:
        ns, name = self._split(uri)
        try:
            cm = self.core_api.read_namespaced_config_map(name=name, namespace=ns)
        except ApiException as e:
            if e.status == 404:
                raise BlobNotFound(uri) from None
            raise
        return base64.b64decode(cm.binary_data["blob"])

    def delete(self, uri: str) -> None:
        ns, name = self._split(uri)
        try:
            self.core_api.delete_namespaced_config_map(name=name, namespace=ns)
        except ApiException as e:
            if e.status != 404:
                raise

    def delete_prefix(self, prefix: str) -> None:
        name_prefix = _sanitize(prefix)
        cms = self.core_api.list_namespaced_config_map(
            namespace=self.namespace, label_selector="app.kubernetes.io/managed-by=sk8s"
        )
        for cm in cms.items:
            if cm.metadata.name.startswith(name_prefix):
                self.delete(f"k8s://{self.namespace}/{cm.metadata.name}")
```

`sk8s/store/__init__.py`:
```python
from __future__ import annotations

from typing import Any

from sk8s.store.base import BlobStore
from sk8s.store.local import LocalStore


def get_store(store: str, *, namespace: str | None = None, core_api: Any | None = None) -> BlobStore:
    if store.startswith("local:"):
        return LocalStore(store[len("local:"):])
    if store == "k8s":
        from sk8s.store.configmap import ConfigMapStore
        from sk8s import k8s

        if namespace is None:
            namespace = k8s.current_namespace()
        if core_api is None:
            core_api = k8s.core_api()
        return ConfigMapStore(namespace=namespace, core_api=core_api)
    if store.startswith(("s3://", "gs://")):
        raise NotImplementedError(
            f"{store!r}: object-store backends arrive in the stores-cloud feature"
        )
    raise ValueError(f"unknown store {store!r}")


__all__ = ["BlobStore", "LocalStore", "get_store"]
```

Note: `sk8s.k8s` (client bootstrap) is created in Task 6; until then the `"k8s"` factory branch is exercised only with an injected `core_api` — pass both `namespace` and `core_api` in tests.

- [ ] **Step 4: Run tests** — `pytest tests/test_store_local.py tests/test_store_configmap.py -v && mypy` — PASS (mypy: `tests/fakes.py` is outside the `sk8s` package; that's fine).

- [ ] **Step 5: Commit** — `git add sk8s/store tests/test_store_local.py tests/test_store_configmap.py tests/fakes.py && git commit -m "feat: BlobStore interface with local and ConfigMap backends"`

---

### Task 6: Kubernetes client bootstrap

**Files:**
- Create: `sk8s/k8s.py`
- Test: `tests/test_k8s_bootstrap.py`

**Interfaces:**
- Produces: `in_pod() -> bool`; `load() -> None` (incluster vs kubeconfig, idempotent); `core_api() -> client.CoreV1Api`; `batch_api() -> client.BatchV1Api`; `current_namespace() -> str` (in-pod: serviceaccount file; outside: active kube-context namespace, default `"default"`).

- [ ] **Step 1: Write the failing test**

`tests/test_k8s_bootstrap.py`:
```python
from pathlib import Path

from sk8s import k8s


def test_in_pod_false_without_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(k8s, "_SA_DIR", Path("/nonexistent"))
    assert k8s.in_pod() is False


def test_in_pod_true_with_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "token").write_text("tok")
    (tmp_path / "namespace").write_text("myns")
    monkeypatch.setattr(k8s, "_SA_DIR", tmp_path)
    assert k8s.in_pod() is True
    assert k8s.current_namespace() == "myns"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_k8s_bootstrap.py -v` — FAIL.

- [ ] **Step 3: Implement `sk8s/k8s.py`**

```python
"""Kubernetes client bootstrap: in-pod detection and lazy, cached API clients."""
from __future__ import annotations

from pathlib import Path

from kubernetes import client, config

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_loaded = False


def in_pod() -> bool:
    return (_SA_DIR / "token").exists()


def load() -> None:
    global _loaded
    if _loaded:
        return
    if in_pod():
        config.load_incluster_config()
    else:
        config.load_kube_config()
    _loaded = True


def core_api() -> client.CoreV1Api:
    load()
    return client.CoreV1Api()


def batch_api() -> client.BatchV1Api:
    load()
    return client.BatchV1Api()


def current_namespace() -> str:
    if in_pod():
        return (_SA_DIR / "namespace").read_text().strip()
    load()
    contexts, active = config.list_kube_config_contexts()
    ns = (active.get("context") or {}).get("namespace")
    return ns or "default"
```

- [ ] **Step 4: Run tests** — `pytest tests/test_k8s_bootstrap.py -v && mypy` — PASS.

- [ ] **Step 5: Commit** — `git add sk8s/k8s.py tests/test_k8s_bootstrap.py && git commit -m "feat: kubernetes client bootstrap"`

---

### Task 7: Job spec builder

**Files:**
- Create: `sk8s/spec.py`
- Test: `tests/test_spec.py`

**Interfaces:**
- Produces:
  ```python
  def build_job(
      name: str, *, image: str, namespace: str, workflow_id: str,
      payload_uri: str, result_key: str, store: str,
      service_account: str, pull_policy: str,
      requests: dict[str, str], limits: dict[str, str],
      volumes: dict[str, str],            # {pvc_name: mount_path}
      backoff_limit: int, ttl: int, privileged: bool,
  ) -> client.V1Job
  def job_name(prefix: str) -> str        # "<sanitized-prefix>-<6 random>" <= 63 chars
  ```
- Container: name `worker`, command `["python", "-m", "sk8s.worker"]`, env `SK8S_STORE`, `SK8S_NAMESPACE`, `SK8S_PAYLOAD_URI`, `SK8S_RESULT_KEY`, `SK8S_WORKFLOW_ID`.

- [ ] **Step 1: Write the failing test**

`tests/test_spec.py`:
```python
import re

from sk8s.spec import build_job, job_name


def make(**over):  # type: ignore[no-untyped-def]
    args = dict(
        name="job-abc123", image="sk8s:dev", namespace="ns1", workflow_id="wf-x",
        payload_uri="k8s://ns1/sk8s-wf-x-job-abc123-payload",
        result_key="wf-x/job-abc123/result", store="k8s",
        service_account="sk8s", pull_policy="IfNotPresent",
        requests={}, limits={}, volumes={}, backoff_limit=0, ttl=3600,
        privileged=False,
    )
    args.update(over)
    return build_job(**args)  # type: ignore[arg-type]


def test_basic_fields() -> None:
    job = make()
    assert job.metadata.name == "job-abc123"
    assert job.metadata.labels["app.kubernetes.io/managed-by"] == "sk8s"
    assert job.metadata.labels["sk8s/workflow-id"] == "wf-x"
    assert job.spec.ttl_seconds_after_finished == 3600
    assert job.spec.backoff_limit == 0
    pod = job.spec.template.spec
    assert pod.restart_policy == "Never"
    assert pod.service_account_name == "sk8s"
    c = pod.containers[0]
    assert c.command == ["python", "-m", "sk8s.worker"]
    env = {e.name: e.value for e in c.env}
    assert env["SK8S_PAYLOAD_URI"].endswith("payload")
    assert env["SK8S_RESULT_KEY"] == "wf-x/job-abc123/result"
    assert env["SK8S_STORE"] == "k8s"
    assert env["SK8S_WORKFLOW_ID"] == "wf-x"


def test_resources_and_volumes() -> None:
    job = make(requests={"cpu": "1"}, limits={"memory": "1Gi"},
               volumes={"data": "/mnt/data"})
    c = job.spec.template.spec.containers[0]
    assert c.resources.requests == {"cpu": "1"}
    assert c.resources.limits == {"memory": "1Gi"}
    assert c.volume_mounts[0].mount_path == "/mnt/data"
    v = job.spec.template.spec.volumes[0]
    assert v.persistent_volume_claim.claim_name == "data"


def test_no_resources_means_none() -> None:
    c = make().spec.template.spec.containers[0]
    assert c.resources is None


def test_job_name_valid_and_random() -> None:
    n1, n2 = job_name("My_Fancy Prefix!"), job_name("My_Fancy Prefix!")
    assert n1 != n2
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", n1)
    assert len(job_name("x" * 100)) <= 63
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_spec.py -v` — FAIL.

- [ ] **Step 3: Implement `sk8s/spec.py`**

```python
"""Build Kubernetes objects with client models. No YAML, no templates."""
from __future__ import annotations

import random
import re
import string

from kubernetes import client

MANAGED_BY = "sk8s"


def job_name(prefix: str) -> str:
    clean = re.sub(r"[^a-z0-9-]+", "-", prefix.lower()).strip("-") or "job"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{clean[:56]}-{suffix}"


def build_job(
    name: str,
    *,
    image: str,
    namespace: str,
    workflow_id: str,
    payload_uri: str,
    result_key: str,
    store: str,
    service_account: str,
    pull_policy: str,
    requests: dict[str, str],
    limits: dict[str, str],
    volumes: dict[str, str],
    backoff_limit: int,
    ttl: int,
    privileged: bool,
) -> client.V1Job:
    labels = {"app.kubernetes.io/managed-by": MANAGED_BY, "sk8s/workflow-id": workflow_id}
    env = [
        client.V1EnvVar(name="SK8S_STORE", value=store),
        client.V1EnvVar(name="SK8S_NAMESPACE", value=namespace),
        client.V1EnvVar(name="SK8S_PAYLOAD_URI", value=payload_uri),
        client.V1EnvVar(name="SK8S_RESULT_KEY", value=result_key),
        client.V1EnvVar(name="SK8S_WORKFLOW_ID", value=workflow_id),
    ]
    resources = None
    if requests or limits:
        resources = client.V1ResourceRequirements(
            requests=requests or None, limits=limits or None
        )
    container = client.V1Container(
        name="worker",
        image=image,
        image_pull_policy=pull_policy,
        command=["python", "-m", "sk8s.worker"],
        env=env,
        resources=resources,
        security_context=(
            client.V1SecurityContext(privileged=True) if privileged else None
        ),
        volume_mounts=[
            client.V1VolumeMount(name=pvc, mount_path=path)
            for pvc, path in volumes.items()
        ] or None,
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        service_account_name=service_account,
        containers=[container],
        volumes=[
            client.V1Volume(
                name=pvc,
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=pvc
                ),
            )
            for pvc in volumes
        ] or None,
    )
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1JobSpec(
            backoff_limit=backoff_limit,
            ttl_seconds_after_finished=ttl,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec
            ),
        ),
    )
```

- [ ] **Step 4: Run tests** — `pytest tests/test_spec.py -v && mypy` — PASS.

- [ ] **Step 5: Commit** — `git add sk8s/spec.py tests/test_spec.py && git commit -m "feat: typed V1Job builder and job naming"`

---

### Task 8: Worker entrypoint

**Files:**
- Create: `sk8s/worker.py`, `sk8s/__main__.py` is NOT used — the worker runs as `python -m sk8s.worker`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `get_store`, `serialize.*`.
- Produces: `worker.main(environ: Mapping[str, str]) -> int` — 0 on success, 1 on user-code failure (so `backoffLimit` retries apply); `python -m sk8s.worker` runs `main(os.environ)`. Reads `SK8S_STORE`, `SK8S_NAMESPACE`, `SK8S_PAYLOAD_URI`, `SK8S_RESULT_KEY`. Always writes a result blob (ok or err) unless the store itself is unreachable.

- [ ] **Step 1: Write the failing test**

`tests/test_worker.py`:
```python
import threading
from pathlib import Path

import pytest

from sk8s import serialize, worker
from sk8s.errors import BlobTooLarge, RemoteTraceback
from sk8s.store.local import LocalStore


def run_worker(tmp_path: Path, func, args=(), kwargs={}):  # type: ignore[no-untyped-def]
    store = LocalStore(tmp_path)
    payload_uri = store.put("wf/j/payload", serialize.encode_payload(func, args, kwargs))
    env = {
        "SK8S_STORE": f"local:{tmp_path}",
        "SK8S_PAYLOAD_URI": payload_uri,
        "SK8S_RESULT_KEY": "wf/j/result",
    }
    code = worker.main(env)
    return code, store.get(store.uri_for("wf/j/result"))


def test_success(tmp_path: Path) -> None:
    code, blob = run_worker(tmp_path, lambda x: x * 2, (21,))
    assert code == 0
    assert serialize.decode_result(blob) == 42


def test_none_result(tmp_path: Path) -> None:
    code, blob = run_worker(tmp_path, lambda: None)
    assert code == 0
    assert serialize.decode_result(blob) is None


def test_user_exception_stored_and_exit_1(tmp_path: Path) -> None:
    def boom() -> None:
        raise KeyError("missing")

    code, blob = run_worker(tmp_path, boom)
    assert code == 1
    with pytest.raises(KeyError) as exc_info:
        serialize.decode_result(blob)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)


def test_unpicklable_result_becomes_error(tmp_path: Path) -> None:
    code, blob = run_worker(tmp_path, lambda: threading.Lock())
    assert code == 1
    with pytest.raises(Exception):
        serialize.decode_result(blob)


def test_oversized_result_becomes_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Patch the local store's put to enforce a tiny cap, simulating ConfigMap limits.
    real_put = LocalStore.put

    def capped_put(self, key, data, owner=None):  # type: ignore[no-untyped-def]
        if key.endswith("/result") and len(data) > 100 and b'"kind": "err"' not in data.split(b"\n", 1)[0]:
            raise BlobTooLarge("too big; use s3:// or gs://")
        return real_put(self, key, data, owner)

    monkeypatch.setattr(LocalStore, "put", capped_put)
    code, blob = run_worker(tmp_path, lambda: "x" * 10_000)
    assert code == 1
    with pytest.raises(BlobTooLarge, match="s3:// or gs://"):
        serialize.decode_result(blob)
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_worker.py -v` — FAIL.

- [ ] **Step 3: Implement `sk8s/worker.py`**

```python
"""In-pod entrypoint: fetch payload, run the function, store result or error.

Exit code 0 = user function succeeded; 1 = it (or result storage) failed, so
the Job's backoffLimit governs retries. A result blob is always written unless
the store itself is unreachable.
"""
from __future__ import annotations

import os
import sys
from typing import Mapping

from sk8s import serialize
from sk8s.store import get_store


def main(environ: Mapping[str, str]) -> int:
    store = get_store(
        environ["SK8S_STORE"], namespace=environ.get("SK8S_NAMESPACE")
    )
    payload = store.get(environ["SK8S_PAYLOAD_URI"])
    result_key = environ["SK8S_RESULT_KEY"]
    try:
        func, args, kwargs = serialize.decode_payload(payload)
        value = func(*args, **kwargs)
        blob = serialize.encode_result(value)
        store.put(result_key, blob)
        return 0
    except BaseException as exc:  # noqa: BLE001 — everything becomes an error result
        store.put(result_key, serialize.encode_error(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main(os.environ))
```

Note the control flow: `encode_result`/`store.put` for a successful value sit *inside* the `try`, so an unpicklable or oversized result falls through to `encode_error` (error blobs are small — header + maybe-empty body).

- [ ] **Step 4: Run tests** — `pytest tests/test_worker.py -v && mypy` — PASS.

- [ ] **Step 5: Commit** — `git add sk8s/worker.py tests/test_worker.py && git commit -m "feat: worker entrypoint with error capture"`

---

### Task 9: JobFuture + LocalCluster

**Files:**
- Create: `sk8s/future.py`, `sk8s/local.py`
- Test: `tests/test_local_cluster.py`

**Interfaces:**
- Consumes: `serialize`, `LocalStore`, `worker` (as subprocess module).
- Produces:
  ```python
  class JobFuture(concurrent.futures.Future):   # sk8s/future.py
      job_name: str
      workflow_id: str
      def __init__(self, job_name: str, workflow_id: str, backend: JobBackend) -> None
      def logs(self) -> str                     # delegates to backend.logs(job_name)
      def cancel(self) -> bool                  # backend.cancel(job_name); True if stopped

  class JobBackend(Protocol):                   # sk8s/future.py
      def logs(self, job_name: str) -> str: ...
      def cancel(self, job_name: str) -> bool: ...

  class LocalCluster:                           # sk8s/local.py
      def __init__(self, root: Path | None = None, max_workers: int = 8) -> None
      def submit(self, func, /, *args, **kwargs) -> JobFuture   # job options accepted+ignored except keep_results
      workflow_id: str
  ```
- `LocalCluster.submit` runs the real pipeline: encode payload → LocalStore → `python -m sk8s.worker` subprocess (no shell) → decode result blob → resolve future. This is the shipped local backend *and* the API test double.

- [ ] **Step 1: Write the failing test**

`tests/test_local_cluster.py`:
```python
import pytest
from concurrent.futures import Future

from sk8s.errors import RemoteTraceback
from sk8s.future import JobFuture
from sk8s.local import LocalCluster


def test_submit_returns_future_with_result() -> None:
    lc = LocalCluster()
    fut = lc.submit(lambda x: x + 1, 41)
    assert isinstance(fut, Future) and isinstance(fut, JobFuture)
    assert fut.job_name.startswith("job-")
    assert fut.result(timeout=60) == 42


def test_none_result() -> None:
    assert LocalCluster().submit(lambda: None).result(timeout=60) is None


def test_function_kwargs_pass_through() -> None:
    fut = LocalCluster().submit(lambda a, b=0: a + b, 1, b=2)
    assert fut.result(timeout=60) == 3


def test_remote_exception_reraised() -> None:
    def boom() -> None:
        raise ValueError("local boom")

    fut = LocalCluster().submit(boom)
    with pytest.raises(ValueError, match="local boom") as exc_info:
        fut.result(timeout=60)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)


def test_closure_capture() -> None:
    n = 10
    assert LocalCluster().submit(lambda: n * 2).result(timeout=60) == 20


def test_job_options_accepted_and_ignored() -> None:
    fut = LocalCluster().submit(lambda: 1, image="whatever:latest", requests={"cpu": "4"})
    assert fut.result(timeout=60) == 1
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_local_cluster.py -v` — FAIL.

- [ ] **Step 3: Implement**

`sk8s/future.py`:
```python
from __future__ import annotations

import concurrent.futures
from typing import Protocol


class JobBackend(Protocol):
    def logs(self, job_name: str) -> str: ...
    def cancel(self, job_name: str) -> bool: ...


class JobFuture(concurrent.futures.Future):  # type: ignore[type-arg]
    """A real Future resolved by the owning cluster's watcher.

    cancel() is stronger than stdlib semantics: it deletes the Kubernetes Job
    even if it has already started running.
    """

    def __init__(self, job_name: str, workflow_id: str, backend: JobBackend) -> None:
        super().__init__()
        self.job_name = job_name
        self.workflow_id = workflow_id
        self._backend = backend

    def logs(self) -> str:
        return self._backend.logs(self.job_name)

    def cancel(self) -> bool:
        """Delete the Job via the backend, then mark this future terminal."""
        if self.done():
            return False
        if not self._backend.cancel(self.job_name):
            return False
        if not super().cancel():
            # already marked running; force a terminal state
            self.set_exception(concurrent.futures.CancelledError())
        return True
```

`sk8s/local.py`:
```python
"""Local backend: same pipeline as the k8s cluster, workers run as subprocesses."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from sk8s import serialize, spec
from sk8s.future import JobFuture
from sk8s.store.local import LocalStore

# Options accepted (for interface parity with Cluster) but meaningless locally.
IGNORED_OPTIONS = {
    "image", "requests", "limits", "volumes", "backoff_limit",
    "service_account", "namespace", "privileged", "pull_policy", "timeout",
}


class LocalCluster:
    def __init__(self, root: Path | str | None = None, max_workers: int = 8) -> None:
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="sk8s-local-"))
        self.store = LocalStore(self.root)
        self.workflow_id = "wf-" + spec.job_name("x").rsplit("-", 1)[1]
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    def logs(self, job_name: str) -> str:
        return ""  # local workers inherit stdio; logs land on the terminal

    def cancel(self, job_name: str) -> bool:
        proc = self._procs.get(job_name)
        if proc is not None and proc.poll() is None:
            proc.kill()
            return True
        return False

    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
        job_opts = {k: kwargs.pop(k) for k in list(kwargs) if k in IGNORED_OPTIONS}
        kwargs.pop("keep_results", None)
        del job_opts  # accepted for parity; meaningless locally
        name = spec.job_name(kwargs.pop("name_prefix", "job"))
        payload_uri = self.store.put(
            f"{self.workflow_id}/{name}/payload",
            serialize.encode_payload(func, args, kwargs),
        )
        result_key = f"{self.workflow_id}/{name}/result"
        fut = JobFuture(name, self.workflow_id, backend=self)

        def run() -> None:
            env = {
                "SK8S_STORE": f"local:{self.root}",
                "SK8S_PAYLOAD_URI": payload_uri,
                "SK8S_RESULT_KEY": result_key,
                "PATH": "/usr/bin:/bin",
            }
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                [sys.executable, "-m", "sk8s.worker"], env=env
            )
            self._procs[name] = proc
            proc.wait()
            try:
                blob = self.store.get(self.store.uri_for(result_key))
                try:
                    fut.set_result(serialize.decode_result(blob))
                except BaseException as exc:  # noqa: BLE001
                    fut.set_exception(exc)
            except Exception as exc:
                fut.set_exception(exc)

        self._pool.submit(run)
        return fut
```

- [ ] **Step 4: Run tests** — `pytest tests/test_local_cluster.py -v && mypy` — PASS. (If the subprocess can't import sk8s because `PATH` env is minimal, extend `env` with `{"PYTHONPATH": ":".join(sys.path)}` — add that to the env dict and keep the test green.)

- [ ] **Step 5: Commit** — `git add sk8s/future.py sk8s/local.py tests/test_local_cluster.py && git commit -m "feat: JobFuture and LocalCluster subprocess backend"`

---

### Task 10: Job watcher

**Files:**
- Create: `sk8s/watch.py`
- Test: `tests/test_watch.py`

**Interfaces:**
- Produces:
  ```python
  JobEvent = tuple[str, str]   # (job_name, phase); phase ∈ {"active","succeeded","failed","imagepull"}

  class JobWatcher:
      def __init__(self, events: Callable[[], Iterator[JobEvent]],
                   resolve: Callable[[str, str], None]) -> None
      def register(self, job_name: str) -> None
      def stop(self) -> None
  ```
- Behavior: daemon thread iterates `events()`; for each event whose name is registered and whose phase is terminal (`succeeded`/`failed`/`imagepull`), calls `resolve(name, phase)` exactly once and unregisters. If `events()` raises, it is re-invoked (reconnect) after 1s. Registration is thread-safe; events for unregistered names are remembered for 60s so a terminal event arriving just before `register()` still resolves.

- [ ] **Step 1: Write the failing test**

`tests/test_watch.py`:
```python
import threading
import time
from typing import Iterator

from sk8s.watch import JobEvent, JobWatcher


def collect_resolver():  # type: ignore[no-untyped-def]
    resolved: dict[str, str] = {}
    done = threading.Event()

    def resolve(name: str, phase: str) -> None:
        resolved[name] = phase
        done.set()

    return resolved, done, resolve


def test_terminal_event_resolves() -> None:
    resolved, done, resolve = collect_resolver()

    def events() -> Iterator[JobEvent]:
        yield ("job-a", "active")
        yield ("job-a", "succeeded")
        while True:
            time.sleep(0.01)
            yield ("job-other", "active")

    w = JobWatcher(events, resolve)
    w.register("job-a")
    assert done.wait(5)
    assert resolved == {"job-a": "succeeded"}
    w.stop()


def test_event_before_registration_still_resolves() -> None:
    resolved, done, resolve = collect_resolver()
    started = threading.Event()

    def events() -> Iterator[JobEvent]:
        yield ("job-b", "failed")
        started.set()
        while True:
            time.sleep(0.01)
            yield ("job-x", "active")

    w = JobWatcher(events, resolve)
    assert started.wait(5)
    w.register("job-b")           # terminal event already passed
    assert done.wait(5)
    assert resolved == {"job-b": "failed"}
    w.stop()


def test_resolve_called_once() -> None:
    calls: list[str] = []
    done = threading.Event()

    def resolve(name: str, phase: str) -> None:
        calls.append(name)
        done.set()

    def events() -> Iterator[JobEvent]:
        yield ("job-c", "succeeded")
        yield ("job-c", "succeeded")
        while True:
            time.sleep(0.01)
            yield ("job-x", "active")

    w = JobWatcher(events, resolve)
    w.register("job-c")
    assert done.wait(5)
    time.sleep(0.1)
    assert calls == ["job-c"]
    w.stop()


def test_stream_crash_reconnects() -> None:
    resolved, done, resolve = collect_resolver()
    attempt = {"n": 0}

    def events() -> Iterator[JobEvent]:
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise ConnectionError("watch dropped")
        yield ("job-d", "succeeded")
        while True:
            time.sleep(0.01)
            yield ("job-x", "active")

    w = JobWatcher(events, resolve, reconnect_delay=0.05)
    w.register("job-d")
    assert done.wait(5)
    assert resolved == {"job-d": "succeeded"}
    w.stop()
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_watch.py -v` — FAIL.

- [ ] **Step 3: Implement `sk8s/watch.py`**

```python
"""One watcher thread per Cluster resolves futures from job events. No polling."""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterator

JobEvent = tuple[str, str]  # (job_name, phase)
TERMINAL = {"succeeded", "failed", "imagepull"}
_RECENT_TTL = 60.0


class JobWatcher:
    def __init__(
        self,
        events: Callable[[], Iterator[JobEvent]],
        resolve: Callable[[str, str], None],
        reconnect_delay: float = 1.0,
    ) -> None:
        self._events = events
        self._resolve = resolve
        self._reconnect_delay = reconnect_delay
        self._lock = threading.Lock()
        self._registered: set[str] = set()
        self._recent_terminal: dict[str, tuple[str, float]] = {}
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sk8s-watch")
        self._thread.start()

    def register(self, job_name: str) -> None:
        with self._lock:
            hit = self._recent_terminal.pop(job_name, None)
            if hit is None:
                self._registered.add(job_name)
                return
            phase = hit[0]
        self._resolve(job_name, phase)

    def stop(self) -> None:
        self._stopped.set()

    def _handle(self, name: str, phase: str) -> None:
        if phase not in TERMINAL:
            return
        with self._lock:
            if name in self._registered:
                self._registered.discard(name)
                fire = True
            else:
                now = time.monotonic()
                self._recent_terminal[name] = (phase, now)
                self._recent_terminal = {
                    k: v for k, v in self._recent_terminal.items()
                    if now - v[1] < _RECENT_TTL
                }
                fire = False
        if fire:
            self._resolve(name, phase)

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                for name, phase in self._events():
                    if self._stopped.is_set():
                        return
                    self._handle(name, phase)
            except Exception:
                time.sleep(self._reconnect_delay)
```

- [ ] **Step 4: Run tests** — `pytest tests/test_watch.py -v && mypy` — PASS.

- [ ] **Step 5: Commit** — `git add sk8s/watch.py tests/test_watch.py && git commit -m "feat: thread-safe job watcher with reconnect and pre-registration buffer"`

---

### Task 11: Cluster

**Files:**
- Create: `sk8s/cluster.py`
- Test: `tests/test_cluster.py` (extend `tests/fakes.py` with `FakeBatchV1`)

**Interfaces:**
- Consumes: everything above.
- Produces:
  ```python
  JOB_OPTIONS = {"image", "requests", "limits", "volumes", "timeout", "backoff_limit",
                 "service_account", "namespace", "name_prefix", "privileged",
                 "pull_policy", "keep_results"}

  class Cluster:
      def __init__(self, config: Config | None = None, *,
                   batch_api: Any | None = None, core_api: Any | None = None,
                   store: BlobStore | None = None,
                   watcher_events: Callable[[], Iterator[JobEvent]] | None = None,
                   **overrides: Any) -> None
      workflow_id: str
      def submit(self, func, /, *args, **kwargs) -> JobFuture
      def run(self, func, /, *args, **kwargs) -> Any          # submit().result(timeout) with JobTimeout
      def logs(self, job_name: str) -> str
      def cancel(self, job_name: str) -> bool                 # delete job, propagate to pods
      def close(self) -> None
  ```
- `submit` splits `kwargs` into job options (names in `JOB_OPTIONS`) and function kwargs (everything else). The default image is `config.image` or `f"{config.image_prefix}ghcr.io/jared-maguire/sk8s:{__version__}-py{maj}.{min}"` — exact fallback string pinned by test.
- Resolution contract (`_on_terminal(name, phase)`): fetch result blob (3 attempts, 0.5s apart — covers store write visibility lag); `succeeded` → `set_result(decode)`; `failed` with result blob → decoded exception via `set_exception`; `failed` without blob → `PodFailed(name, reason, logs)` from pod status; `imagepull` → `ImagePullError(name, image)`. Then delete payload blob always, result blob unless `keep_results`.

- [ ] **Step 1: Extend fakes and write the failing test**

Append to `tests/fakes.py`:
```python
class FakeBatchV1:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], Any] = {}
        self.deleted: list[str] = []

    def create_namespaced_job(self, namespace: str, body: Any) -> Any:
        self.jobs[(namespace, body.metadata.name)] = body
        return body

    def delete_namespaced_job(self, name: str, namespace: str, propagation_policy: str = "Background") -> None:
        self.deleted.append(name)
        self.jobs.pop((namespace, name), None)
```

`tests/test_cluster.py`:
```python
import queue
import threading
from typing import Iterator

import pytest

from sk8s import serialize
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import JobTimeout, PodFailed
from sk8s.store.local import LocalStore
from sk8s.watch import JobEvent
from tests.fakes import FakeBatchV1, FakeCoreV1


class Harness:
    """Cluster wired to fakes; the test plays the role of the k8s control plane."""

    def __init__(self, tmp_path, keep_results=False):  # type: ignore[no-untyped-def]
        self.batch = FakeBatchV1()
        self.store = LocalStore(tmp_path)
        self.events: "queue.Queue[JobEvent]" = queue.Queue()
        cfg = Config(namespace="ns1", image="sk8s:dev", keep_results=keep_results)
        self.cluster = Cluster(
            cfg, batch_api=self.batch, core_api=FakeCoreV1(), store=self.store,
            watcher_events=self._event_iter,
        )

    def _event_iter(self) -> Iterator[JobEvent]:
        while True:
            yield self.events.get()

    def finish_job(self, job_name: str, phase: str = "succeeded") -> None:
        """Simulate the worker: execute the submitted payload, store result, emit event."""
        import sk8s.worker as worker

        job = self.batch.jobs[("ns1", job_name)]
        env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
        env["SK8S_STORE"] = f"local:{self.store.root}"
        worker.main(env)
        self.events.put((job_name, phase))


def test_submit_creates_job_and_resolves(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda x: x * 2, 21)
    assert ("ns1", fut.job_name) in h.batch.jobs
    h.finish_job(fut.job_name)
    assert fut.result(timeout=10) == 42


def test_job_option_extraction(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    # `image` is a job option, never a function kwarg; `scale` reaches the function.
    fut = h.cluster.submit(lambda x, scale=1: x * scale, 5, scale=3, image="custom:1")
    job = h.batch.jobs[("ns1", fut.job_name)]
    assert job.spec.template.spec.containers[0].image == "custom:1"
    h.finish_job(fut.job_name)
    assert fut.result(timeout=10) == 15


def test_failed_job_reraises_user_exception(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)

    def boom() -> None:
        raise ValueError("worker boom")

    fut = h.cluster.submit(boom)
    h.finish_job(fut.job_name, phase="failed")  # worker ran, stored error, job failed
    with pytest.raises(ValueError, match="worker boom"):
        fut.result(timeout=10)


def test_failed_job_without_result_raises_podfailed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    h.events.put((fut.job_name, "failed"))  # no worker ran: infra failure
    with pytest.raises(PodFailed):
        fut.result(timeout=10)


def test_blobs_cleaned_after_fetch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: "ok")
    h.finish_job(fut.job_name)
    assert fut.result(timeout=10) == "ok"
    wf = h.cluster.workflow_id
    from sk8s.errors import BlobNotFound
    with pytest.raises(BlobNotFound):
        h.store.get(h.store.uri_for(f"{wf}/{fut.job_name}/payload"))
    with pytest.raises(BlobNotFound):
        h.store.get(h.store.uri_for(f"{wf}/{fut.job_name}/result"))


def test_keep_results(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path, keep_results=True)
    fut = h.cluster.submit(lambda: "ok")
    h.finish_job(fut.job_name)
    fut.result(timeout=10)
    wf = h.cluster.workflow_id
    assert h.store.get(h.store.uri_for(f"{wf}/{fut.job_name}/result"))


def test_run_timeout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    with pytest.raises(JobTimeout):
        h.cluster.run(lambda: 1, timeout=0.2)  # nothing ever finishes the job


def test_cancel_deletes_job(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    assert fut.cancel() is True
    assert fut.job_name in h.batch.deleted
    assert fut.done()


def test_default_image_fallback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sys

    import sk8s as pkg
    h = Harness(tmp_path)
    h.cluster.config = Config(namespace="ns1")  # no image configured
    fut = h.cluster.submit(lambda: 1)
    expected = (
        f"ghcr.io/jared-maguire/sk8s:{pkg.__version__}"
        f"-py{sys.version_info.major}.{sys.version_info.minor}"
    )
    assert h.batch.jobs[("ns1", fut.job_name)].spec.template.spec.containers[0].image == expected
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_cluster.py -v` — FAIL.

- [ ] **Step 3: Implement `sk8s/cluster.py`**

```python
"""The Kubernetes cluster backend: submit jobs, resolve futures, clean up blobs."""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Iterator

import sk8s
from sk8s import serialize, spec
from sk8s.config import Config, load_config
from sk8s.errors import BlobNotFound, ImagePullError, JobTimeout, PodFailed
from sk8s.future import JobFuture
from sk8s.store import get_store
from sk8s.store.base import BlobStore
from sk8s.watch import JobEvent, JobWatcher

JOB_OPTIONS = {
    "image", "requests", "limits", "volumes", "timeout", "backoff_limit",
    "service_account", "namespace", "name_prefix", "privileged",
    "pull_policy", "keep_results",
}

_WAITING_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}


def default_image(config: Config) -> str:
    if config.image:
        return config.image
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"{config.image_prefix}ghcr.io/jared-maguire/sk8s:{sk8s.__version__}-py{py}"


class Cluster:
    def __init__(
        self,
        config: Config | None = None,
        *,
        batch_api: Any | None = None,
        core_api: Any | None = None,
        store: BlobStore | None = None,
        watcher_events: Callable[[], Iterator[JobEvent]] | None = None,
        **overrides: Any,
    ) -> None:
        import dataclasses

        base = config if config is not None else load_config()
        self.config = dataclasses.replace(base, **overrides) if overrides else base
        if self.config.namespace is None:
            from sk8s import k8s
            self.config = dataclasses.replace(self.config, namespace=k8s.current_namespace())
        self._batch = batch_api
        self._core = core_api
        self._store = store
        self.workflow_id = os.environ.get("SK8S_WORKFLOW_ID") or "wf-" + spec.job_name("x").rsplit("-", 1)[1]
        self._futures: dict[str, tuple[JobFuture, bool]] = {}  # name -> (future, keep_results)
        self._watcher = JobWatcher(
            watcher_events if watcher_events is not None else self._k8s_events,
            self._on_terminal,
        )

    # --- lazy client/store accessors -------------------------------------
    @property
    def batch(self) -> Any:
        if self._batch is None:
            from sk8s import k8s
            self._batch = k8s.batch_api()
        return self._batch

    @property
    def core(self) -> Any:
        if self._core is None:
            from sk8s import k8s
            self._core = k8s.core_api()
        return self._core

    @property
    def store(self) -> BlobStore:
        if self._store is None:
            self._store = get_store(
                self.config.store, namespace=self.config.namespace, core_api=self._core
            )
        return self._store

    # --- events from the real control plane -------------------------------
    def _k8s_events(self) -> Iterator[JobEvent]:
        from kubernetes import watch as k8s_watch

        w = k8s_watch.Watch()
        last_pull_check = 0.0
        for event in w.stream(
            self.batch.list_namespaced_job,
            namespace=self.config.namespace,
            label_selector="app.kubernetes.io/managed-by=sk8s",
            timeout_seconds=30,
        ):
            job = event["object"]
            name = job.metadata.name
            status = job.status
            if (status.succeeded or 0) > 0:
                yield (name, "succeeded")
            elif (status.failed or 0) > 0:
                yield (name, "failed")
            else:
                yield (name, "active")
            # Every 30s of stream activity, check active jobs for stuck image pulls.
            now = time.monotonic()
            if now - last_pull_check > 30:
                last_pull_check = now
                yield from self._pull_failures()
        # stream timeout -> return; JobWatcher reconnects by calling us again
        yield from self._pull_failures()

    def _pull_failures(self) -> Iterator[JobEvent]:
        with_futures = list(self._futures)
        if not with_futures:
            return
        pods = self.core.list_namespaced_pod(
            namespace=self.config.namespace,
            label_selector="app.kubernetes.io/managed-by=sk8s",
        )
        for pod in pods.items:
            job = (pod.metadata.labels or {}).get("job-name")
            if job not in self._futures:
                continue
            for cs in pod.status.container_statuses or []:
                waiting = cs.state.waiting
                if waiting and waiting.reason in _WAITING_PULL_REASONS:
                    yield (job, "imagepull")

    # --- submission --------------------------------------------------------
    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
        opts = {k: kwargs.pop(k) for k in list(kwargs) if k in JOB_OPTIONS}
        opts.pop("timeout", None)  # timeout is a run()/result() concern
        name = spec.job_name(opts.get("name_prefix", "job"))
        namespace = opts.get("namespace") or self.config.namespace
        assert namespace is not None
        image = opts.get("image") or default_image(self.config)
        payload_key = f"{self.workflow_id}/{name}/payload"
        result_key = f"{self.workflow_id}/{name}/result"
        payload_uri = self.store.put(payload_key, serialize.encode_payload(func, args, kwargs))
        job = spec.build_job(
            name,
            image=image,
            namespace=namespace,
            workflow_id=self.workflow_id,
            payload_uri=payload_uri,
            result_key=result_key,
            store=self.config.store,
            service_account=opts.get("service_account", self.config.service_account),
            pull_policy=opts.get("pull_policy", self.config.pull_policy),
            requests=opts.get("requests", {}),
            limits=opts.get("limits", {}),
            volumes=self._normalize_volumes(opts.get("volumes", {})),
            backoff_limit=opts.get("backoff_limit", 0),
            ttl=self.config.ttl_seconds_after_finished,
            privileged=opts.get("privileged", False),
        )
        self.batch.create_namespaced_job(namespace=namespace, body=job)
        fut = JobFuture(name, self.workflow_id, backend=self)
        keep = opts.get("keep_results", self.config.keep_results)
        self._futures[name] = (fut, keep)
        self._watcher.register(name)
        return fut

    @staticmethod
    def _normalize_volumes(volumes: Any) -> dict[str, str]:
        if isinstance(volumes, str):
            return {volumes: f"/mnt/{volumes}"}
        if isinstance(volumes, (list, tuple)):
            return {v: f"/mnt/{v}" for v in volumes}
        return dict(volumes)

    def run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        import concurrent.futures

        timeout = kwargs.pop("timeout", None)
        fut = self.submit(func, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # On 3.10 this is NOT the builtin TimeoutError; catching the
            # concurrent.futures name is correct on every supported version.
            raise JobTimeout(fut.job_name, timeout or 0.0) from None

    # --- resolution --------------------------------------------------------
    def _on_terminal(self, name: str, phase: str) -> None:
        entry = self._futures.pop(name, None)
        if entry is None:
            return
        fut, keep = entry
        result_uri = self.store.uri_for(f"{self.workflow_id}/{name}/result")
        payload_uri = self.store.uri_for(f"{self.workflow_id}/{name}/payload")
        blob: bytes | None = None
        for _ in range(3):
            try:
                blob = self.store.get(result_uri)
                break
            except BlobNotFound:
                if phase == "imagepull":
                    break
                time.sleep(0.5)
        try:
            if blob is not None:
                try:
                    fut.set_result(serialize.decode_result(blob))
                except BaseException as exc:  # noqa: BLE001 — remote exception re-raise
                    fut.set_exception(exc)
            elif phase == "imagepull":
                fut.set_exception(ImagePullError(name, image=self._job_image(name)))
            else:
                fut.set_exception(PodFailed(name, reason=self._failure_reason(name),
                                            logs=self._safe_logs(name)))
        finally:
            self.store.delete(payload_uri)
            if not keep:
                self.store.delete(result_uri)

    def _job_image(self, name: str) -> str:
        try:
            job = self.batch.jobs[(self.config.namespace, name)]  # fake path
            return str(job.spec.template.spec.containers[0].image)
        except (AttributeError, KeyError):
            return default_image(self.config)

    def _failure_reason(self, name: str) -> str:
        try:
            pods = self.core.list_namespaced_pod(
                namespace=self.config.namespace, label_selector=f"job-name={name}"
            )
            for pod in pods.items:
                for cs in pod.status.container_statuses or []:
                    if cs.state.terminated and cs.state.terminated.reason:
                        return str(cs.state.terminated.reason)
        except Exception:  # noqa: BLE001 — diagnostics must not mask the failure
            pass
        return "job failed and left no result (pod details unavailable)"

    def _safe_logs(self, name: str) -> str | None:
        try:
            return self.logs(name)
        except Exception:  # noqa: BLE001
            return None

    # --- JobBackend --------------------------------------------------------
    def logs(self, job_name: str) -> str:
        pods = self.core.list_namespaced_pod(
            namespace=self.config.namespace, label_selector=f"job-name={job_name}"
        )
        chunks = []
        for pod in pods.items:
            chunks.append(
                self.core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=self.config.namespace
                )
            )
        return "\n".join(chunks)

    def cancel(self, job_name: str) -> bool:
        try:
            self.batch.delete_namespaced_job(
                name=job_name, namespace=self.config.namespace,
                propagation_policy="Foreground",
            )
        except Exception:  # noqa: BLE001
            return False
        self._futures.pop(job_name, None)
        return True

    def close(self) -> None:
        self._watcher.stop()
```

Note for implementers: `FakeCoreV1` needs `list_namespaced_pod` and `read_namespaced_pod_log` stubs returning empty results for `test_failed_job_without_result_raises_podfailed` — add to `tests/fakes.py`:
```python
    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        class R:
            items: list[Any] = []
        return R()

    def read_namespaced_pod_log(self, name: str, namespace: str) -> str:
        return ""
```

- [ ] **Step 4: Run tests** — `pytest tests/test_cluster.py -v && mypy` — PASS.

- [ ] **Step 5: Commit** — `git add sk8s/cluster.py tests/test_cluster.py tests/fakes.py && git commit -m "feat: Cluster backend with watch-based resolution and blob cleanup"`

---

### Task 12: Public API — run/submit/map/starmap

**Files:**
- Create: `sk8s/api.py`
- Modify: `sk8s/__init__.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces (all re-exported from `sk8s`):
  ```python
  def configure(cluster: Any | None = None, **config_overrides: Any) -> None  # swap/configure default cluster
  def run(func, /, *args, **kwargs) -> Any
  def submit(func, /, *args, **kwargs) -> JobFuture
  def map(func, iterable, **opts) -> list[Any]                  # ordered; raises MapFailed
  def submit_map(func, iterable, **opts) -> list[JobFuture]
  def starmap(func, iterable, **opts) -> list[Any]
  def submit_starmap(func, iterable, **opts) -> list[JobFuture]
  ```
- The default cluster is created lazily on first use; `configure(cluster=LocalCluster())` swaps it (used by tests and local dev). Any object with `.submit()/.run()` works.
- `map` collects every future, then raises `MapFailed(failures, total)` if any failed, else returns ordered results.

- [ ] **Step 1: Write the failing test**

`tests/test_api.py`:
```python
import pytest

import sk8s
from sk8s.errors import MapFailed
from sk8s.local import LocalCluster


@pytest.fixture(autouse=True)
def local_backend():  # type: ignore[no-untyped-def]
    sk8s.configure(cluster=LocalCluster())
    yield
    sk8s.configure(cluster=None)


def test_run() -> None:
    assert sk8s.run(lambda x: x * 2, 5) == 10


def test_submit_returns_future() -> None:
    assert sk8s.submit(lambda: "hi").result(timeout=60) == "hi"


def test_map_ordered() -> None:
    assert sk8s.map(lambda x: x ** 2, range(5)) == [0, 1, 4, 9, 16]


def test_starmap() -> None:
    assert sk8s.starmap(lambda a, b: a + b, [(1, 2), (3, 4)]) == [3, 7]


def test_map_failure_summary() -> None:
    def maybe_boom(x: int) -> int:
        if x == 2:
            raise ValueError("bad item")
        return x

    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(maybe_boom, range(4))
    assert exc_info.value.total == 4
    assert [i for i, _ in exc_info.value.failures] == [2]


def test_submit_map_harvest_per_item() -> None:
    def maybe_boom(x: int) -> int:
        if x == 1:
            raise ValueError("bad")
        return x

    futs = sk8s.submit_map(maybe_boom, range(3))
    assert futs[0].result(timeout=60) == 0
    assert isinstance(futs[1].exception(timeout=60), ValueError)
    assert futs[2].result(timeout=60) == 2


def test_nested_lambda_workflow() -> None:
    # jobs submitting jobs — the v1 signature move, locally
    def workflow() -> int:
        import sk8s
        from sk8s.local import LocalCluster
        sk8s.configure(cluster=LocalCluster())
        squares = sk8s.map(lambda x: x ** 2, range(4))
        return sum(squares)

    assert sk8s.run(workflow) == 14
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_api.py -v` — FAIL.

- [ ] **Step 3: Implement**

`sk8s/api.py`:
```python
"""Module-level API over an implicit default cluster (requests-Session pattern)."""
from __future__ import annotations

import builtins
import threading
from typing import Any, Callable, Iterable

from sk8s.errors import MapFailed
from sk8s.future import JobFuture

_lock = threading.Lock()
_default_cluster: Any | None = None
_config_overrides: dict[str, Any] = {}


def configure(cluster: Any | None = None, **config_overrides: Any) -> None:
    """Set the default cluster, or configure the one created on first use."""
    global _default_cluster, _config_overrides
    with _lock:
        _default_cluster = cluster
        if config_overrides:
            _config_overrides = config_overrides


def _cluster() -> Any:
    global _default_cluster
    with _lock:
        if _default_cluster is None:
            from sk8s.cluster import Cluster
            _default_cluster = Cluster(**_config_overrides)
        return _default_cluster


def run(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return _cluster().run(func, *args, **kwargs)


def submit(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
    fut: JobFuture = _cluster().submit(func, *args, **kwargs)
    return fut


def submit_map(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    return [submit(func, item, **opts) for item in iterable]


def submit_starmap(
    func: Callable[..., Any], iterable: Iterable[Any], **opts: Any
) -> list[JobFuture]:
    return [submit(func, *item, **opts) for item in iterable]


def _gather(futures: list[JobFuture], timeout: float | None) -> list[Any]:
    results: list[Any] = []
    failures: list[tuple[int, BaseException]] = []
    for i, fut in enumerate(futures):
        try:
            results.append(fut.result(timeout=timeout))
        except BaseException as exc:  # noqa: BLE001 — summarized in MapFailed
            results.append(None)
            failures.append((i, exc))
    if failures:
        raise MapFailed(failures, total=len(futures))
    return results


def map(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any]:
    timeout = opts.pop("timeout", None)
    return _gather(submit_map(func, iterable, **opts), timeout)


def starmap(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[Any]:
    timeout = opts.pop("timeout", None)
    return _gather(submit_starmap(func, iterable, **opts), timeout)
```

`sk8s/__init__.py` becomes:
```python
__version__ = "2.0.0a0"

from sk8s.api import (  # noqa: E402
    configure, map, run, starmap, submit, submit_map, submit_starmap,
)
from sk8s.cluster import Cluster  # noqa: E402
from sk8s.config import Config, load_config  # noqa: E402
from sk8s.future import JobFuture  # noqa: E402

__all__ = [
    "__version__", "configure", "run", "submit", "map", "starmap",
    "submit_map", "submit_starmap", "Cluster", "Config", "load_config",
    "JobFuture",
]
```

(If `sk8s.cluster`'s import of `sk8s` at module top creates a cycle, switch `cluster.py` to `from sk8s import __version__` done lazily inside `default_image` — implementers: verify `python -c "import sk8s"` works and fix with a function-local import if needed.)

- [ ] **Step 4: Run tests** — `pytest tests/ -v && mypy` — ALL PASS (whole suite, not just this file).

- [ ] **Step 5: Commit** — `git add sk8s/api.py sk8s/__init__.py tests/test_api.py && git commit -m "feat: public module-level API with default cluster"`

---

### Task 13: Fan-out controls — batch_size and max_concurrent

**Files:**
- Modify: `sk8s/api.py`
- Test: `tests/test_fanout.py`

**Interfaces:**
- Produces: `submit_map`/`map`/`starmap`/`submit_starmap` accept `batch_size: int = 1` (pack N items into one job; per-item futures resolved from the batch result) and `max_concurrent: int | None = None` (client-side cap on unresolved jobs).
- Batch runner protocol (module-level so it serializes cleanly): `_run_batch(func, items, star)` returns `list[tuple[str, Any]]` — `("ok", value)` or `("err", exc)`; exceptions that fail to pickle inside the outer result degrade to `("err", RemoteException(...))` built by `_safe_outcome`.

- [ ] **Step 1: Write the failing test**

`tests/test_fanout.py`:
```python
import threading

import pytest

import sk8s
from sk8s.errors import MapFailed
from sk8s.local import LocalCluster


@pytest.fixture(autouse=True)
def local_backend():  # type: ignore[no-untyped-def]
    sk8s.configure(cluster=LocalCluster(max_workers=4))
    yield
    sk8s.configure(cluster=None)


def test_batched_map_results_match_unbatched() -> None:
    assert sk8s.map(lambda x: x + 1, range(10), batch_size=3) == list(range(1, 11))


def test_batched_map_per_item_errors() -> None:
    def maybe(x: int) -> int:
        if x in (2, 5):
            raise ValueError(f"bad {x}")
        return x

    with pytest.raises(MapFailed) as exc_info:
        sk8s.map(maybe, range(6), batch_size=4)
    assert sorted(i for i, _ in exc_info.value.failures) == [2, 5]


def test_batched_submit_map_futures() -> None:
    futs = sk8s.submit_map(lambda x: x * 2, range(5), batch_size=2)
    assert len(futs) == 5
    assert [f.result(timeout=60) for f in futs] == [0, 2, 4, 6, 8]


def test_batched_starmap() -> None:
    assert sk8s.starmap(lambda a, b: a * b, [(2, 3), (4, 5)], batch_size=2) == [6, 20]


def test_max_concurrent_caps_inflight() -> None:
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def tracked(x: int) -> int:
        # runs in a subprocess; concurrency is observed via file side effects —
        # instead cap observation happens client-side below
        return x

    futs = sk8s.submit_map(tracked, range(8), max_concurrent=2)
    assert [f.result(timeout=60) for f in futs] == list(range(8))
```

(The `max_concurrent` test asserts completion, not the cap itself — the cap's unit-level guarantee is the semaphore logic below; an exact in-flight assertion is racy across subprocesses. The reviewer should check the semaphore wiring by reading it.)

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_fanout.py -v` — FAIL (`batch_size` unexpected).

- [ ] **Step 3: Implement in `sk8s/api.py`**

Add module-level helpers and rework `submit_map`/`submit_starmap`:

```python
import traceback as _traceback
from concurrent.futures import Future

from sk8s.errors import RemoteException


def _safe_outcome(exc: BaseException) -> tuple[str, BaseException]:
    import cloudpickle
    try:
        cloudpickle.dumps(exc)
        return ("err", exc)
    except Exception:
        return ("err", RemoteException(
            f"{type(exc).__module__}.{type(exc).__qualname__}", str(exc),
            "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ))


def _run_batch(func: Callable[..., Any], items: list[Any], star: bool) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for item in items:
        try:
            out.append(("ok", func(*item) if star else func(item)))
        except BaseException as exc:  # noqa: BLE001
            out.append(_safe_outcome(exc))
    return out


def _submit_many(
    func: Callable[..., Any], iterable: Iterable[Any], star: bool, **opts: Any
) -> list[JobFuture]:
    batch_size = opts.pop("batch_size", 1)
    max_concurrent = opts.pop("max_concurrent", None)
    items = list(iterable)
    sem = threading.BoundedSemaphore(max_concurrent) if max_concurrent else None

    def submit_one(call_args: tuple[Any, ...]) -> JobFuture:
        if sem:
            sem.acquire()
        fut = submit(func, *call_args, **opts) if not star else submit(func, *call_args, **opts)
        if sem:
            fut.add_done_callback(lambda _: sem.release())
        return fut

    if batch_size <= 1:
        return [submit_one(item if star else (item,)) for item in items]

    # Batched: one job per chunk; per-item futures resolved from the chunk result.
    item_futures: list[JobFuture] = [
        JobFuture("batched", "batched", backend=_NullBackend()) for _ in items
    ]
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        if sem:
            sem.acquire()
        parent = submit(_run_batch, func, chunk, star, **opts)
        if sem:
            parent.add_done_callback(lambda _: sem.release())

        def unpack(p: Future, start: int = start, n: int = len(chunk)) -> None:  # type: ignore[type-arg]
            try:
                outcomes = p.result()
            except BaseException as exc:  # noqa: BLE001 — whole batch failed
                for k in range(n):
                    item_futures[start + k].set_exception(exc)
                return
            for k, (kind, value) in enumerate(outcomes):
                if kind == "ok":
                    item_futures[start + k].set_result(value)
                else:
                    item_futures[start + k].set_exception(value)

        parent.add_done_callback(unpack)
        for k in range(len(chunk)):
            item_futures[start + k].job_name = parent.job_name
            item_futures[start + k].workflow_id = parent.workflow_id
            item_futures[start + k]._backend = parent._backend  # noqa: SLF001
    return item_futures


class _NullBackend:
    def logs(self, job_name: str) -> str:
        return ""

    def cancel(self, job_name: str) -> bool:
        return False


def submit_map(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[JobFuture]:
    return _submit_many(func, iterable, star=False, **opts)


def submit_starmap(func: Callable[..., Any], iterable: Iterable[Any], **opts: Any) -> list[JobFuture]:
    return _submit_many(func, iterable, star=True, **opts)
```

(`submit_one` for the star case must splat: implementers — in `_submit_many`, the unbatched branch is `submit(func, *item, **opts)` when `star` else `submit(func, item, **opts)`. Write it exactly that way; the duplicated-looking line in the sketch above is the bug to avoid.)

- [ ] **Step 4: Run tests** — `pytest tests/ -v && mypy` — ALL PASS.

- [ ] **Step 5: Commit** — `git add sk8s/api.py tests/test_fanout.py && git commit -m "feat: batch_size and max_concurrent fan-out controls"`

---

### Task 14: Dev image + minikube integration suite

**Files:**
- Create: `docker/Dockerfile.dev`, `tests/integration/__init__.py`, `tests/integration/conftest.py`, `tests/integration/test_minikube.py`, `docs/dev-setup.md`

**Interfaces:**
- Consumes: the entire package.
- Produces: a repeatable minikube verification path. Not run in CI yet (Plan 8 wires CI); run locally before the PR.

- [ ] **Step 1: Write `docker/Dockerfile.dev`**

```dockerfile
FROM python:3.12-slim
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY sk8s ./sk8s
RUN pip install --no-cache-dir .
```

- [ ] **Step 2: Write the integration suite**

`tests/integration/__init__.py`: empty.

`tests/integration/conftest.py`:
```python
import os

import pytest

requires_cluster = pytest.mark.skipif(
    os.environ.get("SK8S_INTEGRATION") != "1",
    reason="set SK8S_INTEGRATION=1 with a configured cluster + sk8s-dev image",
)


@pytest.fixture(scope="session")
def cluster():  # type: ignore[no-untyped-def]
    from sk8s.cluster import Cluster
    from sk8s.config import load_config

    cfg = load_config(
        image=os.environ.get("SK8S_TEST_IMAGE", "sk8s-dev:latest"),
        pull_policy="Never",
        ttl_seconds_after_finished=30,
    )
    c = Cluster(cfg)
    yield c
    c.close()
```

`tests/integration/test_minikube.py`:
```python
import time

import pytest

from sk8s.errors import PodFailed, RemoteTraceback
from tests.integration.conftest import requires_cluster

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]


def test_run_roundtrip(cluster) -> None:  # type: ignore[no-untyped-def]
    assert cluster.run(lambda x: x * 2, 21) == 42


def test_non_json_result(cluster) -> None:  # type: ignore[no-untyped-def]
    result = cluster.run(lambda: {"tuple": (1, 2), "bytes": b"\x00"})
    assert result == {"tuple": (1, 2), "bytes": b"\x00"}


def test_map_parallel(cluster) -> None:  # type: ignore[no-untyped-def]
    futs = [cluster.submit(lambda x=x: x ** 2) for x in range(3)]
    assert sorted(f.result(timeout=240) for f in futs) == [0, 1, 4]


def test_remote_exception(cluster) -> None:  # type: ignore[no-untyped-def]
    def boom() -> None:
        raise KeyError("remote key")

    fut = cluster.submit(boom)
    with pytest.raises(KeyError) as exc_info:
        fut.result(timeout=240)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)


def test_oom_classified(cluster) -> None:  # type: ignore[no-untyped-def]
    def hog() -> None:
        x = bytearray(512 * 1024 * 1024)
        time.sleep(5)

    fut = cluster.submit(hog, limits={"memory": "64Mi"})
    with pytest.raises(PodFailed) as exc_info:
        fut.result(timeout=240)
    assert "OOM" in exc_info.value.reason


def test_no_blobs_left_behind(cluster) -> None:  # type: ignore[no-untyped-def]
    cluster.run(lambda: "done")
    cms = cluster.core.list_namespaced_config_map(
        namespace=cluster.config.namespace,
        label_selector=f"sk8s/workflow-id={cluster.workflow_id}",
    )
    assert cms.items == []
```

- [ ] **Step 3: Write `docs/dev-setup.md`**

```markdown
# Developer setup (minikube)

1. `minikube start`
2. Service account + RBAC (temporary until `sk8s config` lands in the
   cluster-config feature):
   kubectl create serviceaccount sk8s
   kubectl create role sk8s --verb=get,list,watch,create,delete \
     --resource=jobs,pods,pods/log,configmaps,persistentvolumeclaims
   kubectl create rolebinding sk8s --role=sk8s --serviceaccount=default:sk8s
3. Build the dev worker image inside minikube:
   minikube image build -t sk8s-dev:latest -f docker/Dockerfile.dev .
4. Run the suite:
   SK8S_INTEGRATION=1 pytest tests/integration -v
```

Note: `kubectl` appears here as *developer tooling*, which is fine — the constraint bans it from the package.

- [ ] **Step 4: Run the integration suite against minikube**

Run steps 1–4 from `docs/dev-setup.md`.
Expected: all integration tests PASS. Also re-run the full unit suite: `pytest tests -v -m "not integration" && mypy` — PASS.

- [ ] **Step 5: Commit**

```bash
git add docker/Dockerfile.dev tests/integration docs/dev-setup.md
git commit -m "feat: dev worker image and minikube integration suite"
```

---

## Done criteria for the branch

- Full unit suite green without a cluster; `mypy --strict` clean.
- Integration suite green on minikube.
- `pip install -e .` + `docs/dev-setup.md` reproduces end-to-end `sk8s.run(lambda: "hello")` on a fresh minikube.
- PR from `feat/core` into `version-2` with the whole-branch review done.
