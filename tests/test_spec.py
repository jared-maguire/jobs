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


def _env(job) -> dict:  # type: ignore[no-untyped-def]
    return {e.name: e.value for e in job.spec.template.spec.containers[0].env}


def test_s3_endpoint_exported_only_when_configured() -> None:
    assert "SK8S_S3_ENDPOINT" not in _env(make())
    assert "SK8S_S3_ENDPOINT" not in _env(make(store_endpoint=None))
    assert _env(make(store_endpoint="http://minio:9000"))["SK8S_S3_ENDPOINT"] == "http://minio:9000"


def test_store_env_exported_to_worker() -> None:
    assert "AWS_ACCESS_KEY_ID" not in _env(make())
    env = _env(make(store_env={"AWS_ACCESS_KEY_ID": "k", "AWS_SECRET_ACCESS_KEY": "s"}))
    assert env["AWS_ACCESS_KEY_ID"] == "k"
    assert env["AWS_SECRET_ACCESS_KEY"] == "s"
    assert env["SK8S_STORE"] == "k8s"  # core vars untouched
