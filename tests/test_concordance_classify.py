"""Unit tests for the pure TP/FP/FN/GT classifier in examples/na12878_brca2.py (the example lives
outside the package, so it is loaded by path)."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "na12878_brca2", Path(__file__).resolve().parent.parent / "examples" / "na12878_brca2.py"
)
assert _spec and _spec.loader
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)


def row(key: str, typ: str, g: str) -> list[str]:
    return [f"chr13:{key}", typ, g]


def test_all_three_failure_paths():
    tp_t = [row("100:A:G", "SNP", "0/1"), row("200:C:T", "SNP", "1/1"), row("300:CA:C", "INDEL", "0/1")]
    tp_c = [row("100:A:G", "SNP", "1/1"), row("200:C:T", "SNP", "1|1"), row("300:CA:C", "INDEL", "1/0")]
    fn = [row("400:G:T", "SNP", "0/1")]
    fp = [row("500:A:C", "SNP", "0/1"), row("600:T:TA", "INDEL", "1/1")]
    m, mism = ex.classify_sites(fn, fp, tp_t, tp_c)
    assert m["all"]["tp"] == 3 and m["all"]["fn"] == 1 and m["all"]["fp"] == 2
    assert m["snv"] == {**m["snv"], "tp": 2, "fn": 1, "fp": 1, "gt_match": 1}
    assert m["indel"]["tp"] == 1 and m["indel"]["fp"] == 1 and m["indel"]["gt_match"] == 1  # 1/0 == 0/1
    assert m["all"]["recall"] == 0.75 and m["all"]["precision"] == 0.6
    assert m["all"]["gt_match_rate"] == round(2 / 3, 4)
    assert mism == ["chr13:100:A:G truth=0/1 calls=1/1"]  # phased / reordered GT are NOT mismatches


def test_empty_and_other_class():
    m, mism = ex.classify_sites([], [], [], [])
    assert m["all"]["recall"] is None and m["all"]["f1"] is None and mism == []
    m, _ = ex.classify_sites([row("1:AT:GC", "MNP", "1/1")], [], [], [])
    assert m["other"]["fn"] == 1 and m["all"]["fn"] == 1 and m["snv"]["fn"] == 0
