"""Concordance primitives shared by examples/na12878_brca2.py and examples/cohort_eval.py.

PURE (no cluster, no tools). Scripts that run these in a worker must ship this module BY VALUE
(``cloudpickle.register_pickle_by_value``) because the worker image does not contain examples/.
"""

from __future__ import annotations

from typing import Any


def ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def classify_sites(
    fn: list[list[str]], fp: list[list[str]], tp_truth: list[list[str]], tp_calls: list[list[str]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """PURE: turn isec output into per-class metrics + the genotype mismatches. Every row is
    ``[CHROM:POS:REF:ALT, TYPE, GT]``. fn = truth-only, fp = calls-only, tp_truth/tp_calls = the shared
    records as seen on each side (same keys). Classes: snv, indel, other (MNP etc.), all."""

    def kind(t: str) -> str:
        return {"SNP": "snv", "INDEL": "indel"}.get(t, "other")

    def gt(g: str) -> tuple[str, ...]:
        return tuple(sorted(g.replace("|", "/").split("/")))

    classes: dict[str, dict[str, int]] = {
        k: {"tp": 0, "fp": 0, "fn": 0, "gt_match": 0} for k in ("snv", "indel", "other", "all")
    }
    calls_by_key = {r[0]: r for r in tp_calls}
    mismatches = []
    for r in tp_truth:
        ok = gt(r[2]) == gt(calls_by_key[r[0]][2])
        for k in (kind(r[1]), "all"):
            classes[k]["tp"] += 1
            classes[k]["gt_match"] += ok
        if not ok:
            mismatches.append(f"{r[0]} truth={r[2]} calls={calls_by_key[r[0]][2]}")
    for which, rs in (("fn", fn), ("fp", fp)):
        for r in rs:
            for k in (kind(r[1]), "all"):
                classes[k][which] += 1
    metrics: dict[str, dict[str, Any]] = {}
    for k, c in classes.items():
        rec, prec = ratio(c["tp"], c["tp"] + c["fn"]), ratio(c["tp"], c["tp"] + c["fp"])
        f1 = round(2 * rec * prec / (rec + prec), 4) if rec and prec else None
        metrics[k] = {**c, "recall": rec, "precision": prec, "f1": f1, "gt_match_rate": ratio(c["gt_match"], c["tp"])}
    return metrics, mismatches
