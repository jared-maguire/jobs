"""Local backend: same pipeline as the k8s cluster, workers run as subprocesses."""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from sk8s import expect as expectations
from sk8s import memo as memolib
from sk8s import serialize, spec, staging
from sk8s.cas import ArtifactStore
from sk8s.errors import BlobNotFound, JobTimeout, PodFailed
from sk8s.future import JobFuture
from sk8s.store.local import LocalStore

# Options accepted (for interface parity with Cluster) but meaningless locally.
IGNORED_OPTIONS = {
    "image", "requests", "limits", "volumes", "backoff_limit",
    "service_account", "namespace", "privileged", "pull_policy", "timeout", "budget",
}


class LocalCluster:
    def __init__(self, root: Path | str | None = None, max_workers: int = 8) -> None:
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="sk8s-local-"))
        self.store = LocalStore(self.root)
        self.artifact_store = ArtifactStore(self.store, f"local:{self.root}")
        self.workflow_id = "wf-" + spec.job_name("x").rsplit("-", 1)[1]
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        # All four fields are guarded by _lock. A job is "queued" from submit()
        # until its pool thread starts; "procs" holds the spawned worker;
        # "cancelled" marks jobs whose pool thread must stand down.
        self._lock = threading.Lock()
        self._queued: set[str] = set()
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()

    def logs(self, job_name: str) -> str:
        return ""  # local workers inherit stdio; logs land on the terminal

    def cancel(self, job_name: str) -> bool:
        with self._lock:
            if job_name in self._queued:
                # Not spawned yet: the pool thread will see the flag and skip Popen.
                self._queued.discard(job_name)
                self._cancelled.add(job_name)
                return True
            proc = self._procs.get(job_name)
            if proc is not None and proc.poll() is None:
                # Flag before kill so the pool thread knows the exit was requested.
                self._cancelled.add(job_name)
                proc.kill()
                return True
            return False

    def dry_run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        raise ValueError(
            "dry_run is not supported by LocalCluster: it runs subprocesses, not Kubernetes Jobs, "
            "so there is no Job body to preview"
        )

    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> JobFuture:
        if kwargs.pop("dry_run", False):
            self.dry_run(func, *args, **kwargs)
        for k in IGNORED_OPTIONS:
            kwargs.pop(k, None)  # accepted for parity; meaningless locally
        kwargs.pop("keep_results", None)
        expect = kwargs.pop("expect", None)
        if expect is not None:
            expectations.require_callable(expect)
        use_memo = bool(kwargs.pop("memo", False))
        memo_salt = kwargs.pop("memo_salt", None)
        name = spec.job_name(kwargs.pop("name_prefix", "job"))
        prepared = staging.prepare_submission(self.artifact_store, self.workflow_id, args, kwargs)
        memo_key: str | None = None
        if use_memo:
            memo_key = memolib.memo_key(
                func, prepared.args, prepared.kwargs, image="local", salt=memo_salt, expect=expect
            )
            hit = memolib.lookup(self.artifact_store, memo_key)
            if hit is not None and memolib.adopt_outputs(
                self.artifact_store, self.workflow_id, hit
            ):
                return memolib.hit_future(self.workflow_id, hit)
        payload_uri = self.store.put(
            f"{self.workflow_id}/{name}/payload",
            serialize.encode_payload(
                func, prepared.args, prepared.kwargs, expect=expect, extra_header=prepared.header
            ),
        )
        result_key = f"{self.workflow_id}/{name}/result"
        fut = JobFuture(name, self.workflow_id, backend=self)

        def run() -> None:
            env = {
                **os.environ,  # user functions see a normal environment
                "SK8S_STORE": f"local:{self.root}",
                "SK8S_PAYLOAD_URI": payload_uri,
                "SK8S_RESULT_KEY": result_key,
                # editable installs: the worker must be able to import sk8s
                "PYTHONPATH": ":".join(sys.path),
            }
            with self._lock:
                self._queued.discard(name)
                if name in self._cancelled:
                    self._cancelled.discard(name)
                    return  # cancelled while queued: never spawn
                proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                    [sys.executable, "-m", "sk8s.worker"], env=env
                )
                self._procs[name] = proc
            proc.wait()
            with self._lock:
                self._procs.pop(name, None)
                if name in self._cancelled:
                    self._cancelled.discard(name)
                    return  # killed by cancel(); JobFuture.cancel() owns the state
            if fut.done():
                return
            try:
                blob = self.store.get(self.store.uri_for(result_key))
            except BlobNotFound:
                fut.set_exception(PodFailed(
                    name,
                    reason=f"worker exited with status {proc.returncode} and wrote no result",
                ))
                return
            except Exception as exc:
                fut.set_exception(exc)
                return
            try:
                value = serialize.decode_result(blob)
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)
                return
            if memo_key is not None:  # decoded without error = the job succeeded
                try:
                    memolib.write(
                        self.artifact_store, memo_key, value,
                        function=getattr(func, "__qualname__", repr(func)),
                        produced_by=f"{self.workflow_id}/{name}", image="local",
                        inputs=prepared.inputs,
                    )
                except Exception:  # noqa: BLE001 — a cache write must never fail a successful job
                    pass
            fut.set_result(value)

        with self._lock:
            self._queued.add(name)
        self._pool.submit(run)
        return fut

    def run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", None)
        fut = self.submit(func, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise JobTimeout(fut.job_name, timeout or 0.0) from None
