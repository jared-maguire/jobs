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
        x = bytearray(512 * 1024 * 1024)  # noqa: F841
        time.sleep(5)

    fut = cluster.submit(hog, limits={"memory": "64Mi"})
    with pytest.raises(PodFailed) as exc_info:
        fut.result(timeout=240)
    assert "OOM" in exc_info.value.reason


def test_no_blobs_left_behind(cluster) -> None:  # type: ignore[no-untyped-def]
    cluster.run(lambda: "done")
    # Blob deletion completes shortly *after* result delivery, so poll.
    deadline = time.monotonic() + 10
    items = None
    while time.monotonic() < deadline:
        items = cluster.core.list_namespaced_config_map(
            namespace=cluster.config.namespace,
            # The workflow anchor (sk8s/kind=anchor) legitimately outlives the job.
            label_selector=f"sk8s/workflow-id={cluster.workflow_id},sk8s/kind!=anchor",
        ).items
        if not items:
            break
        time.sleep(0.25)
    assert items == []
