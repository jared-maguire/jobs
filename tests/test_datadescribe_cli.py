"""`sk8s data describe`."""
import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from sk8s import cli, datadescribe
from tests.test_datadescribe import RangeGCS, _cohort_objects, _fastq_cohort, fastq_text, make_bam, write


def test_human_output_for_a_cohort(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fastq_cohort(tmp_path, samples=2)
    assert cli.run(["data", "describe", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Found 2 samples (FASTQ, paired-end, 4 files")
    assert "read length 8" in out


def test_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fastq_cohort(tmp_path, samples=2)
    assert cli.run(["data", "describe", str(tmp_path), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["kind"] == "cohort" and d["file_count"] == 4
    assert d["patterns"]["fastq"]["paired_end_pairs"] == 2


def test_single_file_human_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = write(tmp_path / "s1.bam", make_bam())
    assert cli.run(["data", "describe", str(p)]) == 0
    out = capsys.readouterr().out
    assert "format: bam (confidence: magic, bgzf-compressed)" in out
    assert "sort_order: coordinate" in out
    assert cli.run(["data", "describe", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["facts"]["reference_count"] == 2


def test_missing_path_exits_1_with_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(["data", "describe", str(tmp_path / "nope")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("sk8s: error: no such file or directory")


def test_remote_uri_goes_through_the_client_factory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = RangeGCS(_cohort_objects("bkt", "runs/oct/"))
    seen: list[str] = []

    def factory(scheme: str) -> Any:
        seen.append(scheme)
        return client

    monkeypatch.setattr(datadescribe, "_make_client", factory)
    assert cli.run(["data", "describe", "gs://bkt/runs/oct/", "--json"]) == 0
    assert seen == ["gs"]
    assert json.loads(capsys.readouterr().out)["file_count"] == 8


def test_samples_and_max_objects_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fastq_cohort(tmp_path, samples=6)
    assert cli.run(["data", "describe", str(tmp_path), "--json", "--samples", "2", "--max-objects", "7"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert len(d["samples"]) == 2
    assert d["listing_capped"] is True and d["file_count"] == 7


def test_data_without_action_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        cli.run(["data"])
    assert e.value.code == 2


def test_gz_roundtrip_sanity() -> None:  # keeps the helper imports honest
    assert gzip.decompress(gzip.compress(fastq_text(1).encode())).startswith(b"@read0")
