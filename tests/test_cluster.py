import queue
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from kubernetes import client
from kubernetes.client import ApiException

from sk8s import anchors
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.errors import BlobNotFound, ImagePullError, JobTimeout, PodFailed, StoreError
from sk8s.store.local import LocalStore
from sk8s.watch import JobEvent
from tests.fakes import FakeBatchV1, FakeCoreV1


_OPEN: "list[Cluster]" = []


@pytest.fixture(autouse=True)
def _close_clusters() -> Iterator[None]:
    yield
    while _OPEN:
        _OPEN.pop().close()


class Harness:
    """Cluster wired to fakes; the test plays the role of the k8s control plane."""

    def __init__(self, tmp_path, keep_results=False, core=None):  # type: ignore[no-untyped-def]
        self.batch = FakeBatchV1()
        self.core = core if core is not None else FakeCoreV1()
        self.store = LocalStore(tmp_path)
        self.events: "queue.Queue[JobEvent]" = queue.Queue()
        cfg = Config(namespace="ns1", image="sk8s:dev", keep_results=keep_results)
        self.cluster = Cluster(
            cfg, batch_api=self.batch, core_api=self.core, store=self.store,
            watcher_events=self._event_iter,
        )
        _OPEN.append(self.cluster)

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


def test_failed_job_without_result_raises_podfailed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.0)
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


def test_cancel_cleans_blobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    wf, name = h.cluster.workflow_id, fut.job_name
    payload_uri = h.store.uri_for(f"{wf}/{name}/payload")
    result_uri = h.store.uri_for(f"{wf}/{name}/result")
    # A result written just before cancellation must not be left behind either.
    h.store.put(f"{wf}/{name}/result", b"late")
    assert h.store.get(payload_uri)
    with h.cluster._watcher._lock:
        assert name in h.cluster._watcher._registered
    assert fut.cancel() is True
    for uri in (payload_uri, result_uri):
        with pytest.raises(BlobNotFound):
            h.store.get(uri)
    with h.cluster._watcher._lock:
        assert name not in h.cluster._watcher._registered
    assert name not in h.cluster._futures


def test_cancel_keeps_result_blob_when_keep_results(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path, keep_results=True)
    fut = h.cluster.submit(lambda: 1)
    wf, name = h.cluster.workflow_id, fut.job_name
    h.store.put(f"{wf}/{name}/result", b"keep me")
    assert fut.cancel() is True
    with pytest.raises(BlobNotFound):
        h.store.get(h.store.uri_for(f"{wf}/{name}/payload"))
    assert h.store.get(h.store.uri_for(f"{wf}/{name}/result")) == b"keep me"


def test_cancel_never_raises_when_cleanup_fails(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)

    def broken(uri: str) -> None:
        raise OSError("store down")

    monkeypatch.setattr(h.store, "delete", broken)
    assert fut.cancel() is True


def test_submit_failure_does_not_leak_payload_blob(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)

    def failing_create(namespace: str, body: Any) -> Any:
        raise ApiException(status=403, reason="Forbidden")

    h.batch.create_namespaced_job = failing_create  # type: ignore[method-assign]
    with pytest.raises(ApiException):
        h.cluster.submit(lambda: 1)
    assert not [p for p in tmp_path.rglob("*") if p.is_file()], "payload blob leaked"
    assert not h.cluster._futures


def test_workflow_id_from_environment(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SK8S_WORKFLOW_ID", "wf-from-env")
    h = Harness(tmp_path)
    assert h.cluster.workflow_id == "wf-from-env"


@pytest.mark.parametrize("reserved", ["svc", "doctor"])
def test_workflow_id_reserved(tmp_path, monkeypatch, reserved) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SK8S_WORKFLOW_ID", reserved)
    with pytest.raises(ValueError, match="reserved"):
        Harness(tmp_path)


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


# --- binding contract: _on_terminal never raises, future always resolves ---------


class _FlakyStore(LocalStore):
    """LocalStore whose get/delete can be made to fail with non-BlobNotFound errors."""

    def __init__(self, root) -> None:  # type: ignore[no-untyped-def]
        super().__init__(root)
        self.get_error: Exception | None = None
        self.delete_error: Exception | None = None

    def get(self, uri: str) -> bytes:
        if self.get_error is not None:
            raise self.get_error
        return super().get(uri)

    def delete(self, uri: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        super().delete(uri)


def _flaky_harness(tmp_path):  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    flaky = _FlakyStore(tmp_path)
    h.store = flaky
    h.cluster._store = flaky
    return h, flaky


def test_store_error_on_fetch_still_resolves_future(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.0)
    h, flaky = _flaky_harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    flaky.get_error = RuntimeError("apiserver on fire")
    h.events.put((fut.job_name, "succeeded"))
    with pytest.raises(StoreError, match="apiserver on fire"):
        fut.result(timeout=10)  # an exception, not a hang


def test_on_terminal_never_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.0)
    h, flaky = _flaky_harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    flaky.get_error = RuntimeError("boom")
    flaky.delete_error = RuntimeError("delete boom")
    h.cluster._on_terminal(fut.job_name, "succeeded")  # must not raise
    assert isinstance(fut.exception(timeout=1), StoreError)


def test_delete_failure_does_not_lose_result(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h, flaky = _flaky_harness(tmp_path)
    fut = h.cluster.submit(lambda: "ok")
    flaky.delete_error = RuntimeError("cannot delete")
    h.finish_job(fut.job_name)
    assert fut.result(timeout=10) == "ok"


def test_terminal_event_after_cancel_is_harmless(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import concurrent.futures

    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.0)
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    # Cancel the future without going through the backend, so the cluster's entry
    # is still present when the terminal event arrives: set_result hits
    # InvalidStateError, which must be swallowed.
    assert concurrent.futures.Future.cancel(fut) is True
    assert fut.job_name in h.cluster._futures
    h.cluster._on_terminal(fut.job_name, "succeeded")  # must not raise
    assert fut.cancelled()
    assert fut.job_name not in h.cluster._futures


def test_result_late_visible_is_retried(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.01)
    h, flaky = _flaky_harness(tmp_path)
    fut = h.cluster.submit(lambda: 7)
    real_get = LocalStore.get
    calls = {"n": 0}

    def lagging_get(uri: str) -> bytes:
        calls["n"] += 1
        if calls["n"] < 3:
            raise BlobNotFound(uri)
        return real_get(flaky, uri)

    flaky.get = lagging_get  # type: ignore[method-assign]
    import sk8s.worker as worker

    job = h.batch.jobs[("ns1", fut.job_name)]
    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    env["SK8S_STORE"] = f"local:{flaky.root}"
    worker.main(env)
    h.events.put((fut.job_name, "succeeded"))
    assert fut.result(timeout=10) == 7
    assert calls["n"] == 3


def test_imagepull_event_raises_image_pull_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1, image="nope:1")
    h.events.put((fut.job_name, "imagepull"))
    with pytest.raises(ImagePullError, match="nope:1"):
        fut.result(timeout=10)


# --- binding contract: _k8s_events replays current state via an initial LIST ----


def _set_status(h: Harness, name: str, **status) -> None:  # type: ignore[no-untyped-def]
    h.batch.jobs[("ns1", name)].status = client.V1JobStatus(**status)


def test_initial_list_replays_already_terminal_jobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    done = h.cluster.submit(lambda: 1)
    bad = h.cluster.submit(lambda: 2)
    running = h.cluster.submit(lambda: 3)
    _set_status(h, done.job_name, succeeded=1)
    _set_status(h, bad.job_name, failed=1)
    _set_status(h, running.job_name, active=1)
    # The list portion runs before the (untested here) watch stream is opened.
    gen = h.cluster._k8s_events()
    first = [next(gen) for _ in range(3)]
    gen.close()
    assert dict(first) == {
        done.job_name: "succeeded",
        bad.job_name: "failed",
        running.job_name: "active",
    }


def test_initial_list_only_selects_sk8s_jobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    other = client.V1Job(metadata=client.V1ObjectMeta(name="foreign", labels={"app": "x"}))
    h.batch.create_namespaced_job("ns1", other)
    _set_status(h, fut.job_name, succeeded=1)
    gen = h.cluster._k8s_events()
    assert next(gen) == (fut.job_name, "succeeded")
    gen.close()


def test_replayed_terminal_event_resolves_future_end_to_end(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A job that finished while the stream was down resolves once events replay."""
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda x: x + 1, 1)
    # Run the worker but never emit a live event; only the replayed LIST reports it.
    import sk8s.worker as worker

    job = h.batch.jobs[("ns1", fut.job_name)]
    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    env["SK8S_STORE"] = f"local:{h.store.root}"
    worker.main(env)
    _set_status(h, fut.job_name, succeeded=1)
    gen = h.cluster._k8s_events()
    h.events.put(next(gen))  # what the watcher would receive after reconnecting
    gen.close()
    assert fut.result(timeout=10) == 2


def test_phase_respects_backoff_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    retrying = h.cluster.submit(lambda: 1, backoff_limit=2)
    exhausted = h.cluster.submit(lambda: 1, backoff_limit=2)
    _set_status(h, retrying.job_name, failed=1, active=1)  # one attempt failed, retries left
    _set_status(h, exhausted.job_name, failed=3)
    gen = h.cluster._k8s_events()
    got = dict(next(gen) for _ in range(2))
    gen.close()
    assert got == {retrying.job_name: "active", exhausted.job_name: "failed"}


# --- fix round 1 -----------------------------------------------------------------


class _RaisingCore(FakeCoreV1):
    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        raise ApiException(status=403, reason="Forbidden")


def test_pull_probe_failure_does_not_wedge_event_stream(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster._core = _RaisingCore()
    fut = h.cluster.submit(lambda: 1)
    _set_status(h, fut.job_name, succeeded=1)
    job = h.batch.jobs[("ns1", fut.job_name)]
    watch_opened: list[bool] = []

    class FakeWatch:
        def stream(self, *_a: Any, **_kw: Any) -> Iterator[dict[str, Any]]:
            watch_opened.append(True)
            yield {"object": job}

    monkeypatch.setattr("kubernetes.watch.Watch", FakeWatch)
    events = list(h.cluster._k8s_events())
    # list replay, then (probe failed harmlessly) the watch opened and delivered
    assert events == [(fut.job_name, "succeeded"), (fut.job_name, "succeeded")]
    assert watch_opened == [True]


def test_future_resolves_when_pull_probe_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    h.cluster._core = _RaisingCore()
    fut = h.cluster.submit(lambda x: x + 1, 1)
    import sk8s.worker as worker

    job = h.batch.jobs[("ns1", fut.job_name)]
    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    env["SK8S_STORE"] = f"local:{h.store.root}"
    worker.main(env)
    _set_status(h, fut.job_name, succeeded=1)
    gen = h.cluster._k8s_events()
    h.events.put(next(gen))
    gen.close()
    assert fut.result(timeout=10) == 2


def test_pull_probe_tolerates_missing_container_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)

    class Core(FakeCoreV1):
        def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
            cs = SimpleNamespace(state=None)
            pod = SimpleNamespace(
                metadata=SimpleNamespace(labels={"job-name": fut.job_name}),
                status=SimpleNamespace(container_statuses=[cs]),
            )
            return SimpleNamespace(items=[pod])

    h.cluster._core = Core()
    assert list(h.cluster._pull_failures()) == []


def test_pull_probe_detects_image_pull_backoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)

    class Core(FakeCoreV1):
        def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
            waiting = SimpleNamespace(reason="ImagePullBackOff")
            cs = SimpleNamespace(state=SimpleNamespace(waiting=waiting))
            pod = SimpleNamespace(
                metadata=SimpleNamespace(labels={"job-name": fut.job_name}),
                status=SimpleNamespace(container_statuses=[cs]),
            )
            return SimpleNamespace(items=[pod])

    h.cluster._core = Core()
    assert list(h.cluster._pull_failures()) == [(fut.job_name, "imagepull")]


def test_imagepull_deletes_stuck_job(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1, image="nope:1")
    h.events.put((fut.job_name, "imagepull"))
    with pytest.raises(ImagePullError):
        fut.result(timeout=10)
    assert fut.job_name in h.batch.deleted


def test_imagepull_delete_failure_still_resolves(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)

    def bad_delete(**_: Any) -> None:
        raise ApiException(status=500, reason="nope")

    h.batch.delete_namespaced_job = bad_delete  # type: ignore[method-assign,assignment]
    fut = h.cluster.submit(lambda: 1, image="nope:1")
    h.cluster._on_terminal(fut.job_name, "imagepull")  # must not raise
    assert isinstance(fut.exception(timeout=1), ImagePullError)


def test_per_job_namespace_mismatch_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    with pytest.raises(ValueError, match="later release"):
        h.cluster.submit(lambda: 1, namespace="other")
    assert h.batch.jobs == {}  # nothing created
    assert h.cluster._futures == {}


def test_per_job_namespace_same_value_accepted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1, namespace="ns1")
    assert ("ns1", fut.job_name) in h.batch.jobs


# --- final review: Job deletion must resolve futures ---------------------------------

_DELETED_REASON = "job was deleted before completing (workflow killed or expired?)"


def _fake_watch(monkeypatch, events: "list[dict[str, Any]]") -> None:  # type: ignore[no-untyped-def]
    class FakeWatch:
        def stream(self, *_a: Any, **_kw: Any) -> Iterator[dict[str, Any]]:
            yield from events

    monkeypatch.setattr("kubernetes.watch.Watch", FakeWatch)


def test_deleted_watch_event_fails_active_job_with_deleted_reason(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.0)
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    job = h.batch.jobs[("ns1", fut.job_name)]
    _set_status(h, fut.job_name, active=1)
    _fake_watch(monkeypatch, [{"type": "DELETED", "object": job}])
    gen = h.cluster._k8s_events()
    assert next(gen) == (fut.job_name, "active")  # the LIST replay
    # the job is cascade-deleted (kill/clean) after the LIST, then DELETED arrives
    del h.batch.jobs[("ns1", fut.job_name)]
    assert next(gen) == (fut.job_name, "failed")  # not "active": DELETED is never dropped
    gen.close()
    h.events.put((fut.job_name, "failed"))
    with pytest.raises(PodFailed) as ei:
        fut.result(timeout=10)
    assert ei.value.reason == _DELETED_REASON


def test_reconnect_list_omitting_registered_job_fails_it(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sk8s.cluster._RETRY_DELAY", 0.0)
    h = Harness(tmp_path)
    gone = h.cluster.submit(lambda: 1)
    kept = h.cluster.submit(lambda: 2)
    _set_status(h, kept.job_name, active=1)
    del h.batch.jobs[("ns1", gone.job_name)]  # deleted while the stream was down
    _fake_watch(monkeypatch, [])
    events = list(h.cluster._k8s_events())
    assert (gone.job_name, "failed") in events
    assert (kept.job_name, "active") in events
    assert (kept.job_name, "failed") not in events
    h.events.put((gone.job_name, "failed"))
    with pytest.raises(PodFailed) as ei:
        gone.result(timeout=10)
    assert ei.value.reason == _DELETED_REASON
    assert not kept.done()


def test_reconnect_list_ignores_job_registered_after_snapshot(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A job registered while the LIST is in flight is not 'absent': it postdates the list."""
    h = Harness(tmp_path)
    _fake_watch(monkeypatch, [])
    real_list = h.batch.list_namespaced_job
    late: list[str] = []

    def list_then_submit(*a: Any, **kw: Any) -> Any:
        result = real_list(*a, **kw)  # snapshot of jobs: does not contain the late one
        late.append(h.cluster.submit(lambda: 1).job_name)
        return result

    h.batch.list_namespaced_job = list_then_submit  # type: ignore[method-assign]
    events = list(h.cluster._k8s_events())
    assert late and all(name != late[0] for name, _ in events)


def test_ttl_deleted_job_with_result_blob_resolves_with_result(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda x: x + 1, 41)
    job = h.batch.jobs[("ns1", fut.job_name)]
    import sk8s.worker as worker

    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    env["SK8S_STORE"] = f"local:{h.store.root}"
    worker.main(env)  # result blob stored; the success event was missed
    del h.batch.jobs[("ns1", fut.job_name)]  # TTL controller reaped the finished job
    _fake_watch(monkeypatch, [{"type": "DELETED", "object": job}])
    gen = h.cluster._k8s_events()
    assert next(gen) == (fut.job_name, "failed")  # absent from the LIST
    gen.close()
    h.events.put((fut.job_name, "failed"))
    assert fut.result(timeout=10) == 42  # the result wins over the failed verdict


def test_deleted_event_for_unfinished_job_with_result_blob_in_stream(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: "kept")
    job = h.batch.jobs[("ns1", fut.job_name)]
    import sk8s.worker as worker

    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    env["SK8S_STORE"] = f"local:{h.store.root}"
    worker.main(env)
    _set_status(h, fut.job_name, succeeded=1)
    _fake_watch(monkeypatch, [{"type": "DELETED", "object": job}])
    events = list(h.cluster._k8s_events())
    assert events[-1] == (fut.job_name, "failed")  # DELETED yields failed regardless of status
    h.events.put(events[-1])
    assert fut.result(timeout=10) == "kept"


# --- final review: deleted anchor must fail loudly ----------------------------------


def test_deleted_anchor_detected_after_freshness_window(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    now = [1000.0]
    h.cluster._clock = lambda: now[0]
    h.cluster.submit(lambda: 1)  # creates and caches the anchor at t=1000
    del h.core.configmaps[("ns1", anchors.anchor_name(h.cluster.workflow_id))]
    now[0] += 30  # inside the window: no re-verification, no extra API cost
    h.cluster.submit(lambda: 2)
    now[0] += 61  # beyond the window since the last verification
    jobs_before = len(h.batch.jobs)
    with pytest.raises(RuntimeError, match="was deleted"):
        h.cluster.submit(lambda: 3)
    assert len(h.batch.jobs) == jobs_before  # nothing was created into the dead anchor
    with pytest.raises(RuntimeError, match="was deleted"):
        h.cluster.submit(lambda: 4)  # and it stays loud


def test_present_anchor_is_reverified_at_most_once_per_window(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    now = [1000.0]
    h.cluster._clock = lambda: now[0]
    h.cluster.submit(lambda: 1)
    reads: list[str] = []
    real = h.core.read_namespaced_config_map

    def counting(name: str, namespace: str) -> Any:
        reads.append(name)
        return real(name=name, namespace=namespace)

    h.core.read_namespaced_config_map = counting  # type: ignore[method-assign]
    for _ in range(5):
        h.cluster.submit(lambda: 1)
    assert reads == []
    now[0] += 61
    h.cluster.submit(lambda: 1)
    h.cluster.submit(lambda: 1)
    assert len(reads) == 1


def test_transient_error_during_anchor_reverify_does_not_block_submit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    now = [1000.0]
    h.cluster._clock = lambda: now[0]
    h.cluster.submit(lambda: 1)

    def flaky(name: str, namespace: str) -> Any:
        raise ApiException(status=500, reason="boom")

    h.core.read_namespaced_config_map = flaky  # type: ignore[method-assign]
    now[0] += 61
    assert h.cluster.submit(lambda: 2).job_name


def test_registered_snapshot_reflects_register_and_unregister(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path)
    fut = h.cluster.submit(lambda: 1)
    assert h.cluster._watcher.registered() == {fut.job_name}
    h.cluster.untrack_job(fut.job_name)
    assert h.cluster._watcher.registered() == set()


def test_store_endpoint_flows_to_job_env_and_store_factory(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict = {}  # type: ignore[type-arg]

    def fake_get_store(store, **kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return LocalStore(tmp_path)

    monkeypatch.setattr("sk8s.cluster.get_store", fake_get_store)
    batch = FakeBatchV1()
    cfg = Config(namespace="ns1", image="sk8s:dev", store="s3://b/p/", store_endpoint="http://minio:9000")
    cluster = Cluster(cfg, batch_api=batch, core_api=FakeCoreV1(), watcher_events=lambda: iter(()))
    _OPEN.append(cluster)
    fut = cluster.submit(lambda: 1)
    env = {e.name: e.value for e in batch.jobs[("ns1", fut.job_name)].spec.template.spec.containers[0].env}
    assert env["SK8S_S3_ENDPOINT"] == "http://minio:9000"
    assert seen["endpoint"] == "http://minio:9000"


def test_store_env_flows_to_job_env(tmp_path) -> None:  # type: ignore[no-untyped-def]
    batch = FakeBatchV1()
    cfg = Config(namespace="ns1", image="sk8s:dev", store_env={"AWS_ACCESS_KEY_ID": "k"})
    cluster = Cluster(
        cfg, batch_api=batch, core_api=FakeCoreV1(),
        store=LocalStore(tmp_path), watcher_events=lambda: iter(()),
    )
    _OPEN.append(cluster)
    fut = cluster.submit(lambda: 1)
    env = {e.name: e.value for e in batch.jobs[("ns1", fut.job_name)].spec.template.spec.containers[0].env}
    assert env["AWS_ACCESS_KEY_ID"] == "k"


# --- namespace resolution: explicit > kube-context namespace (if set) > "sk8s" -----------------


def _bare_cluster(monkeypatch: pytest.MonkeyPatch, tmp_path, config=None, **kw):  # type: ignore[no-untyped-def]
    c = Cluster(
        config if config is not None else Config(image="sk8s:dev"),
        batch_api=FakeBatchV1(), core_api=FakeCoreV1(), store=LocalStore(tmp_path), **kw,
    )
    _OPEN.append(c)
    return c


def test_namespace_explicit_config_beats_context(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sk8s import k8s

    monkeypatch.setattr(k8s, "context_namespace", lambda: "ctx-ns")
    c = _bare_cluster(monkeypatch, tmp_path, Config(namespace="explicit", image="x"))
    assert c.config.namespace == "explicit"


def test_namespace_explicit_kwarg_beats_context(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sk8s import k8s

    monkeypatch.setattr(k8s, "context_namespace", lambda: "ctx-ns")
    c = _bare_cluster(monkeypatch, tmp_path, namespace="kw")
    assert c.config.namespace == "kw"


def test_namespace_context_beats_default(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sk8s import k8s

    monkeypatch.setattr(k8s, "context_namespace", lambda: "ctx-ns")
    assert _bare_cluster(monkeypatch, tmp_path).config.namespace == "ctx-ns"


def test_namespace_context_without_namespace_is_sk8s_never_default(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sk8s import k8s

    monkeypatch.setattr(k8s, "context_namespace", lambda: None)
    ns = _bare_cluster(monkeypatch, tmp_path).config.namespace
    assert ns == "sk8s" and ns != "default"


def test_event_stream_never_runs_before_the_watcher_is_assigned() -> None:
    """Regression: the stream thread used to start inside JobWatcher.__init__, before
    ``Cluster._watcher`` existed, so ``_k8s_events`` could hit AttributeError on first connect."""
    from unittest import mock

    from sk8s.watch import JobWatcher

    seen: list[bool] = []
    real_start = JobWatcher.start

    def spy(self: JobWatcher) -> None:
        seen.append(hasattr(owner[0], "_watcher"))
        real_start(self)

    owner: list[Cluster] = []
    cfg = Config(namespace="ns", image="img")
    with mock.patch.object(JobWatcher, "start", spy):
        c = Cluster.__new__(Cluster)
        owner.append(c)
        c.__init__(cfg, batch_api=FakeBatchV1(), core_api=FakeCoreV1(),  # type: ignore[misc]
                   watcher_events=lambda: iter(()))
    assert seen == [True]
    c.close()
