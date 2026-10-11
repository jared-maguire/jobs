"""S3 store against an in-cluster MinIO (deployed by tests/integration/minio.py).

The client reaches MinIO through a `kubectl port-forward`; worker pods reach it via
the in-cluster Service DNS name. Needs an image built with the s3 extra:
    minikube image build -t sk8s-dev-stores:latest -f docker/Dockerfile.dev .
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest

from sk8s.errors import RemoteTraceback
from tests.integration import minio
from tests.integration.conftest import requires_cluster

pytestmark = [pytest.mark.integration, requires_cluster, pytest.mark.timeout(300)]

BUCKET = "sk8s-test"
PREFIX = "it/"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def s3_client() -> Iterator[Any]:
    """boto3 client against MinIO via port-forward; also sets client-side env."""
    import boto3
    from botocore.config import Config as BotoConfig

    in_cluster_endpoint = minio.ensure_minio()
    assert in_cluster_endpoint.startswith("http://minio.")
    port = _free_port()
    pf = subprocess.Popen(
        ["kubectl", "port-forward", "svc/minio", f"{port}:{minio.PORT}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    saved = {k: os.environ.get(k) for k in
             ("SK8S_S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}
    endpoint = f"http://127.0.0.1:{port}"
    os.environ["SK8S_S3_ENDPOINT"] = endpoint
    os.environ["AWS_ACCESS_KEY_ID"] = minio.ACCESS_KEY
    os.environ["AWS_SECRET_ACCESS_KEY"] = minio.SECRET_KEY
    try:
        s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name="us-east-1",
            config=BotoConfig(retries={"max_attempts": 1}, connect_timeout=3),
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                s3.list_buckets()
                break
            except Exception:  # noqa: BLE001 — port-forward not accepting yet
                if time.monotonic() > deadline or pf.poll() is not None:
                    raise
                time.sleep(0.5)
        try:
            s3.create_bucket(Bucket=BUCKET)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
        yield s3
    finally:
        pf.terminate()
        try:
            pf.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pf.kill()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="session")
def s3_cluster(s3_client):  # type: ignore[no-untyped-def]
    from sk8s.cluster import Cluster
    from sk8s.config import load_config

    cfg = load_config(
        image=os.environ.get("SK8S_TEST_IMAGE", "sk8s-dev-stores:latest"),
        pull_policy="Never",
        ttl_seconds_after_finished=30,
        store=f"s3://{BUCKET}/{PREFIX}",
        store_endpoint=f"http://minio.default.svc.cluster.local:{minio.PORT}",
        store_env={
            "AWS_ACCESS_KEY_ID": minio.ACCESS_KEY,
            "AWS_SECRET_ACCESS_KEY": minio.SECRET_KEY,
        },
    )
    c = Cluster(cfg)
    yield c
    c.close()


def test_run_roundtrip_over_s3(s3_cluster) -> None:  # type: ignore[no-untyped-def]
    assert s3_cluster.run(lambda x: x * 2, 21) == 42


def test_large_result_roundtrip(s3_cluster) -> None:  # type: ignore[no-untyped-def]
    size = 2 * 1024 * 1024  # over the ConfigMap store's ~1MiB ceiling
    result = s3_cluster.run(lambda n: b"\xab" * n, size)
    assert isinstance(result, bytes) and len(result) == size
    assert result == b"\xab" * size


def test_remote_exception_over_s3(s3_cluster) -> None:  # type: ignore[no-untyped-def]
    def boom() -> None:
        raise KeyError("remote key")

    fut = s3_cluster.submit(boom)
    with pytest.raises(KeyError) as exc_info:
        fut.result(timeout=240)
    assert isinstance(exc_info.value.__cause__, RemoteTraceback)


def test_no_objects_left_behind(s3_cluster, s3_client) -> None:  # type: ignore[no-untyped-def]
    s3_cluster.run(lambda: "done")
    prefix = f"{PREFIX}{s3_cluster.workflow_id}"
    # Blob deletion completes shortly *after* result delivery, so poll.
    deadline = time.monotonic() + 10
    keys: list[str] = []
    while time.monotonic() < deadline:
        resp = s3_client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        if not keys:
            break
        time.sleep(0.25)
    assert keys == []
