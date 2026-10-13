#!/usr/bin/env python
"""NA12878 / BRCA2: reads -> alignment -> SNVs + indels -> VCF, as a detached, memoized sk8s workflow.

The request, verbatim: "Fetch reads from NA12878 from some small tractable genomic region
(e.g. the BRCA2 gene), align them, call SNVs and Indels there, and create a VCF."
The napkin's last line: "Re-running tomorrow would cost $0 - it's all cached."  This is that,
honestly framed: a detached re-run creates no stage jobs (one driver pod, ~$0.0002); a
--tethered re-run creates no jobs at all.

What it does (see examples/README.md for the one-page version):

    describe the input  ->  dry_run preview of the plan  ->  submit a DETACHED driver which runs
      1. fetch_fastq   inputs are sk8s.artifact()s pinned by generation AND sha256; md5-check the
                       manifest too; mate-aware FASTQ out                               [expect=]
      2. align         bwa index + mem, fixmate, sort, markdup  -> BAM artifact            [expect=]
      3. call_window   bcftools mpileup | call -mv over N windows (starmap, pilot=1)     [expect=]
      4. merge         concat, bcftools norm, lift slice coords -> chr13, bgzip+tabix,
                       stats.json                                                        [expect=]
      5. concordance   our VCF vs the pinned GIAB HG001 v4.2.1 truth inside the confident BED:
                       norm both, bcftools isec -> TP/FP/FN, GT match; site/GT matching, NOT
                       hap.py. concordance.json + stats.json (concordance folded in)        [expect=]
      6. publish       Artifact.publish() of VCF + index + stats + concordance.json to
                       results/<run-id>/ with sk8s-artifact-sha256 / sk8s-produced-by metadata
    Every stage is memo=True: run it again and the whole chain is a memo hit - no stage pods.

Inputs are PINNED (gs://.../demo-data/na12878-brca2/, provenance in its MANIFEST.json): 26,841
reads of 1000 Genomes NYGC 30x NA12878, GRCh38, BRCA2 +/-10kb = chr13:32,305,086-32,410,268, plus
the matching 105,183 bp reference slice (contig ``chr13_32305086_32410268``).

Environment (nothing else is read implicitly):

    KUBECONFIG    kubeconfig whose current context is the GKE cluster
    SK8S_CONFIG   toml with namespace / store (gs://...) for a Workload-Identity-ready namespace
    SK8S_IMAGE    worker image = sk8s worker base BUILT FROM A CHECKOUT WITH THE ARTIFACT LAYER
                  + bwa/samtools/bcftools (examples/Dockerfile.na12878); its Python minor MUST
                  equal this client's (3.14 for the demo image). Memo keys include this string,
                  so a new image tag is a (deliberate) cache miss.

Usage:
    python examples/na12878_brca2.py                  # describe, preview, one full run
    python examples/na12878_brca2.py --repeat 2       # two runs: 1st computes, 2nd is all memo hits
    python examples/na12878_brca2.py --memo-salt v2   # change the salt: everything recomputes
    python examples/na12878_brca2.py --tethered       # drive from this process (a pure re-run = 0 jobs)
    python examples/na12878_brca2.py --preview-only   # describe + dry_run plan, touch nothing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import sk8s

DATA = "gs://jared-genome-analysis-sk8s/demo-data/na12878-brca2/"
GIAB = DATA + "giab/"  # GIAB HG001 GRCh38 v4.2.1 truth sliced to the region; provenance in giab/MANIFEST.json
RESULTS = "gs://jared-genome-analysis-sk8s/sk8s-demo/results/"

CONTIG = "chr13_32305086_32410268"  # reference-slice contig name
SLICE_LEN = 105_183
OFFSET = 32_305_085  # chr13 position = slice position + OFFSET (slice is 1-based)
CHR13_LEN = 114_364_328  # GRCh38 chr13, only used for the lifted ##contig header line
N_WINDOWS = 4

BAM = f"NA12878.{CONTIG}.bam"
REF = f"GRCh38.{CONTIG}.fa"
# sha256 of the mirrored objects, copied from MANIFEST.json. Together with the generation pin
# sk8s.artifact() records at submit, an input can neither change nor be swapped unnoticed.
SHA256 = {
    BAM: "e5fe6834224568732013d53d7ae0df7eb4009bd90fc08c4657d36d6a58904b60",
    REF: "b2a1f089a5470299a2d9f47b65162bba8d3044acc25982ceb308c550e411036b",
}
GIAB_SHA256 = {  # from giab/MANIFEST.json
    "truth.vcf.gz": "6402bf61d60977529858c5702296647099d2ba603b5639900d1a6056951545b7",
    "truth.vcf.gz.tbi": "f835c273f6ba7d6ad16f071963f3d76b6542875d093749398aa567e51a7d59ee",
    "confident.bed": "0ae341e5ad3da3300fad432b507f45ca4a3daf1668e50a88877c50b4fc1d5b32",
}

# Per-stage resources. Autopilot rounds these up; nothing here asserts exact values.
R_FETCH = {"cpu": "1", "memory": "2Gi"}
R_ALIGN = {"cpu": "2", "memory": "4Gi"}
R_CALL = {"cpu": "1", "memory": "2Gi"}
R_MERGE = {"cpu": "1", "memory": "2Gi"}
R_CONCORD = {"cpu": "1", "memory": "2Gi"}


def inputs(data: str) -> tuple[Any, Any, Any]:
    """The three input Artifacts: region BAM and reference slice (sha256-pinned) and the manifest."""
    return (
        sk8s.artifact(data + BAM, sha256=SHA256[BAM]),
        sk8s.artifact(data + REF, sha256=SHA256[REF]),
        sk8s.artifact(data + "MANIFEST.json"),
    )


def truth_inputs(giab: str) -> tuple[Any, Any, Any]:
    """The pinned GIAB truth: high-confidence VCF, its index, and the confident-regions BED."""
    return tuple(sk8s.artifact(giab + n, sha256=h) for n, h in GIAB_SHA256.items())  # type: ignore[return-value]


# ----------------------------------------------------------------------------------------------
# Stage functions run IN THE CLUSTER. Artifact arguments arrive as real local Paths; whatever is
# returned under the OutDir becomes an Artifact in the CAS. They are deterministic and take no
# run-id, so their memo keys depend only on code + inputs. (This file's __main__ functions pickle
# by value, so the image needs no copy of the script - only sk8s and the bio tools.)
# ----------------------------------------------------------------------------------------------


def _sh(script: str, cwd: str) -> str:
    """Run a bash pipeline with errexit+pipefail; on failure raise with the stderr tail."""
    import subprocess

    p = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], cwd=cwd, capture_output=True, text=True
    )
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {script[:200]}\n{p.stderr[-1500:]}")
    return p.stdout


def _md5(path: Path) -> str:
    import hashlib

    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_fastq(bam: Path, ref: Path, manifest: Path, out: sk8s.OutDir) -> dict[str, Any]:
    """Mate-aware FASTQ from the region BAM. sk8s already verified sha256 and generation while
    streaming the inputs down; the manifest md5 cross-check is a second, independent belt."""
    import os
    import tempfile

    o = os.fspath(out)  # str(OutDir) is its repr; the path itself is what a shell wants
    files = json.load(open(manifest))["files"]
    for p in (bam, ref):
        got, want = _md5(p), files[p.name]["md5"]
        if got != want:
            raise RuntimeError(f"{p.name}: md5 {got} != manifest {want}")
    d = tempfile.mkdtemp()
    # collate groups mates, -F 0x900 drops secondary/supplementary, complete pairs go to r1/r2 and
    # orphans (mate outside the window) to singletons.fq, which is not aligned.
    _sh(
        f"samtools collate -u -O {bam} col | samtools fastq -n -F 0x900"
        f" -1 {o}/r1.fq -2 {o}/r2.fq -0 /dev/null -s singletons.fq"
        f" && gzip -n {o}/r1.fq {o}/r2.fq",
        d,
    )
    return {
        "stats": {
            "bam_records": int(_sh(f"samtools view -c {bam}", d)),
            "pairs": int(_sh(f"zcat {o}/r1.fq.gz | wc -l", d)) // 4,
            "singletons_dropped": int(_sh("wc -l < singletons.fq", d)) // 4,
            "checked": ["sha256+generation (sk8s)", "md5 (manifest)"],
        },
        "r1": out / "r1.fq.gz",
        "r2": out / "r2.fq.gz",
    }


def align(r1: Path, r2: Path, ref: Path, out: sk8s.OutDir) -> dict[str, Any]:
    """bwa index + mem against the reference slice; fixmate/sort/markdup; index; count."""
    import os
    import shutil
    import tempfile

    o = os.fspath(out)
    d = tempfile.mkdtemp()
    shutil.copy(ref, d)  # bwa writes its index files next to the FASTA
    fa = Path(d) / ref.name
    rg = r"@RG\tID:NA12878\tSM:NA12878\tPL:illumina"
    _sh(f"bwa index {fa}", d)
    # -K fixes the batch size so the result cannot depend on the thread count.
    _sh(
        f"bwa mem -t 2 -K 100000000 -R '{rg}' {fa} {r1} {r2}"
        " | samtools fixmate -m -u - -"
        " | samtools sort -u -"
        f" | samtools markdup - {o}/aln.bam",
        d,
    )
    bam = out / "aln.bam"
    primary = int(_sh(f"samtools view -c -F 0x900 {bam}", d))
    mapped = int(_sh(f"samtools view -c -F 0x904 {bam}", d))
    return {
        "stats": {
            "reads_primary": primary,
            "reads_mapped": mapped,
            "pct_mapped": round(100.0 * mapped / max(primary, 1), 3),
            "duplicates_marked": int(_sh(f"samtools view -c -F 0x900 -f 0x400 {bam}", d)),
        },
        "bam": bam,
    }


def call_window(
    bam: Path, ref: Path, idx: int, start: int, end: int, out: sk8s.OutDir
) -> dict[str, Any]:
    """SNV + indel calls (bcftools mpileup | call -mv) for one slice window, 1-based inclusive."""
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    shutil.copy(ref, d)
    shutil.copy(bam, d)
    fa = ref.name
    _sh(f"samtools index {bam.name}", d)
    vcf = out / f"win{idx:02d}.vcf.gz"
    _sh(
        f"bcftools mpileup -f {fa} -r {CONTIG}:{start}-{end} -a AD,DP -Ou {bam.name}"
        f" | bcftools call -mv -Oz -o {vcf}",
        d,
    )
    return {
        "stats": {
            "window": idx,
            "region": f"{CONTIG}:{start}-{end}",
            "records": int(_sh(f"bcftools view -H {vcf} | wc -l", d)),
        },
        "vcf": vcf,
    }


def _lift(vcf_text: str) -> str:
    """Slice coordinates -> chr13. bcftools can rename contigs but not shift POS, so: a few lines
    of text processing on the (small) VCF. Header contig line is rewritten, records get
    CHROM=chr13, POS += OFFSET; a ##liftover line documents it. Nothing positional lives in INFO
    (no END/SVLEN: this is not a gVCF), so POS is the only coordinate that moves."""
    out: list[str] = []
    for line in vcf_text.splitlines():
        if line.startswith(f"##contig=<ID={CONTIG}"):
            out.append(f"##contig=<ID=chr13,length={CHR13_LEN},assembly=GRCh38>")
        elif line.startswith("#CHROM"):
            out.append(
                f"##liftover=slice {CONTIG} pos + {OFFSET} -> GRCh38 chr13 (sk8s examples/na12878_brca2.py)"
            )
            out.append(line)
        elif line.startswith("#"):
            out.append(line)
        else:
            cols = line.split("\t")
            cols[0] = "chr13"
            cols[1] = str(int(cols[1]) + OFFSET)
            out.append("\t".join(cols))
    return "\n".join(out) + "\n"


def merge_calls(
    vcfs: list[Path], ref: Path, upstream: dict[str, Any], out: sk8s.OutDir
) -> dict[str, Any]:
    """concat windows -> bcftools norm -> lift to chr13 -> bgzip+tabix -> stats.json. (Named for
    what it feeds: the driver publishes these three outputs.) No run-id in, none out: the result
    is a pure function of its inputs, which is what lets it be memoized."""
    import hashlib
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    shutil.copy(ref, d)
    names = []
    for i, v in enumerate(vcfs):  # the list is in window order; staged names may collide, so rename
        shutil.copy(v, f"{d}/win{i:02d}.vcf.gz")
        names.append(f"win{i:02d}.vcf.gz")
    # Windows are disjoint and ordered, so plain concat suffices. norm: left-align indels against
    # the slice and split multiallelics (-m -any) so every record is one SNP or one indel.
    _sh(
        f"bcftools concat {' '.join(names)} -Ou"
        f" | bcftools norm -f {ref.name} -m -any -Oz -o normed.vcf.gz",
        d,
    )
    open(f"{d}/lifted.vcf", "w").write(_lift(_sh("bcftools view normed.vcf.gz", d)))
    vcf = out / "NA12878.BRCA2.vcf.gz"
    _sh(f"bcftools view -Oz -o {vcf} lifted.vcf && bcftools index -t {vcf}", d)
    rows = [
        r.split("\t")
        for r in _sh(f"bcftools query -f '%CHROM\\t%POS\\t%REF\\t%ALT\\t%TYPE\\t[%GT]\\n' {vcf}", d).splitlines()
    ]
    by_type: dict[str, int] = {}
    ts = tv = het = hom = 0
    transitions = {("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")}
    for _chrom, _pos, ref_a, alt, typ, gt in rows:
        by_type[typ] = by_type.get(typ, 0) + 1
        if typ == "SNP":
            if (ref_a, alt) in transitions:
                ts += 1
            else:
                tv += 1
        a = gt.replace("|", "/").split("/")
        if len(set(a)) > 1:
            het += 1
        elif a[0] not in (".", "0"):
            hom += 1
    body = _sh(f"bcftools view -H {vcf}", d)
    stats = {
        "sample": "NA12878",
        "region": "chr13:32305086-32410268 (GRCh38; BRCA2 +/-10kb)",
        "variants": {
            "total": len(rows),
            "by_type": dict(sorted(by_type.items())),
            "ts": ts,
            "tv": tv,
            "ts_tv": round(ts / tv, 3) if tv else None,
            "het": het,
            "hom_alt": hom,
        },
        "records_md5": hashlib.md5(body.encode()).hexdigest(),  # body only: headers carry dates
        "lifted": f"slice pos + {OFFSET} -> chr13",
        "upstream": upstream,
    }
    json.dump(stats, open(out / "stats.json", "w"), indent=2, sort_keys=True)
    return {"stats": stats, "vcf": vcf, "tbi": out / "NA12878.BRCA2.vcf.gz.tbi", "stats_json": out / "stats.json"}


def _padded_chr13(ref: Path, dest: str) -> None:
    """chr13-coordinate FASTA for ``bcftools norm -f``: N x OFFSET then the slice, one contig named
    chr13, 60-col lines. This lets BOTH sides be normalized and compared in chr13 space (the truth's
    space) without reverse-lifting anything. Only positions inside the slice are ever looked up."""
    seq = "".join(l.strip() for l in open(ref) if not l.startswith(">"))
    with open(dest, "w") as f:
        f.write(">chr13\n")
        pad = "N" * OFFSET + seq
        for i in range(0, len(pad), 60):
            f.write(pad[i : i + 60] + "\n")


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def concordance_check(
    vcf: Path, stats_json: Path, ref: Path, truth: Path, truth_tbi: Path, bed: Path, out: sk8s.OutDir
) -> dict[str, Any]:
    """Our final VCF vs the GIAB HG001 v4.2.1 truth, inside the confident regions.

    Both sides: ``bcftools norm -f <chr13 ref> -m -any``, then ``-R confident.bed``, then
    ``bcftools isec`` (same POS+REF+ALT). private-to-truth = FN, private-to-calls = FP, shared = TP.
    Genotype concordance is on the TP set: GT compared as an unordered allele pair, after normalization.
    This is site/GT matching, NOT hap.py-style haplotype-aware comparison."""
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    shutil.copy(vcf, f"{d}/calls_in.vcf.gz")
    shutil.copy(truth, f"{d}/truth_in.vcf.gz")
    shutil.copy(truth_tbi, f"{d}/truth_in.vcf.gz.tbi")
    shutil.copy(bed, f"{d}/confident.bed")  # keep the .bed suffix: bcftools reads it as 0-based
    _padded_chr13(ref, f"{d}/chr13.fa")
    _sh("samtools faidx chr13.fa", d)
    for name in ("calls", "truth"):
        _sh(
            f"bcftools norm -f chr13.fa -m -any -Oz -o {name}_norm.vcf.gz {name}_in.vcf.gz"
            f" && bcftools index -t {name}_norm.vcf.gz"  # -R needs an index (no streaming)
            f" && bcftools view -R confident.bed -Oz -o {name}.vcf.gz {name}_norm.vcf.gz"
            f" && bcftools index -t {name}.vcf.gz",
            d,
        )
    _sh("bcftools isec -p isec truth.vcf.gz calls.vcf.gz", d)  # 0000 FN, 0001 FP, 0002/0003 shared

    fmt = "%CHROM:%POS:%REF:%ALT\\t%TYPE\\t[%GT]\\n"

    def rows(path: str) -> list[list[str]]:
        return [r.split("\t") for r in _sh(f"bcftools query -f '{fmt}' {path}", d).splitlines()]

    def kind(t: str) -> str:
        return {"SNP": "snv", "INDEL": "indel"}.get(t, "other")  # MNP/other: counted in 'all' only

    def gt(g: str) -> tuple[str, ...]:
        return tuple(sorted(g.replace("|", "/").split("/")))

    fn, fp = rows("isec/0000.vcf"), rows("isec/0001.vcf")
    tp_t = {r[0]: r for r in rows("isec/0002.vcf")}
    tp_c = {r[0]: r for r in rows("isec/0003.vcf")}
    classes: dict[str, dict[str, int]] = {
        k: {"tp": 0, "fp": 0, "fn": 0, "gt_match": 0} for k in ("snv", "indel", "other", "all")
    }
    mismatches = []
    for key, r in tp_t.items():
        ok = gt(r[2]) == gt(tp_c[key][2])
        for k in (kind(r[1]), "all"):
            classes[k]["tp"] += 1
            classes[k]["gt_match"] += ok
        if not ok:
            mismatches.append(f"{key} truth={r[2]} calls={tp_c[key][2]}")
    for k_, rs in (("fn", fn), ("fp", fp)):
        for r in rs:
            for k in (kind(r[1]), "all"):
                classes[k][k_] += 1
    metrics: dict[str, Any] = {}
    for k, c in classes.items():
        rec, prec = _ratio(c["tp"], c["tp"] + c["fn"]), _ratio(c["tp"], c["tp"] + c["fp"])
        f1 = round(2 * rec * prec / (rec + prec), 4) if rec and prec else None
        metrics[k] = {
            **c, "recall": rec, "precision": prec, "f1": f1,
            "gt_match_rate": _ratio(c["gt_match"], c["tp"]),
        }
    bed_bp = sum(int(a[2]) - int(a[1]) for a in (l.split() for l in open(f"{d}/confident.bed")))
    result = {
        "method": (
            "site+genotype matching, NOT haplotype-aware (not hap.py/vcfeval): both VCFs normalized "
            "(bcftools norm -f ref -m -any), restricted to the GIAB confident BED, bcftools isec on exact "
            "POS+REF+ALT = TP/FP/FN; GT compared as an unordered allele pair on TPs. Raw unfiltered "
            "bcftools call -mv. Equivalent variants represented differently count as FP+FN."
        ),
        "truth": "GIAB HG001 (NA12878) GRCh38 v4.2.1 high-confidence small variants",
        "region": "chr13:32305086-32410268",
        "confident_bp": bed_bp,
        "confident_fraction_of_region": round(bed_bp / SLICE_LEN, 4),
        # not scorable: outside the confident regions the truth makes no claim (these are NOT errors)
        "calls_outside_bed": int(_sh("bcftools view -H calls_norm.vcf.gz | wc -l", d)) - classes["all"]["tp"] - classes["all"]["fp"],
        "truth_outside_bed": int(_sh("bcftools view -H truth_norm.vcf.gz | wc -l", d)) - classes["all"]["tp"] - classes["all"]["fn"],
        "truth_records_in_bed": classes["all"]["tp"] + classes["all"]["fn"],
        "calls_records_in_bed": classes["all"]["tp"] + classes["all"]["fp"],
        "metrics": metrics,
        "gt_mismatches": mismatches,
        "fp_sites": [r[0] + " " + r[1] for r in fp],
        "fn_sites": [r[0] + " " + r[1] for r in fn],
    }
    json.dump(result, open(out / "concordance.json", "w"), indent=2, sort_keys=True)
    stats = json.load(open(stats_json))  # the merge stage's stats, with concordance folded in
    stats["concordance"] = result
    json.dump(stats, open(out / "stats.json", "w"), indent=2, sort_keys=True)
    return {"concordance": result, "concordance_json": out / "concordance.json", "stats_json": out / "stats.json"}


# ---- QC gates (expect=): run in the worker on each stage's return value, BEFORE upload ------------


def gate_fastq(r: dict[str, Any]) -> str | None:
    if r["stats"]["pairs"] < 10_000:
        return f"only {r['stats']['pairs']} complete pairs extracted (expect ~13,236)"
    return None


def gate_alignment(r: dict[str, Any]) -> str | None:
    s = r["stats"]
    if s["reads_mapped"] < 20_000:
        return f"only {s['reads_mapped']} reads aligned (need >= 20,000)"
    if s["pct_mapped"] <= 90.0:
        return f"only {s['pct_mapped']}% mapped (need > 90%)"
    return None


def gate_window(r: dict[str, Any]) -> str | None:
    # ~26 kb of a human genome carries dozens of NA12878 variants; none/very few means the pileup broke.
    s = r["stats"]
    if s["records"] < 10:
        return f"window {s['window']} ({s['region']}) has only {s['records']} calls (need >= 10)"
    return None


def gate_calls(r: dict[str, Any]) -> str | None:
    v = r["stats"]["variants"]
    if v["total"] == 0:
        return "VCF is empty"
    if v["total"] < 50:
        return f"only {v['total']} VCF records (need >= 50)"
    if v["ts_tv"] is None or not 1.0 <= v["ts_tv"] <= 4.0:
        return f"ts/tv {v['ts_tv']} outside [1.0, 4.0] (ts={v['ts']}, tv={v['tv']})"
    return None


# Regression floors, NOT aspirations. First live run (see examples/README.md): inside the GIAB confident
# regions recall = precision = GT match = 1.0 (166 TP, 0 FP, 0 FN; 139 SNV, 27 indel). Floors sit just
# below that: one lost SNV out of 139 (0.9928) still passes, a real regression does not. The indel floor
# is looser because one miss in 27 is already 0.963.
FLOOR_ALL = 0.97
FLOOR_SNV = 0.97
FLOOR_INDEL = 0.93
MIN_TRUTH = 150  # the truth actually loaded and the BED actually applied (observed 166)


def gate_concordance(r: dict[str, Any]) -> str | None:
    c = r["concordance"]
    m = c["metrics"]
    if c["truth_records_in_bed"] < MIN_TRUTH:
        return f"only {c['truth_records_in_bed']} truth records inside the confident BED (need >= {MIN_TRUTH}); truth/BED not applied?"
    for cls, floor in (("all", FLOOR_ALL), ("snv", FLOOR_SNV), ("indel", FLOOR_INDEL)):
        for metric in ("recall", "precision", "gt_match_rate"):
            v = m[cls][metric]
            if v is None or v < floor:
                return f"concordance {cls} {metric} = {v} below regression floor {floor} ({m[cls]})"
    return None


# ---- the pipeline (runs as the detached driver, or in this process with --tethered) -------------


def pipeline(run_id: str, data: str, results: str, salt: str, giab: str = GIAB) -> dict[str, Any]:
    """Chain the per-stage jobs. Every stage is memo=True; ``memo`` below records hit/computed."""
    out = f"{results}{run_id}/"
    t0 = time.time()
    bam, ref, manifest = inputs(data)
    memo: dict[str, Any] = {}
    opts: dict[str, Any] = {"memo": True, "memo_salt": salt}

    def stage(label: str, fn: Any, *args: Any, **kw: Any) -> Any:
        t = time.time()
        fut = sk8s.submit(fn, *args, **opts, **kw)
        res = fut.result()
        memo[label] = "HIT" if fut.memo_hit else "computed"
        print(f"[{run_id}] {label}: {memo[label]} ({time.time() - t:.1f}s)", flush=True)  # `sk8s logs <wf>`
        return res

    fq = stage("fetch_fastq", fetch_fastq, bam, ref, manifest, requests=R_FETCH, expect=gate_fastq)
    al = stage("align", align, fq["r1"], fq["r2"], ref, requests=R_ALIGN, expect=gate_alignment)
    # Natural fan-out: the 105 kb slice is cut into N disjoint windows called in parallel. Calling is
    # per-position over the one full BAM (passed by hash), so this equals a single whole-region call
    # (verified on this data: identical sites), not an approximation. pilot=1: window 0 must pass
    # its gate before the rest are submitted.
    size = -(-SLICE_LEN // N_WINDOWS)
    spans = [(i, i * size + 1, min((i + 1) * size, SLICE_LEN)) for i in range(N_WINDOWS)]
    t = time.time()
    futs = sk8s.submit_starmap(
        call_window,
        [(al["bam"], ref, i, s, e) for i, s, e in spans],
        pilot=1, requests=R_CALL, expect=gate_window, **opts,
    )
    calls = [f.result() for f in futs]
    memo["call_window"] = ["HIT" if f.memo_hit else "computed" for f in futs]
    print(f"[{run_id}] call_window x{N_WINDOWS}: {memo['call_window']} ({time.time() - t:.1f}s)", flush=True)
    upstream = {
        "fastq": fq["stats"],
        "alignment": al["stats"],
        "windows": [c["stats"] for c in calls],
    }
    final = stage(
        "merge", merge_calls, [c["vcf"] for c in calls], ref, upstream,
        requests=R_MERGE, expect=gate_calls,
    )
    tvcf, ttbi, tbed = truth_inputs(giab)
    conc = stage(
        "concordance", concordance_check, final["vcf"], final["stats_json"], ref, tvcf, ttbi, tbed,
        requests=R_CONCORD, expect=gate_concordance,
    )
    # Publish: a permanent, legible home. copy=True keeps the CAS copy as well, so a later memo
    # hit can still find the bytes even if this results prefix is cleaned up.
    published = {
        k: src[k].publish(out + name, copy=True).uri
        for src, k, name in (
            (final, "vcf", "NA12878.BRCA2.vcf.gz"),
            (final, "tbi", "NA12878.BRCA2.vcf.gz.tbi"),
            (conc, "stats_json", "stats.json"),  # merge's stats + the concordance block
            (conc, "concordance_json", "concordance.json"),
        )
    }
    print(f"[{run_id}] published {len(published)} objects under {out}", flush=True)
    return {
        "run_id": run_id,
        "stats": {**final["stats"], "concordance": conc["concordance"]},
        "memo": memo,
        "published": published,
        "produced_by": final["vcf"].produced_by,
        "driver_seconds": round(time.time() - t0, 1),
    }


# ----------------------------------------------------------------------------------------------
# Client side
# ----------------------------------------------------------------------------------------------


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:4]


def preview(run_id: str, salt: str) -> None:
    """dry_run the whole plan: nothing is created, pinned, hashed or looked up."""
    size = -(-SLICE_LEN // N_WINDOWS)
    bam, ref, manifest = inputs(DATA)
    # Downstream inputs do not exist yet; stand-ins with the right shape are enough to size the plan.
    fake = lambda name: sk8s.artifact(f"{RESULTS}{run_id}/placeholder/{name}")  # noqa: E731
    o: dict[str, Any] = {"dry_run": True, "memo": True, "memo_salt": salt}
    plan: list[tuple[str, Any]] = [
        ("driver (detached)", sk8s.submit_workflow(pipeline, run_id, DATA, RESULTS, salt, dry_run=True)),
        ("1 fetch_fastq", sk8s.submit(fetch_fastq, bam, ref, manifest, requests=R_FETCH, expect=gate_fastq, **o)),
        ("2 align", sk8s.submit(align, fake("r1.fq.gz"), fake("r2.fq.gz"), ref, requests=R_ALIGN, expect=gate_alignment, **o)),
        (
            f"3 call_window x{N_WINDOWS}",
            sk8s.starmap(
                call_window,
                [(fake("aln.bam"), ref, i, i * size + 1, min((i + 1) * size, SLICE_LEN)) for i in range(N_WINDOWS)],
                pilot=1, requests=R_CALL, expect=gate_window, **o,
            ),
        ),
        ("4 merge", sk8s.submit(merge_calls, [fake("win00.vcf.gz")], ref, {}, requests=R_MERGE, expect=gate_calls, **o)),
        (
            "5 concordance",
            sk8s.submit(
                concordance_check, fake("NA12878.BRCA2.vcf.gz"), fake("stats.json"), ref, *truth_inputs(GIAB),
                requests=R_CONCORD, expect=gate_concordance, **o,
            ),
        ),
    ]
    print(f"PLAN for run {run_id}  (dry_run: nothing created; memo salt {salt!r})")
    for label, dr in plan:
        print(
            f"  {label:<22} jobs={dr.n_jobs}  cpu={dr.total_cpu_request:g}  "
            f"mem={dr.total_memory_request / 2**30:.0f}Gi  artifacts={dr.n_artifacts}  ns={dr.namespace}"
        )
    first = plan[0][1]
    print(f"  image: {first.image}\n  store: {first.store}")
    for label, dr in plan:
        for note in dr.notes:
            print(f"  note[{label}]: {note}")


def run_once(run_id: str, salt: str, tethered: bool) -> dict[str, Any]:
    t0 = time.time()
    if tethered:
        print(f"driving run_id={run_id} from this process (tethered; no driver pod)", flush=True)
        summary: dict[str, Any] = pipeline(run_id, DATA, RESULTS, salt)
        summary["wf_id"] = "(tethered)"
    else:
        wf = sk8s.submit_workflow(pipeline, run_id, DATA, RESULTS, salt)
        print(f"submitted detached workflow  wf.id={wf.id}  run_id={run_id}", flush=True)
        print(f"  watch:  sk8s explain {wf.id}   |   sk8s logs {wf.id}", flush=True)
        summary = wf.result(delete=False)  # raises the driver's error (e.g. ExpectationFailed)
        summary["wf_id"] = wf.id
    summary["wall_seconds"] = round(time.time() - t0, 1)
    return summary


def prove_gate(salt: str) -> None:
    """Show a gate FAILING on a real result: stages 1-2 are memo hits (free), then a 200 bp window
    is called and must be rejected before anything is uploaded or memoized."""
    bam, ref, manifest = inputs(DATA)
    opts: dict[str, Any] = {"memo": True, "memo_salt": salt}
    fq = sk8s.run(fetch_fastq, bam, ref, manifest, requests=R_FETCH, expect=gate_fastq, **opts)
    al = sk8s.run(align, fq["r1"], fq["r2"], ref, requests=R_ALIGN, expect=gate_alignment, **opts)
    try:
        sk8s.run(call_window, al["bam"], ref, 99, 1, 200, requests=R_CALL, expect=gate_window, **opts)
    except sk8s.ExpectationFailed as e:
        print(f"gate correctly REJECTED a bad result: ExpectationFailed: {e.message}")
    else:
        raise SystemExit("gate did not fire")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=1, help="runs in sequence (fresh run-id each); counts must match")
    ap.add_argument("--memo-salt", default="v1", help="change it to invalidate every stage's cache")
    ap.add_argument("--tethered", action="store_true", help="drive the pipeline from this process, no driver pod")
    ap.add_argument("--preview-only", action="store_true", help="describe + dry_run, then stop")
    ap.add_argument("--prove-gate", action="store_true", help="show expect= rejecting a bad result")
    args = ap.parse_args()
    if args.prove_gate:
        prove_gate(args.memo_salt)
        return 0

    d = sk8s.describe(DATA)
    print("DESCRIBE", DATA)
    print(" ", d["headline"])
    print(json.dumps({k: d[k] for k in ("formats", "patterns") if k in d}, indent=2))
    preview(new_run_id(), args.memo_salt)
    if args.preview_only:
        return 0

    runs = []
    for _ in range(args.repeat):
        s = run_once(new_run_id(), args.memo_salt, args.tethered)
        v, up = s["stats"]["variants"], s["stats"]["upstream"]
        print(
            f"RUN {s['run_id']} wf={s['wf_id']}: {up['fastq']['bam_records']} reads in -> "
            f"{up['alignment']['reads_mapped']} aligned ({up['alignment']['pct_mapped']}%) -> "
            f"{v['total']} variants {v['by_type']} ts/tv={v['ts_tv']}  het={v['het']} hom={v['hom_alt']}  "
            f"wall={s['wall_seconds']}s",
            flush=True,
        )
        a = s["stats"]["concordance"]["metrics"]["all"]
        print(
            f"  concordance vs GIAB v4.2.1 (site+GT, confident BED): recall={a['recall']} precision={a['precision']} "
            f"f1={a['f1']} gt_match={a['gt_match_rate']}  (tp={a['tp']} fp={a['fp']} fn={a['fn']}; "
            f"snv f1={s['stats']['concordance']['metrics']['snv']['f1']} indel f1={s['stats']['concordance']['metrics']['indel']['f1']})",
            flush=True,
        )
        print(f"  memo: {s['memo']}")
        print("  published:", *s["published"].values(), sep="\n    ")
        runs.append(s)
    if len(runs) > 1:
        counts = [r["stats"]["variants"]["total"] for r in runs]
        md5s = [r["stats"]["records_md5"] for r in runs]
        assert len(set(counts)) == 1, f"variant counts differ between runs: {counts}"
        print(f"REPRODUCIBLE: {len(runs)} runs, identical variant count {counts[0]}; record md5 identical: {len(set(md5s)) == 1}")
        cj = {json.dumps(r["stats"]["concordance"], sort_keys=True) for r in runs}
        assert len(cj) == 1, "concordance differs between runs"
        print("  concordance identical across runs: True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
