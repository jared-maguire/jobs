"""Multi-client stress test: six client PROCESSES hammer one namespace for ~3 minutes.

It is the "30 users on one cluster" scenario in miniature and proves the isolation
invariants under concurrent load:

* two batch clients      - ``sk8s.map`` / ``starmap`` with ``batch_size``; exact results
* two workflow clients   - ``submit_workflow`` -> ``attach`` -> ``result``; one of them also
                           owns a ``keep=True`` volume that must survive everything
* one churn client       - submits sleepers, cancels half, kills a detached workflow
                           mid-run and checks the kill cascades
* one hygiene client     - loops ``sk8s.clean()`` and asserts it NEVER returns a workflow id
                           that is registered as live; it also holds a backdated-but-live
                           workflow (must survive every sweep) and a backdated-and-idle one
                           (a positive control: ``clean`` must be *able* to delete)

Every client registers its workflow ids in a shared ``live`` dict before it submits and
unregisters them just before any deliberate deletion, so a violation is "an id that was
live before AND after a ``clean()`` call came back in its result". The parent then checks
that every client exited 0 (all per-client failures are printed, not only the first) and
sweeps each workflow id by label for leftovers, with a bounded poll for lingering objects.

Needs the worker image built from THIS branch (the in-pod worker runs this branch's code)::

    minikube image build -t sk8s-dev-cidocs:latest -f docker/Dockerfile.dev .
    SK8S_INTEGRATION=1 .venv/bin/pytest tests/integration/test_stress.py -v

Override the tag with ``SK8S_STRESS_TEST_IMAGE``. Excluded from the per-PR CI run
(``-m "integration and not stress"``).
"""
from __future__ import annotations

import concurrent.futures
import datetime
import multiprocessing
import os
import secrets
import sys
import time
import traceback
from typing import Any, Callable, NamedTuple

import pytest

from sk8s import anchors
from sk8s.cluster import Cluster
from sk8s.config import load_config
from tests.integration.conftest import requires_cluster

pytestmark = [
    pytest.mark.integration, pytest.mark.stress, requires_cluster, pytest.mark.timeout(900),
]

IMAGE = os.environ.get("SK8S_STRESS_TEST_IMAGE", "sk8s-dev-cidocs:latest")
NS = "default"
TTL = 20  # modest ttl_seconds_after_finished
WORK_SECONDS = 150  # clients start new rounds until this long after launch (~3 min total)
HARD_SECONDS = 330  # shared hard deadline: every wait in every client is bounded by it
SWEEP_SECONDS = 120  # parent's bounded poll for lingering objects after the clients exit


class Shared(NamedTuple):
    """Cross-process state (Manager proxies + wall-clock deadlines)."""

    live: Any  # dict: workflow id -> client name, for ids that must NOT be cleaned
    ledger: Any  # list of (client, workflow id) for every workflow any client ever used
    stats: Any  # dict: client -> completed rounds
    errors: Any  # list of (client, traceback text)
    token: str  # unique per run (kept-volume name)
    work_end: float
    hard_end: float


def _make_cluster() -> Cluster:
    return Cluster(
        load_config(namespace=NS, image=IMAGE, pull_policy="Never", ttl_seconds_after_finished=TTL)
    )


def _register(sh: Shared, name: str, c: Cluster, *, live: bool = True) -> str:
    wf = c.workflow_id
    sh.ledger.append((name, wf))
    if live:
        sh.live[wf] = name
    return wf


def _budget(sh: Shared, cap: float) -> float:
    """A wait length bounded by both ``cap`` and the shared hard deadline."""
    left = sh.hard_end - time.time()
    if left <= 1:
        raise TimeoutError("shared stress deadline exceeded")
    return min(cap, left)


def _more_work(sh: Shared, rounds: int, reserve: float = 0.0) -> bool:
    return rounds == 0 or time.time() < sh.work_end - reserve


def _poll(cond: Callable[[], Any], timeout: float, what: str, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = cond()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last={last!r})")


def _labeled(c: Cluster, wf: str) -> dict[str, list[str]]:
    """Every object that carries workflow ``wf``'s label (anchor, blobs, jobs, pods, PVCs)."""
    sel = f"{anchors.WORKFLOW_LABEL}={wf}"
    ns = c.config.namespace
    assert ns is not None
    return {
        "configmaps": sorted(i.metadata.name for i in c.core.list_namespaced_config_map(ns, label_selector=sel).items),
        "jobs": sorted(i.metadata.name for i in c.batch.list_namespaced_job(ns, label_selector=sel).items),
        "pods": sorted(i.metadata.name for i in c.core.list_namespaced_pod(ns, label_selector=sel).items),
        "pvcs": sorted(
            i.metadata.name for i in c.core.list_namespaced_persistent_volume_claim(ns, label_selector=sel).items
        ),
    }


def _empty(objs: dict[str, list[str]]) -> bool:
    return not any(objs.values())


def _anchor_alive(c: Cluster, what: str) -> None:
    anchor = anchors.get_anchor(c.core, namespace=NS, workflow_id=c.workflow_id)
    assert anchor is not None, f"cross-workflow deletion: live anchor of {what} ({c.workflow_id}) is gone"
    assert anchor.metadata.deletion_timestamp is None, f"live anchor of {what} is being deleted"


# --------------------------------------------------------------------------- clients


def client_batch(name: str, idx: int, sh: Shared) -> int:
    import sk8s

    c = _make_cluster()
    sk8s.configure(cluster=c)
    _register(sh, name, c)
    rounds = 0
    try:
        while _more_work(sh, rounds):
            base = idx * 1000 + rounds * 100
            data = list(range(base, base + 10 + 2 * idx))
            if idx == 0:
                got = sk8s.map(lambda x: x * x + 1, data, batch_size=4, timeout=_budget(sh, 150))
                want = [x * x + 1 for x in data]
            else:
                got = sk8s.starmap(
                    lambda x, k: x * k - 7, [(x, 3) for x in data],
                    batch_size=3, max_concurrent=3, timeout=_budget(sh, 150),
                )
                want = [x * 3 - 7 for x in data]
            assert got == want, f"{name} round {rounds}: map returned {got}, expected {want}"
            assert sum(got) == sum(want)
            _anchor_alive(c, name)
            rounds += 1
    finally:
        sh.live.pop(c.workflow_id, None)
        c.close()
        sk8s.configure()
    return rounds


def _volume_alive(core: Any, vol: str) -> None:
    pvc = core.read_namespaced_persistent_volume_claim(name=vol, namespace=NS)  # 404 -> fails the test
    labels = pvc.metadata.labels or {}
    assert pvc.metadata.deletion_timestamp is None, f"kept volume {vol} is being deleted"
    assert not pvc.metadata.owner_references, f"kept volume {vol} gained an owner: {pvc.metadata.owner_references}"
    assert labels.get("sk8s/kind") == "volume-keep", labels
    assert anchors.WORKFLOW_LABEL not in labels, labels


def client_workflow(name: str, idx: int, sh: Shared) -> int:
    from sk8s import attach, create_volume, delete_volume, submit_workflow
    from sk8s.errors import Sk8sError  # noqa: F401  (documented failure type)

    def pipeline(n: int) -> int:
        # Defined locally so cloudpickle ships it by value (the worker image has no tests package).
        import sk8s

        return int(sum(sk8s.map(lambda x: x * x, range(n), batch_size=2)))

    vc = _make_cluster() if idx == 0 else None
    vol = None
    if vc is not None:
        vol = create_volume("50Mi", name=f"stress-keep-{sh.token}", keep=True, cluster=vc)
        _volume_alive(vc.core, vol)
    rounds = 0
    try:
        while _more_work(sh, rounds):
            n = 4 + idx + rounds % 3
            want = sum(x * x for x in range(n))
            c1, c2 = _make_cluster(), _make_cluster()
            wf = None
            try:
                h = submit_workflow(pipeline, n, cluster=c1)
                wf = h.id
                sh.ledger.append((name, wf))
                sh.live[wf] = name
                h2 = attach(wf, cluster=c2)  # a brand-new Cluster, as another process would build
                got = h2.result(timeout=_budget(sh, 200), delete=False)
                assert got == want, f"{name} round {rounds}: workflow returned {got!r}, expected {want}"
                anchor = anchors.get_anchor(c2.core, namespace=NS, workflow_id=wf)
                assert anchor is not None, f"cross-workflow deletion: live workflow {wf} lost its anchor"
                sh.live.pop(wf, None)  # deliberate deletion starts here
                assert h2.result(timeout=_budget(sh, 60)) == want  # repeatable; delete=True
                _poll(
                    lambda: anchors.get_anchor(c2.core, namespace=NS, workflow_id=wf) is None,
                    _budget(sh, 30), f"workflow {wf} to be deleted after result()",
                )
            finally:
                if wf is not None:
                    sh.live.pop(wf, None)
                    try:
                        h.kill()  # no-op once deleted; never leaks a detached workflow on failure
                    except Exception:  # noqa: BLE001
                        pass
                c1.close(delete_workflow=False)
                c2.close(delete_workflow=False)
            if vc is not None and vol is not None:
                _volume_alive(vc.core, vol)
            rounds += 1
        if vc is not None and vol is not None:
            _volume_alive(vc.core, vol)  # at the very end: survived every sweep and workflow
    finally:
        if vc is not None and vol is not None:
            delete_volume(vol, cluster=vc)
            vc.close()
    if vc is not None and vol is not None:
        _poll(
            lambda: not any(
                p.metadata.name == vol
                for p in vc.core.list_namespaced_persistent_volume_claim(NS).items
            ),
            _budget(sh, 60), f"kept volume {vol} to be deleted",
        )
    return rounds


def client_churn(name: str, idx: int, sh: Shared) -> int:
    from sk8s.errors import Sk8sError
    from sk8s.workflows import submit_workflow

    rounds = 0
    while _more_work(sh, rounds, reserve=75):
        # --- sleepers: cancel half, the rest must still deliver exact values --------
        c = _make_cluster()
        _register(sh, name, c)
        try:
            def sleeper(i: int) -> int:
                import time

                time.sleep(20)
                return i * 10

            futs = [c.submit(sleeper, i) for i in range(6)]
            cancelled, kept = futs[::2], futs[1::2]
            for f in cancelled:
                assert f.cancel() is True, f"cancel() of {f.job_name} returned False"
                assert f.cancelled() or isinstance(f.exception(timeout=5), concurrent.futures.CancelledError), (
                    f"{f.job_name} not cancelled"
                )
                with pytest.raises(concurrent.futures.CancelledError):
                    f.result(timeout=5)
            got = [f.result(timeout=_budget(sh, 150)) for f in kept]
            assert got == [10, 30, 50], f"{name} round {rounds}: kept sleepers returned {got}"
            _anchor_alive(c, f"{name} sleepers")
            gone = [f.job_name for f in cancelled]
            _poll(
                lambda: not (set(gone) & {j.metadata.name for j in c.batch.list_namespaced_job(NS).items}),
                _budget(sh, 90), f"cancelled jobs {gone} to disappear",
            )
        finally:
            sh.live.pop(c.workflow_id, None)
            c.close()

        # --- detached workflow killed mid-run: the kill must cascade ----------------
        def driver() -> list[int]:
            import sk8s

            def nap(i: int) -> int:
                import time

                time.sleep(120)
                return i

            return list(sk8s.map(nap, range(3)))

        c3 = _make_cluster()
        wf = None
        try:
            h = submit_workflow(driver, cluster=c3)
            wf = h.id
            sh.ledger.append((name, wf))
            sh.live[wf] = name
            before = _poll(
                lambda: (lambda o: o if len(o["jobs"]) >= 3 else None)(_labeled(c3, wf)),
                _budget(sh, 100), f"workflow {wf} to fan out nested jobs",
            )
            assert before["pods"], before
            sh.live.pop(wf, None)
            h.kill()
            _poll(
                lambda: _empty(_labeled(c3, wf)),
                _budget(sh, 120), f"kill of {wf} to cascade to {before}",
            )
            with pytest.raises(Sk8sError):
                h.result(timeout=5)
        finally:
            if wf is not None:
                sh.live.pop(wf, None)
                try:
                    h.kill()
                except Exception:  # noqa: BLE001
                    pass
            c3.close(delete_workflow=False)
        rounds += 1
    return rounds


def client_hygiene(name: str, idx: int, sh: Shared) -> int:
    import sk8s

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)

    # Backdated but LIVE (an active sleeper): every sweep must leave it alone.
    hc = _make_cluster()
    _register(sh, name, hc)

    def long_sleeper() -> None:
        import time

        time.sleep(WORK_SECONDS + 60)

    live_fut = hc.submit(long_sleeper)
    anchors.set_annotation(
        hc.core, namespace=NS, workflow_id=hc.workflow_id, key=anchors.CREATED_AT, value=old.isoformat()
    )

    # Backdated and IDLE (finished job): the positive control - clean() must be able to delete.
    dc = _make_cluster()
    _register(sh, name, dc, live=False)
    assert dc.submit(lambda: 42).result(timeout=_budget(sh, 120)) == 42
    anchors.set_annotation(
        dc.core, namespace=NS, workflow_id=dc.workflow_id, key=anchors.CREATED_AT, value=old.isoformat()
    )
    dc.close(delete_workflow=False)

    def gone(c: Cluster) -> bool:
        return anchors.get_anchor(c.core, namespace=NS, workflow_id=c.workflow_id) is None

    rounds = 0
    try:
        while True:
            before = set(sh.live.keys())
            deleted = sk8s.clean(NS, older_than_days=0)
            after = set(sh.live.keys())
            bad = sorted(set(deleted) & before & after)
            assert not bad, f"clean() deleted LIVE workflows {bad} (returned {deleted})"
            _anchor_alive(hc, "backdated-live workflow")
            rounds += 1
            if time.time() >= sh.work_end and gone(dc):
                break
            if time.time() >= sh.work_end + 60:
                raise AssertionError(
                    f"positive control failed: backdated idle workflow {dc.workflow_id} was never "
                    f"cleaned (last clean() returned {deleted})"
                )
            time.sleep(2)
        # Once it is idle and deregistered the backdated workflow becomes fair game too.
        sh.live.pop(hc.workflow_id, None)
        assert live_fut.cancel() is True
        _poll(
            lambda: gone(hc) or (sk8s.clean(NS, older_than_days=0) and gone(hc)),
            _budget(sh, 120), f"idle workflow {hc.workflow_id} to become cleanable",
            interval=3.0,
        )
    finally:
        sh.live.pop(hc.workflow_id, None)
        hc.close(delete_workflow=False)
    return rounds


# --------------------------------------------------------------------------- harness


_CLIENTS: list[tuple[str, Callable[[str, int, Shared], int], int]] = [
    ("batch-0", client_batch, 0),
    ("batch-1", client_batch, 1),
    ("workflow-0", client_workflow, 0),  # also owns the keep=True volume
    ("workflow-1", client_workflow, 1),
    ("churn-0", client_churn, 0),
    ("hygiene-0", client_hygiene, 0),
]


def _client_entry(name: str, kind: str, idx: int, sh: Shared) -> None:
    """Process entry point (module-level so ``spawn`` can import it)."""
    fn = {f.__name__: f for _, f, _ in _CLIENTS}[kind]
    try:
        sh.stats[name] = fn(name, idx, sh)
    except BaseException:  # noqa: BLE001 - report everything, then fail the exit code
        sh.errors.append((name, traceback.format_exc()))
        sys.exit(1)


def _dump(c: Cluster, ids: list[str]) -> str:
    lines = []
    for wf in ids:
        try:
            objs = _labeled(c, wf)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  {wf}: could not list ({exc!r})")
            continue
        if not _empty(objs):
            lines.append(f"  {wf}: {objs}")
    try:
        kept = [
            p.metadata.name for p in c.core.list_namespaced_persistent_volume_claim(
                NS, label_selector="sk8s/kind=volume-keep").items
        ]
        lines.append(f"  kept volumes (all runs): {kept}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  could not list kept volumes ({exc!r})")
    return "\n".join(lines) or "  (nothing labeled survives)"


def test_multi_client_isolation_under_load() -> None:
    ctx = multiprocessing.get_context("spawn")
    start = time.time()
    token = secrets.token_hex(3)
    probe = _make_cluster()
    procs: list[tuple[str, Any]] = []
    ledger: list[tuple[str, str]] = []
    try:
        with ctx.Manager() as mgr:
            sh = Shared(
                live=mgr.dict(), ledger=mgr.list(), stats=mgr.dict(), errors=mgr.list(),
                token=token, work_end=start + WORK_SECONDS, hard_end=start + HARD_SECONDS,
            )
            for name, fn, idx in _CLIENTS:
                p = ctx.Process(target=_client_entry, args=(name, fn.__name__, idx, sh), name=name)
                p.start()
                procs.append((name, p))
            problems: list[str] = []
            for name, p in procs:
                p.join(timeout=max(1.0, sh.hard_end + 30 - time.time()))
                if p.is_alive():
                    p.terminate()
                    p.join(10)
                    problems.append(f"{name}: still running at the shared deadline (terminated)")
                elif p.exitcode != 0:
                    problems.append(f"{name}: exit code {p.exitcode}")
            for name, tb in list(sh.errors):
                problems.append(f"{name} raised:\n{tb}")
            stats = dict(sh.stats)
            ledger[:] = list(sh.ledger)
            ids = sorted({wf for _, wf in ledger})

            print(f"\nstress: {time.time() - start:.0f}s, rounds per client: {stats}, "
                  f"{len(ids)} workflows touched")
            if problems:
                pytest.fail(
                    "stress clients failed:\n" + "\n\n".join(problems)
                    + f"\n\nsurviving labeled objects:\n{_dump(probe, ids)}",
                    pytrace=False,
                )
            missing = [n for n, _, _ in _CLIENTS if stats.get(n, 0) < 1]
            assert not missing, f"clients that completed no round: {missing}"

            # Per-client, label-scoped sweeps: nothing of any workflow may linger.
            deadline = time.monotonic() + SWEEP_SECONDS
            leftover: dict[str, dict[str, list[str]]] = {}
            while True:
                leftover = {}
                for client, wf in ledger:
                    objs = _labeled(probe, wf)
                    if not _empty(objs):
                        leftover[f"{client}/{wf}"] = objs
                if not leftover or time.monotonic() > deadline:
                    break
                time.sleep(3)
            assert not leftover, (
                f"objects survive after all clients finished (waited {SWEEP_SECONDS}s):\n{leftover}\n"
                f"{_dump(probe, ids)}"
            )
            kept_left = [
                p.metadata.name for p in probe.core.list_namespaced_persistent_volume_claim(
                    NS, label_selector="sk8s/kind=volume-keep").items
                if p.metadata.name == f"stress-keep-{token}"
            ]
            assert not kept_left, f"kept volume was not deleted: {kept_left}"
    finally:
        # Never leave debris for the next run, whatever happened above.
        from sk8s import delete_volume
        from sk8s.cleaning import clean

        for _, p in procs:
            if p.is_alive():
                p.terminate()
        for wf in sorted({wf for _, wf in ledger}):
            try:
                clean(NS, workflow=wf)
            except Exception:  # noqa: BLE001
                pass
        try:
            delete_volume(f"stress-keep-{token}", cluster=probe)
        except Exception:  # noqa: BLE001
            pass
        probe.close()
