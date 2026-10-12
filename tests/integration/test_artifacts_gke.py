"""ONE end-to-end artifact run on GKE: gs:// input -> staged -> function -> OutDir -> CAS ->
publish -> memo re-run with zero jobs.

Needs a worker image built from THIS branch (py3.14, with the gcs extra) pushed under its own tag;
never the shared one::

    docker build -f docker/Dockerfile.dev --build-arg EXTRAS=s3,gcs -t $IMG . && docker push $IMG

Run with ``SK8S_ARTIFACTS_GKE_IMAGE=$IMG SK8S_TEST_PULL_POLICY=IfNotPresent
SK8S_STORE=gs://<bucket>/sk8s/ SK8S_NAMESPACE=sk8s KUBECONFIG=.kubeconfig-gke
SK8S_CONFIG=<isolated toml> SK8S_TEST_TIMEOUT_SCALE=3``. Everything it creates is named
``spike-artifacts-*`` under the store's bucket and is removed afterwards (the test's CAS keys,
index entries, memo entry, workflow reference sets and anchor included).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import sk8s
from sk8s import cas
from sk8s.cas import ArtifactStore
from sk8s.cluster import Cluster
from sk8s.config import load_config
from sk8s.store.gcs import GCSStore
from tests.integration.conftest import requires_cluster, suite_namespace, suite_pull_policy

STORE_URL = os.environ.get("SK8S_STORE", "")
IMAGE = os.environ.get("SK8S_ARTIFACTS_GKE_IMAGE", "")

pytestmark = [
    pytest.mark.integration,
    requires_cluster,
    pytest.mark.skipif(not STORE_URL.startswith("gs://"), reason="SK8S_STORE is not a gs:// URL"),
    pytest.mark.skipif(not IMAGE, reason="set SK8S_ARTIFACTS_GKE_IMAGE to the branch worker image"),
    pytest.mark.timeout(900),
]

BUCKET = urlsplit(STORE_URL).netloc


def jobs_in(c: Cluster) -> set[str]:
    return {j.metadata.name for j in c.batch.list_namespaced_job(namespace=c.config.namespace).items}


def test_gcs_input_to_published_output_with_memo(tmp_path: Path) -> None:
    rid = secrets.token_hex(4)
    gcs = GCSStore(BUCKET)  # whole-bucket handle for the user-visible locations
    in_uri = f"gs://{BUCKET}/spike-artifacts-in/{rid}/s1.reads.txt"
    out_uri = f"gs://{BUCKET}/spike-artifacts-out/{rid}/s1.counts.txt"
    payload = b"".join(f"read-{i}\tACGTACGTAC\n".encode() for i in range(40_000))  # ~0.8 MiB
    src = tmp_path / "s1.reads.txt"
    src.write_bytes(payload)
    gcs.upload_file(f"spike-artifacts-in/{rid}/s1.reads.txt", src)
    in_sha = hashlib.sha256(payload).hexdigest()

    cluster = Cluster(load_config(
        namespace=suite_namespace(), image=IMAGE, pull_policy=suite_pull_policy(),
        ttl_seconds_after_finished=60,
    ))
    astore = cluster.artifact_store
    touched: dict[str, str] = {}  # what to clean afterwards
    try:
        ref = sk8s.artifact(in_uri)

        def count(reads: Path, out: sk8s.OutDir) -> Path:
            n = sum(1 for _ in reads.open("rb"))  # a REAL local file in the pod
            (out / "s1.counts.txt").write_text(f"{reads.name}\t{n}\n")
            return out / "s1.counts.txt"

        t0 = time.monotonic()
        first = cluster.submit(count, ref, memo=True)
        job = cluster.batch.read_namespaced_job(name=first.job_name, namespace=cluster.config.namespace)
        requested = job.spec.template.spec.containers[0].resources.requests
        result = first.result(timeout=600)
        wall = time.monotonic() - t0
        wf = cluster.workflow_id
        touched["wf"] = wf

        # 1. staged in: the function saw the bytes (it counted 40000 lines) and the hash was learned
        assert result.download(tmp_path / "counts.txt").read_text() == "s1.reads.txt\t40000\n"
        in_idx = astore.read_index(in_sha)
        assert in_idx is not None and in_idx["locations"][0]["kind"] == "external"
        assert in_idx["locations"][0]["uri"] == in_uri and in_idx["locations"][0]["version"].isdigit()

        # 2. the output is in the CAS (hashed while uploading), with lineage back to the input
        out_sha = result.sha256 or ""
        touched["out_sha"] = out_sha
        assert result.uri == f"{STORE_URL.rstrip('/')}/{cas.cas_key(out_sha)}"
        out_idx = astore.read_index(out_sha)
        assert out_idx is not None
        assert out_idx["provenance"] == {
            "created_at": out_idx["provenance"]["created_at"],
            "inputs": [in_sha], "produced_by": result.produced_by,
        }

        # 3. publish: server-side rewrite, metadata lands with the content, CAS key retired
        pub = result.publish(out_uri)
        st = gcs.stat(out_uri)
        assert st is not None and st.size == result.size and pub.version == st.version
        assert st.metadata == {
            "sk8s-artifact-sha256": out_sha, "sk8s-produced-by": result.produced_by,
        }
        assert hashlib.sha256((tmp_path / "counts.txt").read_bytes()).hexdigest() == out_sha
        assert astore.store.stat(result.uri) is None
        moved = astore.read_index(out_sha)
        assert moved is not None and [(loc["kind"], loc["uri"]) for loc in moved["locations"]] == [("published", out_uri)]

        # 4. memo re-run: zero new jobs (the output now lives at its published location)
        jobs_before = jobs_in(cluster)
        t1 = time.monotonic()
        second = cluster.submit(count, sk8s.artifact(in_uri), memo=True)
        memo_wall = time.monotonic() - t1
        assert second.memo_hit and second.result() == result
        assert jobs_in(cluster) == jobs_before

        # evidence for the report
        pod = cluster.core.list_namespaced_pod(
            namespace=cluster.config.namespace, label_selector=f"job-name={first.job_name}"
        ).items
        print("\nGKE EVIDENCE")
        print("  image            :", IMAGE)
        print("  workflow / job   :", wf, first.job_name)
        print("  requested        :", requested)
        if pod:
            print("  pod resources    :", pod[0].spec.containers[0].resources.requests,
                  "/", pod[0].spec.containers[0].resources.limits)
            print("  pod node         :", pod[0].spec.node_name)
        print(f"  first run        : {wall:.1f}s wall; memo re-run {memo_wall * 1000:.0f} ms, jobs unchanged ({len(jobs_before)})")
        print("  input pin        :", in_idx["locations"][0])
        print("  output           :", out_sha, result.size, "bytes ->", out_uri)
        print("  object metadata  :", st.metadata)
    finally:
        # Teardown: every key this test created, then prove the prefixes are empty.
        cleaned: list[str] = []
        try:
            for blob in cluster.artifact_store.store.list_blobs("artifacts/memo/"):
                entry = cluster.artifact_store.store.get(cluster.artifact_store.store.uri_for(blob.key))
                if touched.get("out_sha", "~").encode() in entry:
                    cluster.artifact_store.store.delete(cluster.artifact_store.store.uri_for(blob.key))
                    cleaned.append(blob.key)
            for sha in (in_sha, touched.get("out_sha")):
                if sha:
                    for key in (cas.index_key(sha), cas.cas_key(sha)):
                        cluster.artifact_store.store.delete(cluster.artifact_store.store.uri_for(key))
                        cleaned.append(key)
        finally:
            cluster.close(delete_workflow=True)  # anchor + <wf>/ blobs + workflows/<wf>/ reference sets
            gcs.delete_prefix(f"spike-artifacts-in/{rid}")
            gcs.delete_prefix(f"spike-artifacts-out/{rid}")
        leftovers = [b.name for p in (f"spike-artifacts-in/{rid}", f"spike-artifacts-out/{rid}")
                     for b in gcs._client.list_blobs(BUCKET, prefix=p)]
        wf_left = [b.key for b in cluster.artifact_store.store.list_blobs(f"workflows/{touched.get('wf', 'none')}/")]
        print("  teardown         : removed", len(cleaned), "store keys; leftovers:", leftovers, wf_left)
        assert leftovers == [] and wf_left == []
