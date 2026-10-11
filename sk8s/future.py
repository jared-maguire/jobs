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
