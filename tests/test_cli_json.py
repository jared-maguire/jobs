"""`sk8s explain` and the --json flags: machine-readable output; human output stays as pinned."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest
from kubernetes import client

import sk8s.cleaning as cleaning
import sk8s.doctor as doctor_mod
from sk8s import anchors, cli
from sk8s.cleaning import CleanReport
from sk8s.doctor import Check
from tests.fakes import FakeCoreV1
from tests.test_cli import _anchor, _fakes, _job, env  # noqa: F401  (autouse fixture)

H = datetime.timedelta


def _pod(core: FakeCoreV1, job: str, wf: str, *, phase: str, waiting: tuple[str, str] | None = None) -> None:
    state = client.V1ContainerState(waiting=client.V1ContainerStateWaiting(reason=waiting[0], message=waiting[1])) if waiting else None
    core.pods[("ctx-ns", f"{job}-pod")] = client.V1Pod(
        metadata=client.V1ObjectMeta(name=f"{job}-pod", namespace="ctx-ns",
                                     labels={"job-name": job, anchors.WORKFLOW_LABEL: wf}),
        spec=client.V1PodSpec(containers=[client.V1Container(name="worker", image="nope:1")]),
        status=client.V1PodStatus(phase=phase, container_statuses=[client.V1ContainerStatus(
            name="worker", image="nope:1", image_id="", ready=False, restart_count=0, state=state)] if state else None),
    )


# --- explain ----------------------------------------------------------------------------------------


def test_explain_human_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    core, batch, _ = _fakes(monkeypatch)
    _anchor(core, "wf-1", age=H(hours=3), driver="drv")
    batch.jobs[("ctx-ns", "drv")] = _job("drv", active=1)
    batch.jobs[("ctx-ns", "drv")].metadata.labels = {anchors.WORKFLOW_LABEL: "wf-1"}
    assert cli.run(["explain", "wf-1"]) == 0
    out = capsys.readouterr().out
    assert "workflow wf-1" in out and "detached" in out and "drv" in out
    assert "nothing is blocking" in out


def test_explain_json_is_the_report(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    core, batch, _ = _fakes(monkeypatch)
    _anchor(core, "wf-1", age=H(hours=3), driver="")
    assert cli.run(["explain", "wf-1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["workflow"]["id"] == "wf-1" and report["blocking"] == []
    assert set(report) >= {"workflow", "driver", "jobs", "counts", "volumes", "store", "blocking"}


def test_explain_exit_0_even_when_unhealthy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    core, batch, _ = _fakes(monkeypatch)
    _anchor(core, "wf-1", age=H(hours=1), driver="")
    job = _job("pull", active=1)
    job.metadata.labels = {anchors.WORKFLOW_LABEL: "wf-1"}
    batch.jobs[("ctx-ns", "pull")] = job
    _pod(core, "pull", "wf-1", phase="Pending", waiting=("ImagePullBackOff", "Back-off"))
    assert cli.run(["explain", "wf-1"]) == 0
    out = capsys.readouterr().out
    assert "blocking:" in out and "nope:1" in out
    assert cli.run(["explain", "wf-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["blocking"]


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_explain_unknown_workflow_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    _fakes(monkeypatch)
    assert cli.run(["explain", "wf-nope", *flags]) == 1
    captured = capsys.readouterr()
    assert "unknown workflow wf-nope" in captured.err and "Traceback" not in captured.err
    assert captured.out == ""


def test_explain_needs_a_workflow() -> None:
    with pytest.raises(SystemExit) as e:
        cli.run(["explain"])
    assert e.value.code == 2


# --- workflows --json ---------------------------------------------------------------------------------


def test_workflows_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    core, batch, _ = _fakes(monkeypatch)
    _anchor(core, "wf-run", age=H(hours=3), driver="drv-1")
    _anchor(core, "wf-nodrv", age=H(minutes=5), driver="", detached=False)
    batch.create_namespaced_job("ctx-ns", _job("drv-1", active=1))
    assert cli.run(["workflows", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["wf-run", "wf-nodrv"]  # oldest first, as in the table
    run = rows[0]
    assert run["detached"] is True and run["age"] == "3h" and run["driver"] == "active"
    assert run["driver_job"] == "drv-1" and run["age_seconds"] == pytest.approx(3 * 3600, abs=5)
    assert rows[1]["detached"] is False and rows[1]["driver"] == "unknown" and rows[1]["driver_job"] == ""


def test_workflows_json_empty_is_an_empty_list(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fakes(monkeypatch)
    assert cli.run(["workflows", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


# --- doctor --json ------------------------------------------------------------------------------------


def test_doctor_json_keeps_the_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor_mod, "doctor", lambda profile, **kw: [
        Check("cluster reachable", True), Check("role permissions", False, "run: sk8s config"),
    ])
    assert cli.run(["doctor", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == [
        {"name": "cluster reachable", "ok": True, "fix_hint": ""},
        {"name": "role permissions", "ok": False, "fix_hint": "run: sk8s config"},
    ]


def test_doctor_json_all_pass_exit_0(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor_mod, "doctor", lambda profile, **kw: [Check("a", True)])
    assert cli.run(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["ok"] is True


# --- clean --json -------------------------------------------------------------------------------------


def test_clean_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[Any] = []

    def fake(namespace: str | None = None, **kw: Any) -> CleanReport:
        calls.append((namespace, kw))
        return CleanReport(["wf-1"], [{"workflow": "wf-2", "reason": "not expired: 1.00 days old, limit 7 days"}])

    monkeypatch.setattr(cleaning, "clean_report", fake)
    assert cli.run(["clean", "--older-than", "36h", "--json"]) == 0
    assert calls == [("ctx-ns", {"older_than_days": 1.5})]
    assert json.loads(capsys.readouterr().out) == {
        "deleted": ["wf-1"],
        "kept": [{"workflow": "wf-2", "reason": "not expired: 1.00 days old, limit 7 days"}],
    }


def test_clean_json_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cleaning, "clean_report", lambda *a, **k: CleanReport())
    assert cli.run(["clean", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": [], "kept": []}


def test_clean_without_json_still_uses_plain_clean(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cleaning, "clean", lambda *a, **k: ["wf-1"])
    monkeypatch.setattr(cleaning, "clean_report", lambda *a, **k: pytest.fail("human output must not change"))
    assert cli.run(["clean"]) == 0
    assert capsys.readouterr().out == "deleted wf-1\n"
