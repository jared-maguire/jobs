#!/usr/bin/env python
"""NA12878 / BRCA2: reads -> alignment -> SNVs + indels -> VCF, as a detached sk8s workflow.

The request, verbatim: "Fetch reads from NA12878 from some small tractable genomic region
(e.g. the BRCA2 gene), align them, call SNVs and Indels there, and create a VCF."

What it does (see examples/README.md for the one-page version):

    describe the input  ->  dry_run preview of the plan  ->  submit a DETACHED driver which runs
      1. fetch_fastq   stage the pinned region BAM + reference slice from the GCS mirror,
                       verify md5s against MANIFEST.json, extract mate-aware FASTQ     [expect=]
      2. align         bwa index + bwa mem, fixmate, sort, markdup, index              [expect=]
      3. call_window   bcftools mpileup | call -mv over N windows, sk8s.map(pilot=1)   [expect=]
      4. merge         concat, bcftools norm, lift slice coords -> chr13, bgzip+tabix,
                       stats.json, publish to results/<run-id>/                        [expect=]

Inputs are PINNED (gs://.../demo-data/na12878-brca2/, provenance in its MANIFEST.json): 26,841
reads of 1000 Genomes NYGC 30x NA12878, GRCh38, BRCA2 +/-10kb = chr13:32,305,086-32,410,268, plus
the matching 105,183 bp reference slice (contig ``chr13_32305086_32410268``).

Environment (nothing else is read implicitly):

    KUBECONFIG    kubeconfig whose current context is the GKE cluster
    SK8S_CONFIG   toml with namespace / store (gs://...) / the Workload-Identity-ready namespace
    SK8S_IMAGE    worker image = sk8s worker base + bwa/samtools/bcftools (examples/Dockerfile.na12878);
                  its Python minor MUST equal this client's (3.14 for the demo image)

Usage:
    python examples/na12878_brca2.py                  # describe, preview, one full run
    python examples/na12878_brca2.py --repeat 2       # two fresh runs, assert identical variant counts
    python examples/na12878_brca2.py --preview-only   # describe + dry_run plan, touch nothing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

import sk8s

DATA = "gs://jared-genome-analysis-sk8s/demo-data/na12878-brca2/"
RESULTS = "gs://jared-genome-analysis-sk8s/sk8s-demo/results/"

CONTIG = "chr13_32305086_32410268"  # reference-slice contig name
SLICE_LEN = 105_183
OFFSET = 32_305_085  # chr13 position = slice position + OFFSET (slice is 1-based)
CHR13_LEN = 114_364_328  # GRCh38 chr13, only used for the lifted ##contig header line
N_WINDOWS = 4

BAM = f"NA12878.{CONTIG}.bam"
REF = f"GRCh38.{CONTIG}.fa"

# Per-stage resources. Autopilot rounds these up; nothing here asserts exact values.
R_FETCH = {"cpu": "1", "memory": "2Gi"}
R_ALIGN = {"cpu": "2", "memory": "4Gi"}
R_CALL = {"cpu": "1", "memory": "2Gi"}
R_MERGE = {"cpu": "1", "memory": "2Gi"}


# ----------------------------------------------------------------------------------------------
# Everything below the next marker runs IN THE CLUSTER (this file's __main__ functions pickle by
# value, so the image needs no copy of this script - only sk8s[gcs] and the bio tools).
# ----------------------------------------------------------------------------------------------


def _blob(uri: str) -> Any:
    from google.cloud import storage  # type: ignore[import-untyped,unused-ignore]

    bucket, _, name = uri.removeprefix("gs://").partition("/")
    return storage.Client().bucket(bucket).blob(name)


def _download(uri: str, dest: str) -> None:
    _blob(uri).download_to_filename(dest)


def _upload(src: str, uri: str) -> None:
    _blob(uri).upload_from_filename(src)


def _sh(script: str, cwd: str) -> str:
    """Run a bash pipeline with errexit+pipefail; on failure raise with the stderr tail."""
    import subprocess

    p = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], cwd=cwd, capture_output=True, text=True
    )
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {script[:200]}\n{p.stderr[-1500:]}")
    return p.stdout


def _md5(path: str) -> str:
    import hashlib

    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_fastq(data: str, work: str) -> dict[str, Any]:
    """Stage the mirror, verify it against MANIFEST.json, and extract mate-aware FASTQ."""
    import tempfile

    t0 = time.time()
    d = tempfile.mkdtemp()
    for name in (BAM, REF, "MANIFEST.json"):
        _download(data + name, f"{d}/{name}")
    files = json.load(open(f"{d}/MANIFEST.json"))["files"]
    for name in (BAM, REF):
        got, want = _md5(f"{d}/{name}"), files[name]["md5"]
        if got != want:
            raise RuntimeError(f"{name}: md5 {got} != manifest {want} (mirror corrupted?)")
    # Mate-aware: collate groups mates, -F 0x900 drops secondary/supplementary records, complete
    # pairs go to r1/r2 and orphans (mate outside the window) to singletons.fq (not aligned).
    _sh(
        f"mkdir -p tmp && samtools collate -u -O {BAM} tmp/col"
        " | samtools fastq -n -F 0x900 -1 r1.fq -2 r2.fq -0 /dev/null -s singletons.fq"
        " && gzip -n r1.fq r2.fq",
        d,
    )
    records = int(_sh(f"samtools view -c {BAM}", d))
    pairs = int(_sh("zcat r1.fq.gz | wc -l", d)) // 4
    singletons = int(_sh("wc -l < singletons.fq", d)) // 4
    _upload(f"{d}/r1.fq.gz", work + "r1.fq.gz")
    _upload(f"{d}/r2.fq.gz", work + "r2.fq.gz")
    return {
        "bam_records": records,
        "pairs": pairs,
        "singletons_dropped": singletons,
        "md5_verified": [BAM, REF],
        "seconds": round(time.time() - t0, 1),
    }


def align(data: str, work: str) -> dict[str, Any]:
    """bwa index + mem against the reference slice; fixmate/sort/markdup; index; count."""
    import tempfile

    t0 = time.time()
    d = tempfile.mkdtemp()
    for name in (REF, f"{REF}.fai"):
        _download(data + name, f"{d}/{name}")
    for name in ("r1.fq.gz", "r2.fq.gz"):
        _download(work + name, f"{d}/{name}")
    rg = r"@RG\tID:NA12878\tSM:NA12878\tPL:illumina"
    _sh(f"bwa index {REF}", d)
    # -K fixes the batch size so the result cannot depend on the thread count.
    _sh(
        f"bwa mem -t 2 -K 100000000 -R '{rg}' {REF} r1.fq.gz r2.fq.gz"
        " | samtools fixmate -m -u - -"
        " | samtools sort -u -"
        " | samtools markdup - aln.bam"
        " && samtools index aln.bam",
        d,
    )
    primary = int(_sh("samtools view -c -F 0x900 aln.bam", d))
    mapped = int(_sh("samtools view -c -F 0x904 aln.bam", d))
    dups = int(_sh("samtools view -c -F 0x900 -f 0x400 aln.bam", d))
    _upload(f"{d}/aln.bam", work + "aln.bam")
    _upload(f"{d}/aln.bam.bai", work + "aln.bam.bai")
    return {
        "reads_primary": primary,
        "reads_mapped": mapped,
        "pct_mapped": round(100.0 * mapped / max(primary, 1), 3),
        "duplicates_marked": dups,
        "seconds": round(time.time() - t0, 1),
    }


def call_window(data: str, work: str, idx: int, start: int, end: int) -> dict[str, Any]:
    """SNV + indel calls (bcftools mpileup | call -mv) for one slice window, 1-based inclusive."""
    import tempfile

    t0 = time.time()
    d = tempfile.mkdtemp()
    for name in (REF, f"{REF}.fai"):
        _download(data + name, f"{d}/{name}")
    for name in ("aln.bam", "aln.bam.bai"):
        _download(work + name, f"{d}/{name}")
    _sh(
        f"bcftools mpileup -f {REF} -r {CONTIG}:{start}-{end} -a AD,DP -Ou aln.bam"
        f" | bcftools call -mv -Oz -o win.vcf.gz",
        d,
    )
    records = int(_sh("bcftools view -H win.vcf.gz | wc -l", d))
    _upload(f"{d}/win.vcf.gz", f"{work}win{idx:02d}.vcf.gz")
    return {
        "window": idx,
        "region": f"{CONTIG}:{start}-{end}",
        "records": records,
        "seconds": round(time.time() - t0, 1),
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


def merge_publish(
    data: str, work: str, out: str, n_windows: int, upstream: dict[str, Any]
) -> dict[str, Any]:
    """concat windows -> bcftools norm -> lift to chr13 -> bgzip+tabix -> stats.json -> publish."""
    import hashlib
    import tempfile

    t0 = time.time()
    d = tempfile.mkdtemp()
    for name in (REF, f"{REF}.fai"):
        _download(data + name, f"{d}/{name}")
    wins = [f"win{i:02d}.vcf.gz" for i in range(n_windows)]
    for name in wins:
        _download(work + name, f"{d}/{name}")
    # Windows are disjoint and ordered, so plain concat suffices. norm: left-align indels against
    # the slice and split multiallelics (-m -any) so every record is one SNP or one indel.
    _sh(
        f"bcftools concat {' '.join(wins)} -Ou"
        f" | bcftools norm -f {REF} -m -any -Oz -o normed.vcf.gz",
        d,
    )
    open(f"{d}/lifted.vcf", "w").write(_lift(_sh("bcftools view normed.vcf.gz", d)))
    _sh(
        "bcftools view -Oz -o NA12878.BRCA2.vcf.gz lifted.vcf"
        " && bcftools index -t NA12878.BRCA2.vcf.gz",
        d,
    )
    rows = [
        r.split("\t")
        for r in _sh(
            "bcftools query -f '%CHROM\\t%POS\\t%REF\\t%ALT\\t%TYPE\\t[%GT]\\n' NA12878.BRCA2.vcf.gz", d
        ).splitlines()
    ]
    by_type: dict[str, int] = {}
    ts = tv = het = hom = 0
    transitions = {("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")}
    for _chrom, _pos, ref, alt, typ, gt in rows:
        by_type[typ] = by_type.get(typ, 0) + 1
        if typ == "SNP":
            if (ref, alt) in transitions:
                ts += 1
            else:
                tv += 1
        a = gt.replace("|", "/").split("/")
        if len(set(a)) > 1:
            het += 1
        elif a[0] not in (".", "0"):
            hom += 1
    body = _sh("bcftools view -H NA12878.BRCA2.vcf.gz", d)
    stats = {
        "run_id": out.rstrip("/").rsplit("/", 1)[-1],
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
    json.dump(stats, open(f"{d}/stats.json", "w"), indent=2, sort_keys=True)
    for name in ("NA12878.BRCA2.vcf.gz", "NA12878.BRCA2.vcf.gz.tbi", "stats.json"):
        _upload(f"{d}/{name}", out + name)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["outputs"] = [out + n for n in ("NA12878.BRCA2.vcf.gz", "NA12878.BRCA2.vcf.gz.tbi", "stats.json")]
    return stats


# ---- QC gates (expect=): run in the worker on each stage's return value --------------------------


def gate_fastq(r: dict[str, Any]) -> str | None:
    if r["pairs"] < 10_000:
        return f"only {r['pairs']} complete pairs extracted (expect ~13,236)"
    return None


def gate_alignment(r: dict[str, Any]) -> str | None:
    if r["reads_mapped"] < 20_000:
        return f"only {r['reads_mapped']} reads aligned (need >= 20,000)"
    if r["pct_mapped"] <= 90.0:
        return f"only {r['pct_mapped']}% mapped (need > 90%)"
    return None


def gate_window(r: dict[str, Any]) -> str | None:
    # ~26 kb of a human genome carries dozens of NA12878 variants; none/very few means the pileup broke.
    if r["records"] < 10:
        return f"window {r['window']} ({r['region']}) has only {r['records']} calls (need >= 10)"
    return None


def gate_calls(r: dict[str, Any]) -> str | None:
    v = r["variants"]
    if v["total"] == 0:
        return "VCF is empty"
    if v["total"] < 50:
        return f"only {v['total']} VCF records (need >= 50)"
    if v["ts_tv"] is None or not 1.0 <= v["ts_tv"] <= 4.0:
        return f"ts/tv {v['ts_tv']} outside [1.0, 4.0] (ts={v['ts']}, tv={v['tv']})"
    return None


# ---- the detached driver ---------------------------------------------------------------------------


def pipeline(run_id: str, data: str, results: str) -> dict[str, Any]:
    """Runs IN the cluster as the workflow driver; chains the per-stage jobs."""
    work = f"{results}{run_id}/work/"
    out = f"{results}{run_id}/"
    t0 = time.time()
    print(f"[{run_id}] stage 1/4 fetch_fastq", flush=True)  # `sk8s logs <wf>` shows these
    fq = sk8s.run(fetch_fastq, data, work, requests=R_FETCH, expect=gate_fastq)
    print(f"[{run_id}] gate ok: {fq['pairs']} pairs; stage 2/4 align", flush=True)
    al = sk8s.run(align, data, work, requests=R_ALIGN, expect=gate_alignment)
    print(f"[{run_id}] gate ok: {al['reads_mapped']} aligned ({al['pct_mapped']}%); stage 3/4 call x{N_WINDOWS}", flush=True)
    # Natural fan-out: the 105 kb slice is cut into N disjoint windows called in parallel. Because
    # calling is per-position over the one full BAM, this is equivalent to a single whole-region
    # call (verified: identical sites on this data), not an approximation. pilot=1: window 0 must
    # pass its gate before the rest are submitted.
    size = -(-SLICE_LEN // N_WINDOWS)
    spans = [(i, i * size + 1, min((i + 1) * size, SLICE_LEN)) for i in range(N_WINDOWS)]
    calls = sk8s.starmap(
        call_window,
        [(data, work, i, s, e) for i, s, e in spans],
        pilot=1,
        requests=R_CALL,
        expect=gate_window,
    )
    print(f"[{run_id}] gates ok: windows {[c['records'] for c in calls]}; stage 4/4 merge_publish", flush=True)
    upstream = {"fastq": fq, "alignment": al, "windows": calls}
    final = sk8s.run(
        merge_publish, data, work, out, N_WINDOWS, upstream, requests=R_MERGE, expect=gate_calls
    )
    final["driver_seconds"] = round(time.time() - t0, 1)
    print(f"[{run_id}] gate ok: {final['variants']['total']} variants, ts/tv {final['variants']['ts_tv']}", flush=True)
    return final


# ----------------------------------------------------------------------------------------------
# Client side
# ----------------------------------------------------------------------------------------------


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:4]


def preview(run_id: str) -> None:
    """dry_run the whole plan: nothing is created on the cluster."""
    work, out = f"{RESULTS}{run_id}/work/", f"{RESULTS}{run_id}/"
    size = -(-SLICE_LEN // N_WINDOWS)
    plan: list[tuple[str, Any]] = [
        ("driver (detached)", sk8s.submit_workflow(pipeline, run_id, DATA, RESULTS, dry_run=True)),
        ("1 fetch_fastq", sk8s.submit(fetch_fastq, DATA, work, dry_run=True, requests=R_FETCH, expect=gate_fastq)),
        ("2 align", sk8s.submit(align, DATA, work, dry_run=True, requests=R_ALIGN, expect=gate_alignment)),
        (
            f"3 call_window x{N_WINDOWS}",
            sk8s.starmap(
                call_window,
                [(DATA, work, i, i * size + 1, min((i + 1) * size, SLICE_LEN)) for i in range(N_WINDOWS)],
                dry_run=True, pilot=1, requests=R_CALL, expect=gate_window,
            ),
        ),
        ("4 merge_publish", sk8s.submit(merge_publish, DATA, work, out, N_WINDOWS, {}, dry_run=True, requests=R_MERGE, expect=gate_calls)),
    ]
    print(f"PLAN for run {run_id}  (dry_run: nothing created)")
    for label, dr in plan:
        print(
            f"  {label:<22} jobs={dr.n_jobs}  cpu={dr.total_cpu_request:g}  "
            f"mem={dr.total_memory_request / 2**30:.0f}Gi  ns={dr.namespace}"
        )
    first = plan[0][1]
    print(f"  image: {first.image}\n  store: {first.store}")
    for label, dr in plan:
        for note in dr.notes:
            print(f"  note[{label}]: {note}")


def run_once(run_id: str) -> dict[str, Any]:
    t0 = time.time()
    wf = sk8s.submit_workflow(pipeline, run_id, DATA, RESULTS)
    print(f"submitted detached workflow  wf.id={wf.id}  run_id={run_id}", flush=True)
    print(f"  watch:  sk8s explain {wf.id}   |   sk8s logs {wf.id}", flush=True)
    stats: dict[str, Any] = wf.result(delete=False)  # raises the driver's error (e.g. ExpectationFailed)
    stats["wf_id"] = wf.id
    stats["wall_seconds"] = round(time.time() - t0, 1)
    return stats


def prove_gate(work: str) -> None:
    """Show a gate FAILING: call a 200 bp window (a handful of variants at best) against the
    alignment of an earlier run. ``work`` is that run's .../work/ prefix. Cheap, one tiny job."""
    try:
        sk8s.run(call_window, DATA, work, 99, 1, 200, requests=R_CALL, expect=gate_window)
    except sk8s.ExpectationFailed as e:
        print(f"gate correctly REJECTED a bad result: ExpectationFailed: {e.message}")
    else:
        raise SystemExit("gate did not fire")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=1, help="fresh end-to-end runs; counts must match")
    ap.add_argument("--preview-only", action="store_true", help="describe + dry_run, then stop")
    ap.add_argument("--prove-gate", metavar="WORK_PREFIX", help="gs://.../results/<run-id>/work/ of a finished run: show expect= rejecting a bad result")
    args = ap.parse_args()
    if args.prove_gate:
        prove_gate(args.prove_gate)
        return 0

    d = sk8s.describe(DATA)
    print("DESCRIBE", DATA)
    print(" ", d["headline"])
    print(json.dumps({k: d[k] for k in ("formats", "patterns") if k in d}, indent=2))
    preview(new_run_id())
    if args.preview_only:
        return 0

    runs = []
    for _ in range(args.repeat):
        s = run_once(new_run_id())
        v = s["variants"]
        print(
            f"RUN {s['run_id']} wf={s['wf_id']}: {s['upstream']['fastq']['bam_records']} reads in -> "
            f"{s['upstream']['alignment']['reads_mapped']} aligned "
            f"({s['upstream']['alignment']['pct_mapped']}%) -> {v['total']} variants "
            f"{v['by_type']} ts/tv={v['ts_tv']}  het={v['het']} hom={v['hom_alt']}  "
            f"wall={s['wall_seconds']}s",
            flush=True,
        )
        print("  outputs:", *s["outputs"], sep="\n    ")
        runs.append(s)
    if len(runs) > 1:
        counts = [r["variants"]["total"] for r in runs]
        md5s = [r["records_md5"] for r in runs]
        assert len(set(counts)) == 1, f"variant counts differ between runs: {counts}"
        print(f"REPRODUCIBLE: {len(runs)} runs, identical variant count {counts[0]}; record md5 identical: {len(set(md5s)) == 1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
