"""The logical-volume mount API: ``volumes=`` syntax, default read-only, and the built Job."""
from __future__ import annotations

from typing import Any

import pytest
from kubernetes import client

from sk8s import volumes
from sk8s.cluster import Cluster
from sk8s.config import Config
from sk8s.spec import PodMount, build_job
from sk8s.store.local import LocalStore
from tests.fakes import FakeBatchV1, FakeCoreV1

NS = "ns1"
M = volumes.Mount


# --- parse_mounts: the ruled syntax ---------------------------------------------------------------


def test_str_is_one_read_only_mount_at_mnt_name() -> None:
    assert volumes.parse_mounts("data") == [M("data", "ro", "/mnt/data")]


def test_list_is_many_read_only_mounts() -> None:
    assert volumes.parse_mounts(["a", "b"]) == [M("a", "ro", "/mnt/a"), M("b", "ro", "/mnt/b")]
    assert volumes.parse_mounts(("a",)) == [M("a", "ro", "/mnt/a")]


def test_dict_inner_keys_are_both_optional_with_ro_and_mnt_defaults() -> None:
    got = volumes.parse_mounts({
        "a": {},
        "b": {"access_mode": "rw"},
        "c": {"mountpoint": "/data/c"},
        "d": {"access_mode": "rw", "mountpoint": "/w"},
        "e": {"access_mode": "ro"},
    })
    assert got == [
        M("a", "ro", "/mnt/a"),
        M("b", "rw", "/mnt/b"),
        M("c", "ro", "/data/c"),
        M("d", "rw", "/w"),
        M("e", "ro", "/mnt/e"),
    ]


@pytest.mark.parametrize("empty", [None, {}, [], ()])
def test_nothing_mounts_nothing(empty: Any) -> None:
    assert volumes.parse_mounts(empty) == []


def test_unknown_inner_keys_are_a_clear_value_error() -> None:
    with pytest.raises(ValueError) as e:
        volumes.parse_mounts({"data": {"access_mode": "rw", "path": "/x", "mode": "ro"}})
    msg = str(e.value)
    assert "data" in msg and "path" in msg and "mode" in msg
    assert "access_mode" in msg and "mountpoint" in msg  # names what IS allowed


@pytest.mark.parametrize("bad", ["rwx", "RW", "read-only", "", None, True, "ReadWriteOnce"])
def test_bad_access_mode_is_a_clear_value_error(bad: Any) -> None:
    with pytest.raises(ValueError) as e:
        volumes.parse_mounts({"data": {"access_mode": bad}})
    assert "access_mode" in str(e.value) and "'rw'" in str(e.value) and "'ro'" in str(e.value)


@pytest.mark.parametrize("bad", ["", "relative/path", None, 7])
def test_bad_mountpoint_is_a_clear_value_error(bad: Any) -> None:
    with pytest.raises(ValueError, match="mountpoint"):
        volumes.parse_mounts({"data": {"mountpoint": bad}})


def test_the_old_name_to_path_dict_form_is_rejected_naming_the_new_form() -> None:
    with pytest.raises(ValueError) as e:
        volumes.parse_mounts({"data": "/mnt/data"})
    msg = str(e.value)
    assert "'data'" in msg and "/mnt/data" in msg
    assert '"mountpoint"' in msg and '"access_mode"' in msg  # shows the replacement
    assert "read-only" in msg  # and that the default changed


def test_non_dict_inner_value_is_rejected() -> None:
    with pytest.raises(ValueError):
        volumes.parse_mounts({"data": ["rw"]})
    with pytest.raises(ValueError):
        volumes.parse_mounts({"data": None})


@pytest.mark.parametrize("bad", [3, 1.5, {"a"}, b"x"])
def test_wrong_top_level_type_is_rejected(bad: Any) -> None:
    with pytest.raises(ValueError, match="volumes"):
        volumes.parse_mounts(bad)


def test_list_entries_must_be_names_and_unique() -> None:
    with pytest.raises(ValueError):
        volumes.parse_mounts(["a", 3])
    with pytest.raises(ValueError, match="more than once"):
        volumes.parse_mounts(["a", "a"])
    with pytest.raises(ValueError):
        volumes.parse_mounts("")


def test_two_volumes_cannot_share_a_mountpoint() -> None:
    with pytest.raises(ValueError, match="mountpoint"):
        volumes.parse_mounts({"a": {"mountpoint": "/x"}, "b": {"mountpoint": "/x"}})


# --- build_job: readOnly on BOTH the volume and the mount ------------------------------------------


def _job(mounts: list[PodMount], **over: Any) -> client.V1Job:
    args: dict[str, Any] = dict(
        image="img:1", namespace=NS, workflow_id="wf-x", payload_uri="k8s://ns1/p",
        result_key="wf-x/job-abc123/result", store="k8s", service_account="sk8s",
        pull_policy="IfNotPresent", requests={"cpu": "1"}, limits={}, volumes=mounts,
        backoff_limit=0, ttl=3600, privileged=False,
    )
    args.update(over)
    return build_job("job-abc123", **args)


def _body(job: client.V1Job) -> dict[str, Any]:
    out = client.ApiClient().sanitize_for_serialization(job)
    assert isinstance(out, dict)
    return out


def test_ro_mount_sets_read_only_on_both_the_volume_and_the_volume_mount() -> None:
    job = _job([PodMount(name="idx", claim="idx", mountpoint="/mnt/idx", read_only=True)])
    pod = job.spec.template.spec
    assert pod.volumes[0].persistent_volume_claim.read_only is True
    assert pod.containers[0].volume_mounts[0].read_only is True


def test_ro_mount_of_a_clone_keeps_the_logical_name_for_the_pod_volume() -> None:
    job = _job([PodMount(name="idx", claim="idx-ro-abc", mountpoint="/mnt/idx", read_only=True)])
    pod = job.spec.template.spec
    assert pod.volumes[0].name == "idx" and pod.volumes[0].persistent_volume_claim.claim_name == "idx-ro-abc"
    assert pod.containers[0].volume_mounts[0].name == "idx"


def test_rw_mount_has_no_read_only_field_at_all() -> None:
    job = _job([PodMount(name="a", claim="a", mountpoint="/mnt/a", read_only=False)])
    body = _body(job)["spec"]["template"]["spec"]
    assert "readOnly" not in body["volumes"][0]["persistentVolumeClaim"]
    assert "readOnly" not in body["containers"][0]["volumeMounts"][0]


def test_rw_mount_job_is_byte_identical_to_the_pre_branch_job() -> None:
    """RF2: the whole built Job for rw mounts equals the body the pre-branch builder produced.

    ``BASELINE`` was generated by running the b39f882 ``build_job`` with
    ``volumes={"a": "/mnt/a", "b": "/custom/b"}`` and serializing the result.
    """
    job = _job([
        PodMount(name="a", claim="a", mountpoint="/mnt/a", read_only=False),
        PodMount(name="b", claim="b", mountpoint="/custom/b", read_only=False),
    ])
    assert _body(job) == BASELINE


def test_no_mounts_leaves_volumes_unset() -> None:
    pod = _job([]).spec.template.spec
    assert pod.volumes is None and pod.containers[0].volume_mounts is None


# --- end to end through Cluster.submit --------------------------------------------------------------


class H:
    def __init__(self, tmp_path: Any) -> None:
        self.batch = FakeBatchV1()
        self.core = FakeCoreV1()
        self.cluster = Cluster(
            Config(namespace=NS, image="sk8s:dev", store=f"local:{tmp_path}"),
            batch_api=self.batch, core_api=self.core, store=LocalStore(tmp_path),
            watcher_events=lambda: iter(()),
        )

    def make_volume(self, name: str) -> None:
        self.core.pvcs[(NS, name)] = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=name, namespace=NS),
            spec=client.V1PersistentVolumeClaimSpec(access_modes=["ReadWriteOnce"]),
        )

    def pod(self, fut: Any) -> Any:
        return self.batch.jobs[(NS, fut.job_name)].spec.template.spec


@pytest.fixture
def h(tmp_path):  # type: ignore[no-untyped-def]
    harness = H(tmp_path)
    yield harness
    harness.cluster.close()


def test_submit_default_is_a_read_only_mount(h: H) -> None:
    h.make_volume("data")
    pod = h.pod(h.cluster.submit(lambda: 1, volumes="data"))
    assert pod.volumes[0].persistent_volume_claim.read_only is True
    assert pod.containers[0].volume_mounts[0].read_only is True
    assert pod.containers[0].volume_mounts[0].mount_path == "/mnt/data"


def test_submit_rw_dict_form_writes_and_honours_the_mountpoint(h: H) -> None:
    h.make_volume("data")
    fut = h.cluster.submit(
        lambda: 1, volumes={"data": {"access_mode": "rw", "mountpoint": "/work"}}
    )
    pod = h.pod(fut)
    assert pod.volumes[0].persistent_volume_claim.read_only is None
    mount = pod.containers[0].volume_mounts[0]
    assert mount.read_only is None and mount.mount_path == "/work"


def test_submit_mixed_modes_in_one_job(h: H) -> None:
    h.make_volume("a")
    h.make_volume("b")
    pod = h.pod(h.cluster.submit(lambda: 1, volumes={"a": {"access_mode": "rw"}, "b": {}}))
    ro = {m.name: m.read_only for m in pod.containers[0].volume_mounts}
    assert ro == {"a": None, "b": True}


def test_submit_rejects_the_old_dict_form_before_creating_anything(h: H) -> None:
    with pytest.raises(ValueError, match="mountpoint"):
        h.cluster.submit(lambda: 1, volumes={"data": "/mnt/data"})
    assert not h.batch.jobs and not h.core.configmaps  # no anchor, no job, no payload


def test_submit_validates_before_any_api_call_on_bad_access_mode(h: H) -> None:
    with pytest.raises(ValueError, match="access_mode"):
        h.cluster.submit(lambda: 1, volumes={"data": {"access_mode": "rwx"}})
    assert not h.batch.jobs


def test_workflow_driver_and_nested_env_do_not_carry_volumes(h: H) -> None:
    """Volumes are per-job: nothing about them is exported into the pod's SK8S_* env."""
    h.make_volume("data")
    pod = h.pod(h.cluster.submit(lambda: 1, volumes={"data": {"access_mode": "rw"}}))
    names = {e.name for e in pod.containers[0].env}
    assert not any("VOLUME" in n or "MOUNT" in n for n in names)


BASELINE: dict[str, Any] = {
    'apiVersion': 'batch/v1',
    'kind': 'Job',
    'metadata': {
        'labels': {'app.kubernetes.io/managed-by': 'sk8s', 'sk8s/workflow-id': 'wf-x'},
        'name': 'job-abc123',
        'namespace': 'ns1',
    },
    'spec': {
        'backoffLimit': 0,
        'template': {
            'metadata': {
                'labels': {'app.kubernetes.io/managed-by': 'sk8s', 'sk8s/workflow-id': 'wf-x'}
            },
            'spec': {
                'containers': [{
                    'command': ['python', '-m', 'sk8s.worker'],
                    'env': [
                        {'name': 'SK8S_STORE', 'value': 'k8s'},
                        {'name': 'SK8S_NAMESPACE', 'value': 'ns1'},
                        {'name': 'SK8S_PAYLOAD_URI', 'value': 'k8s://ns1/p'},
                        {'name': 'SK8S_RESULT_KEY', 'value': 'wf-x/job-abc123/result'},
                        {'name': 'SK8S_WORKFLOW_ID', 'value': 'wf-x'},
                        {'name': 'SK8S_IMAGE', 'value': 'img:1'},
                        {'name': 'SK8S_PULL_POLICY', 'value': 'IfNotPresent'},
                        {'name': 'SK8S_SERVICE_ACCOUNT', 'value': 'sk8s'},
                    ],
                    'image': 'img:1',
                    'imagePullPolicy': 'IfNotPresent',
                    'name': 'worker',
                    'resources': {'requests': {'cpu': '1'}},
                    'volumeMounts': [
                        {'mountPath': '/mnt/a', 'name': 'a'},
                        {'mountPath': '/custom/b', 'name': 'b'},
                    ],
                }],
                'restartPolicy': 'Never',
                'serviceAccountName': 'sk8s',
                'volumes': [
                    {'name': 'a', 'persistentVolumeClaim': {'claimName': 'a'}},
                    {'name': 'b', 'persistentVolumeClaim': {'claimName': 'b'}},
                ],
            },
        },
        'ttlSecondsAfterFinished': 3600,
    },
}
