"""Conditional-write primitives on all four stores, plus the shared bounded-retry helper.

The same contract runs against LocalStore, the ConfigMap store (fake k8s API), GCS and S3
(dict-backed fakes that model the verified/documented precondition semantics).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from sk8s.errors import BlobNotFound, RetryExhausted
from sk8s.store.base import BlobStore
from sk8s.store.configmap import ConfigMapStore
from sk8s.store.gcs import GCSStore
from sk8s.store.local import LocalStore
from sk8s.store.retry import backoff_delay, retry_conflicts, update_blob
from sk8s.store.s3 import S3Store
from tests.fakes import FakeCoreV1
from tests.fakes_gcs import FakeGCSClient
from tests.fakes_s3 import FakeS3Client

KEY = "artifacts/index/sha256/ab/abcdef.json"


@pytest.fixture(params=["local", "configmap", "gcs", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> BlobStore:
    if request.param == "local":
        return LocalStore(tmp_path)
    if request.param == "configmap":
        return ConfigMapStore(namespace="ns1", core_api=FakeCoreV1())
    if request.param == "gcs":
        return GCSStore("bkt", "pre", client=FakeGCSClient())
    return S3Store("bkt", "pre", client=FakeS3Client())


def test_put_if_absent_creates_then_refuses(store: BlobStore) -> None:
    assert store.put_if_absent(KEY, b"first") is True
    assert store.put_if_absent(KEY, b"second") is False
    assert store.get(store.uri_for(KEY)) == b"first"  # the loser never overwrote


def test_put_if_absent_never_clobbers_a_plain_put(store: BlobStore) -> None:
    store.put(KEY, b"plain")
    assert store.put_if_absent(KEY, b"x") is False
    assert store.get(store.uri_for(KEY)) == b"plain"


def test_read_with_version_missing_raises(store: BlobStore) -> None:
    with pytest.raises(BlobNotFound):
        store.read_with_version(KEY)


def test_read_with_version_returns_bytes_and_str_token(store: BlobStore) -> None:
    store.put_if_absent(KEY, b"v1")
    data, version = store.read_with_version(KEY)
    assert data == b"v1"
    assert isinstance(version, str) and version


def test_put_if_version_swaps_on_current_version(store: BlobStore) -> None:
    store.put_if_absent(KEY, b"v1")
    _, v1 = store.read_with_version(KEY)
    assert store.put_if_version(KEY, b"v2", v1) is True
    data, v2 = store.read_with_version(KEY)
    assert data == b"v2" and v2 != v1


def test_put_if_version_rejects_stale_token(store: BlobStore) -> None:
    store.put_if_absent(KEY, b"v1")
    _, v1 = store.read_with_version(KEY)
    assert store.put_if_version(KEY, b"interloper", v1) is True
    assert store.put_if_version(KEY, b"stale-writer", v1) is False
    assert store.read_with_version(KEY)[0] == b"interloper"


def test_put_if_version_on_missing_key_is_false_not_a_create(store: BlobStore) -> None:
    assert store.put_if_version(KEY, b"x", "whatever-1") is False
    with pytest.raises(BlobNotFound):
        store.read_with_version(KEY)


def test_two_way_create_race_has_exactly_one_winner(store: BlobStore) -> None:
    for trial in range(10):
        key = f"{KEY}.{trial}"
        gate = threading.Barrier(2)
        wins: list[bool] = []

        def contend(i: int) -> None:
            gate.wait()
            wins.append(store.put_if_absent(key, f"writer-{i}".encode()))

        threads = [threading.Thread(target=contend, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(wins) == [False, True], f"trial {trial}: {wins}"
        winner = store.read_with_version(key)[0]
        assert winner in (b"writer-0", b"writer-1")


def _add_member(member: str) -> Callable[[bytes | None], bytes | None]:
    """An idempotent set-merge mutation (the only kind update_blob should be given)."""

    def mutate(current: bytes | None) -> bytes | None:
        members = set(json.loads(current)) if current else set()
        if member in members:
            return None
        return json.dumps(sorted(members | {member})).encode()

    return mutate


def test_update_blob_creates_then_merges(store: BlobStore) -> None:
    update_blob(store, KEY, _add_member("a"))
    update_blob(store, KEY, _add_member("b"))
    update_blob(store, KEY, _add_member("a"))  # already present: no write needed
    assert json.loads(store.read_with_version(KEY)[0]) == ["a", "b"]


def test_ten_way_contended_update_loses_nothing(store: BlobStore) -> None:
    gate = threading.Barrier(10)
    errors: list[BaseException] = []

    def contend(i: int) -> None:
        gate.wait()
        try:
            update_blob(store, KEY, _add_member(f"m{i}"), sleep=lambda _: None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert json.loads(store.read_with_version(KEY)[0]) == sorted(f"m{i}" for i in range(10))


# --- backend specifics ------------------------------------------------------------


def test_local_put_if_version_serialises_across_processes(tmp_path: Path) -> None:
    import multiprocessing

    s = LocalStore(tmp_path)
    s.put_if_absent(KEY, b"[]")
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_proc_add, args=(str(tmp_path), f"p{i}")) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    assert json.loads(s.read_with_version(KEY)[0]) == ["p0", "p1", "p2", "p3"]


def _proc_add(root: str, member: str) -> None:
    update_blob(LocalStore(root), KEY, _add_member(member))


def test_local_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    s.put_if_absent(KEY, b"a")
    s.put_if_absent(KEY, b"b")  # loser
    update_blob(s, KEY, lambda cur: b"c")
    left = [p.name for p in (tmp_path / KEY).parent.iterdir()]
    assert left == [Path(KEY).name]


def test_configmap_put_if_version_sends_a_resource_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec Appendix B.1 (ConfigMap): a patch WITHOUT metadata.resourceVersion is a blind write. Never send one."""
    core = FakeCoreV1()
    s = ConfigMapStore("ns1", core)
    s.put_if_absent(KEY, b"v1")
    bodies: list[dict[str, Any]] = []
    real = core.patch_namespaced_config_map

    def spy(**kw: Any) -> Any:
        bodies.append(kw["body"])
        return real(**kw)

    monkeypatch.setattr(core, "patch_namespaced_config_map", spy)
    _, version = s.read_with_version(KEY)
    s.put_if_version(KEY, b"v2", version)
    assert bodies[0]["metadata"]["resourceVersion"] == version


def test_configmap_conditional_never_falls_back_to_delete_recreate() -> None:
    core = FakeCoreV1()
    s = ConfigMapStore("ns1", core)
    s.put_if_absent(KEY, b"v1")
    assert s.put_if_absent(KEY, b"v2") is False
    _, v = s.read_with_version(KEY)
    s.put_if_version(KEY, b"v3", v)
    assert core.delete_policies == {}  # nothing was deleted along the way


def test_configmap_oversize_conditional_write_is_rejected() -> None:
    from sk8s.errors import BlobTooLarge
    from sk8s.store.configmap import MAX_CONFIGMAP_BLOB

    s = ConfigMapStore("ns1", FakeCoreV1())
    with pytest.raises(BlobTooLarge):
        s.put_if_absent(KEY, b"x" * (MAX_CONFIGMAP_BLOB + 1))


def test_gcs_sends_generation_preconditions(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = __import__("tests.fakes_gcs", fromlist=["FakeBlob"]).FakeBlob.upload_from_string

    def spy(self: Any, data: Any, *a: Any, **kw: Any) -> None:
        seen.append(kw.get("if_generation_match"))
        real(self, data, *a, **kw)

    monkeypatch.setattr("tests.fakes_gcs.FakeBlob.upload_from_string", spy)
    s = GCSStore("bkt", client=FakeGCSClient())
    s.put_if_absent("k", b"a")
    _, gen = s.read_with_version("k")
    s.put_if_version("k", b"b", gen)
    assert seen == [0, int(gen)]


def test_gcs_other_errors_propagate() -> None:
    from google.api_core.exceptions import Forbidden

    class Denied(FakeGCSClient):
        def bucket(self, name: str) -> Any:
            b = super().bucket(name)
            real = b.blob

            def blob(n: str) -> Any:
                x = real(n)

                def boom(*a: Any, **k: Any) -> None:
                    raise Forbidden("nope")

                x.upload_from_string = boom
                return x

            b.blob = blob
            return b

    with pytest.raises(Forbidden):
        GCSStore("bkt", client=Denied()).put_if_absent("k", b"a")


def test_s3_uses_if_none_match_and_if_match() -> None:
    fake = FakeS3Client()
    seen: list[dict[str, Any]] = []
    real = fake.put_object

    def spy(**kw: Any) -> Any:
        seen.append({k: v for k, v in kw.items() if k in ("IfNoneMatch", "IfMatch")})
        return real(**kw)

    fake.put_object = spy  # type: ignore[method-assign]
    s = S3Store("bkt", client=fake)
    s.put_if_absent("k", b"a")
    _, etag = s.read_with_version("k")
    s.put_if_version("k", b"b", etag)
    assert seen == [{"IfNoneMatch": "*"}, {"IfMatch": etag}]


def test_s3_retries_a_409_conditional_request_conflict_then_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeS3Client()
    s = S3Store("bkt", client=fake)
    fake.inject_conflicts = 2  # two in-flight-write conflicts, then a clean answer
    slept: list[float] = []
    monkeypatch.setattr("sk8s.store.retry.time.sleep", slept.append)
    assert s.put_if_absent("k", b"a") is True
    assert len(slept) == 2 and fake.objects[("bkt", "k")] == b"a"


def test_s3_persistent_409_gives_up_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3Client()
    s = S3Store("bkt", client=fake)
    fake.inject_conflicts = 10_000
    monkeypatch.setattr("sk8s.store.retry.time.sleep", lambda _: None)
    with pytest.raises(RetryExhausted):
        s.put_if_absent("k", b"a")


def test_s3_if_match_on_a_missing_key_is_a_false() -> None:
    assert S3Store("bkt", client=FakeS3Client()).put_if_version("k", b"a", '"etag"') is False


# --- the shared retry helper -------------------------------------------------------


def test_backoff_is_full_jitter_and_capped() -> None:
    assert backoff_delay(0, base=0.05, cap=2.0, rng=lambda lo, hi: hi) == 0.05
    assert backoff_delay(3, base=0.05, cap=2.0, rng=lambda lo, hi: hi) == 0.4
    assert backoff_delay(30, base=0.05, cap=2.0, rng=lambda lo, hi: hi) == 2.0
    assert backoff_delay(5, rng=lambda lo, hi: lo) == 0.0  # jitter spans [0, bound]


def test_retry_conflicts_is_bounded() -> None:
    calls = 0
    slept: list[float] = []

    def always_lose() -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(RetryExhausted):
        retry_conflicts(always_lose, what="x", max_attempts=5, sleep=slept.append)
    assert calls == 5 and len(slept) == 4  # no sleep after the last loss


def test_retry_conflicts_returns_first_win_even_if_falsy() -> None:
    results = iter([None, None, False])
    assert retry_conflicts(lambda: next(results), what="x", sleep=lambda _: None) is False


def test_update_blob_gives_up_instead_of_spinning(tmp_path: Path) -> None:
    class AlwaysStale(LocalStore):
        def put_if_version(self, key: str, data: bytes, version: str) -> bool:
            return False

    s = AlwaysStale(tmp_path)
    s.put_if_absent(KEY, b"[]")
    with pytest.raises(RetryExhausted):
        update_blob(s, KEY, _add_member("a"), max_attempts=4, sleep=lambda _: None)


def test_update_blob_noop_mutation_writes_nothing(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    s.put_if_absent(KEY, b'["a"]')
    before = s.read_with_version(KEY)
    update_blob(s, KEY, _add_member("a"))
    assert s.read_with_version(KEY) == before


def test_update_blob_survives_the_key_being_deleted_mid_loop(tmp_path: Path) -> None:
    class Vanishing(LocalStore):
        n = 0

        def put_if_version(self, key: str, data: bytes, version: str) -> bool:
            self.n += 1
            if self.n == 1:
                self.delete(self.uri_for(key))  # gone between our read and write
            return super().put_if_version(key, data, version)

    s = Vanishing(tmp_path)
    s.put_if_absent(KEY, b'["a"]')
    update_blob(s, KEY, _add_member("b"), sleep=lambda _: None)
    assert json.loads(s.read_with_version(KEY)[0]) == ["b"]  # recreated from absence
