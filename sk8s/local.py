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
                # editable installs: the worker must be able to import sk8s
                "PYTHONPATH": ":".join(sys.path),
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

    def run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", None)
        return self.submit(func, *args, **kwargs).result(timeout)
