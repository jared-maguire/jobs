"""CRAM -> FASTQ -> alignment -> variants -> concordance, for every 1kG sample, over BRCA2.

One sk8s job per small batch of samples; each sample is processed end to end inside the
worker (ranged CRAM fetch, mate-aware FASTQ, bwa against the BRCA2 slice reference,
bcftools call, lift to chr13, concordance against the published 1kG phased panel).
Detached, memoized, budget-capped, failures harvested rather than fatal.

Concordance semantics: the published panel is PASS-only with minor allele count >= 2,
so singletons/private variants are absent by construction. We therefore report
  - recall of the sample's published alt genotypes (well-defined), and
  - "extra" calls not in the panel as a separate rate, never as false positives.

Run:  python examples/cohort_brca2.py --limit 8        # smoke/pilot
      python examples/cohort_brca2.py --fleet          # all 2,504 unrelated samples
      python examples/cohort_brca2.py --dry-run --fleet
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import sk8s

# --- pinned inputs (all mirrored, provenance in the MANIFESTs beside them) --------------------
BUCKET = "jared-genome-analysis-sk8s"
REGION = "chr13:32305086-32410268"          # BRCA2 +/- 10kb, GRCh38
OFFSET = 32305085                            # slice coord + OFFSET = chr13 coord
SLICE_FA = f"gs://{BUCKET}/demo-data/na12878-brca2/GRCh38.chr13_32305086_32410268.fa"
CHR13_FA = f"gs://{BUCKET}/demo-data/cohort-eval/ref/chr13.fa"      # CRAM decode needs the real contig
PANEL_VCF = f"gs://{BUCKET}/demo-data/cohort-eval/published/panel.BRCA2.vcf.gz"
SAMPLES_TSV = f"gs://{BUCKET}/demo-data/cohort-eval/samples/samples.tsv"
RESULTS = f"gs://{BUCKET}/sk8s-cohort/results"

SLICE_CONTIG = "chr13_32305086_32410268"
CACHE = Path("/tmp/cohort-cache")            # shared inside a pod; batch_size>1 reuses it


def _run(cmd: str) -> None:
    subprocess.run(cmd, shell=False if isinstance(cmd, list) else True, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _sh(cmd: str) -> str:
    p = subprocess.run(["bash", "-ceo", "pipefail", cmd], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.stdout


def _gs_fetch(uri: str, dest: Path) -> Path:
    """Download a gs:// object once per pod (idempotent across a batch)."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    from google.cloud import storage  # in the worker image

    dest.parent.mkdir(parents=True, exist_ok=True)
    bucket_name, _, key = uri[len("gs://"):].partition("/")
    tmp = dest.with_suffix(dest.suffix + ".part")
    storage.Client().bucket(bucket_name).blob(key).download_to_filename(str(tmp))
    os.replace(tmp, dest)
    return dest


def _prep_shared() -> dict[str, Path]:
    """Reference + panel, cached per pod; slice bwa index built once."""
    sfa = _gs_fetch(SLICE_FA, CACHE / "slice.fa")
    _gs_fetch(SLICE_FA + ".fai", CACHE / "slice.fa.fai")
    c13 = _gs_fetch(CHR13_FA, CACHE / "chr13.fa")
    _gs_fetch(CHR13_FA + ".fai", CACHE / "chr13.fa.fai")
    panel = _gs_fetch(PANEL_VCF, CACHE / "panel.vcf.gz")
    _gs_fetch(PANEL_VCF + ".tbi", CACHE / "panel.vcf.gz.tbi")
    if not (CACHE / "slice.fa.bwt").exists():
        _sh(f"bwa index {sfa}")
    return {"slice": sfa, "chr13": c13, "panel": panel}


def _norm_gt(gt: str) -> tuple[str, ...]:
    return tuple(sorted(gt.replace("|", "/").split("/")))


def _read_sites(vcf_text: str) -> dict[tuple[int, str, str], tuple[str, ...]]:
    """pos/ref/alt -> normalized GT, from bcftools-view text output."""
    sites: dict[tuple[int, str, str], tuple[str, ...]] = {}
    for line in vcf_text.splitlines():
        if line.startswith("#"):
            continue
        p = line.split("\t")
        gt = p[9].split(":")[0] if len(p) > 9 else "?"
        sites[(int(p[1]), p[3], p[4])] = _norm_gt(gt)
    return sites


def eval_sample(sample: str, cram_url: str, run_id: str) -> dict:
    """The whole pipeline for one sample. Runs in the worker."""
    refs = _prep_shared()
    wd = Path(f"/tmp/{sample}")
    wd.mkdir(exist_ok=True)
    t: dict[str, float] = {}
    env_note = ""

    t0 = time.time()
    # 1. ranged CRAM fetch (decode against the real chr13 contig), region BAM.
    # The S3 mirror's .crai is a ZERO-BYTE object for a couple of samples (HG00619/620),
    # so fetch the index ourselves and fall back to the EBI mirror's crai when empty.
    import urllib.request
    crai = wd / "aln.crai"
    ebi_run = cram_url.rstrip("/").rsplit("/", 2)[-2]          # e.g. ERR3241719
    ebi_crai = (f"https://ftp.sra.ebi.ac.uk/vol1/run/{ebi_run[:6]}/{ebi_run}/"
                f"{sample}.final.cram.crai")
    for url in (f"{cram_url}.crai", ebi_crai):
        try:
            urllib.request.urlretrieve(url, crai)
            if crai.stat().st_size > 0:
                break
        except Exception:
            continue
    if not crai.exists() or crai.stat().st_size == 0:
        raise RuntimeError(f"no usable .crai for {sample} (tried S3 + EBI)")
    _sh(f"cd {wd} && samtools view -b -T {refs['chr13']} "
        f"-X '{cram_url}' aln.crai {REGION} -o region.bam && samtools index region.bam")
    n_reads = int(_sh(f"samtools view -c {wd}/region.bam").strip())
    t["fetch_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    # 2. mate-aware FASTQ  3. one bwa pass vs the slice  (coords become slice-local)
    _sh(f"cd {wd} && samtools collate -O region.bam | "
        f"samtools fastq -n -F 0x900 -1 r1.fq -2 r2.fq -s rs.fq - 2>/dev/null")
    _sh(f"cd {wd} && bwa mem -t 2 -R '@RG\\tID:{sample}\\tSM:{sample}' {refs['slice']} r1.fq r2.fq "
        f"2>/dev/null | samtools fixmate -m - - | samtools sort - | "
        f"samtools markdup - aln.bam && samtools index aln.bam")
    t["align_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    # 4. call + normalize + lift back to chr13
    _sh(f"cd {wd} && bcftools mpileup -f {refs['slice']} aln.bam 2>/dev/null | "
        f"bcftools call -mv 2>/dev/null | bcftools norm -f {refs['slice']} -m -any -Ov 2>/dev/null "
        f"| awk 'BEGIN{{OFS=\"\\t\"}} /^#/ {{print; next}} {{$1=\"chr13\"; $2=$2+{OFFSET}; print}}' "
        f"> calls.chr13.vcf")
    t["call_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    # 5. concordance vs the published panel (this sample's alt sites; MAC>=2 panel)
    # NB: -s and -i must be separate stages — in one `bcftools view`, -i evaluates
    # BEFORE the -s subset, so GT[0] would be the panel's first sample, not ours.
    pub = _sh(f"bcftools view -s {sample} {refs['panel']} 2>/dev/null | "
              f"bcftools view -i 'GT[0]=\"alt\"' 2>/dev/null | "
              f"bcftools norm -m -any -Ov 2>/dev/null")
    published = _read_sites(pub)
    ours = _read_sites((wd / "calls.chr13.vcf").read_text())
    tp = {k for k in published if k in ours}
    fn = {k for k in published if k not in ours}
    gt_match = sum(1 for k in tp if published[k] == ours[k])
    extras = len([k for k in ours if k not in published])
    t["conc_s"] = round(time.time() - t0, 1)

    n_pub = len(published)
    return {
        "sample": sample, "run_id": run_id, "n_reads": n_reads,
        "published_alt_sites": n_pub, "tp": len(tp), "fn": len(fn),
        "recall": round(len(tp) / n_pub, 4) if n_pub else None,
        "gt_match": gt_match,
        "gt_match_rate": round(gt_match / len(tp), 4) if tp else None,
        "extra_calls": extras,  # not FPs: panel is PASS-only, MAC>=2 (singletons absent)
        "fn_sites": sorted([f"{p}:{r}>{a}" for (p, r, a) in fn])[:50],
        "timings": t, "note": env_note,
    }


def gate(result: dict) -> str | None:
    if result["n_reads"] < 5000:
        return f"only {result['n_reads']} reads fetched for {result['sample']}"
    # >=10 guards against a broken panel subset (which gives ~0); real samples
    # range 23-229 alt sites in BRCA2 (fleet-1 measured), so 30 was too strict.
    if result["published_alt_sites"] < 10:
        return f"only {result['published_alt_sites']} published alt sites for {result['sample']}"
    return None  # recall itself is the measurement, not a gate: populations vary


def load_samples(limit: int | None) -> list[tuple[str, str]]:
    from google.cloud import storage

    bucket_name, _, key = SAMPLES_TSV[len("gs://"):].partition("/")
    text = storage.Client().bucket(bucket_name).blob(key).download_as_text()
    rows = [r for r in csv.DictReader(io.StringIO(text), delimiter="\t")
            if r["cohort"] == "unrelated2504"]
    rows.sort(key=lambda r: r["sample"])
    if limit:
        rows = rows[:limit]
    # NYGC's upstream index has stray spaces inside a few filenames ("HG02635 .final.cram");
    # the real S3 keys have none.
    return [(r["sample"].strip(), r["s3_cram_uri"].replace(" ", "")) for r in rows]


def eval_chunk(chunk: list[tuple[str, str]], run_id: str) -> list[dict]:
    """Run many samples inside ONE pod: amortizes pod startup and _prep_shared()
    (the ~116MB chr13 + panel download) across the whole chunk, while keeping
    per-sample failure harvesting — one bad sample never fails its chunk.
    memo=True stays legal because the library still sees batch_size=1 (of chunks).
    """
    out: list[dict] = []
    for sample, cram_url in chunk:
        try:
            r = eval_sample(sample, cram_url, run_id)
            bad = gate(r)  # same worker-side QC, applied per sample not per Job
            if bad:
                r = {"sample": sample, "error": f"ExpectationFailed: {bad}"}
            out.append(r)
        except Exception as e:
            out.append({"sample": sample, "error": f"{type(e).__name__}: {e}"[:300]})
        finally:
            # 28 samples × ~35MB of leftover BAM/FASTQ blows the pod's ephemeral-storage
            # limit (fleet-3 lost 4 chunks to eviction); keep only the shared ref cache
            subprocess.run(["rm", "-rf", f"/tmp/{sample}"], check=False)
    return out


def main(run_id: str, limit: int | None = None, chunk_size: int = 28) -> dict:
    """The detached driver: fan out chunks, harvest, aggregate."""
    sk8s.configure(volume_wait_seconds=3600)
    samples = load_samples(limit)
    # 2,504 samples / 28 ≈ 90 Jobs = one wave at max_concurrent: pod+prep overhead is
    # paid 90 times instead of 2,504 (Jared's chunking note, 2026-10-14)
    results: list[dict] = []
    failures: dict[str, str] = {}

    def run_wave(wave: list[tuple[str, str]], tag: str) -> list[tuple[str, str]]:
        """Submit one wave of chunks; harvest; return samples lost to WHOLE-CHUNK
        (infra) failures so the caller can retry them. Per-sample errors are final."""
        chunks = [wave[i:i + chunk_size] for i in range(0, len(wave), chunk_size)]
        futs = sk8s.submit_starmap(
            eval_chunk, [(c, run_id) for c in chunks],
            batch_size=1, max_concurrent=90,
            requests={"cpu": "2", "memory": "3Gi", "ephemeral-storage": "4Gi"}, memo=True,
        )
        lost: list[tuple[str, str]] = []
        for c, fut in zip(chunks, futs):
            try:
                for r in fut.result():
                    if "error" in r:
                        failures[r["sample"]] = r["error"]
                    else:
                        results.append(r)
            except Exception as e:  # whole-chunk infra failure (e.g. Autopilot eviction)
                lost.extend(c)
                for s, _ in c:
                    failures[s] = f"chunk[{tag}]: {type(e).__name__}: {e}"[:300]
        return lost

    # Autopilot's scale-down taint-evicts multi-minute chunk pods (fleet-4 lost 2 chunks
    # that way), so retry infra-lost samples once; memo makes the retry touch only them.
    lost = run_wave(samples, "wave1")
    if lost:
        for s, _ in lost:
            failures.pop(s, None)
        lost = run_wave(lost, "retry")

    recalls = [r["recall"] for r in results if r["recall"] is not None]
    gtr = [r["gt_match_rate"] for r in results if r["gt_match_rate"] is not None]
    # house convention: headline metrics are aggregated over ALL calls in ALL samples
    # (micro-averaged), not medians of per-sample rates; per-sample stats stay as detail
    tp = sum(r["tp"] for r in results)
    fn = sum(r["fn"] for r in results)
    gt = sum(r["gt_match"] for r in results)
    extras = sum(r["extra_calls"] for r in results)
    summary = {
        "run_id": run_id, "samples_requested": len(samples),
        "samples_ok": len(results), "samples_failed": len(failures),
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "gt_match": round(gt / tp, 4) if tp else None,
        "tp": tp, "fn": fn, "extra_calls": extras,
        "recall_median": round(statistics.median(recalls), 4) if recalls else None,
        "recall_mean": round(statistics.fmean(recalls), 4) if recalls else None,
        "recall_min": min(recalls) if recalls else None,
        "gt_match_median": round(statistics.median(gtr), 4) if gtr else None,
        "worst_samples": sorted(results, key=lambda r: (r["recall"] or 0))[:10],
        "failures": failures,
        "method": "per-sample CRAM->FQ->bwa(slice)->bcftools->site+GT concordance vs the "
                  "1kG 2020-10 phased panel (PASS, MAC>=2: singletons absent; 'extra_calls' "
                  "are therefore not false positives). Region BRCA2+/-10kb GRCh38.",
    }
    from sk8s import store as _store  # store the full per-sample table alongside the summary
    cfg = sk8s.load_config()
    st = _store.get_store(cfg.store, namespace=cfg.namespace, endpoint=cfg.store_endpoint)
    st.put(f"cohort/{run_id}/results.json", json.dumps(results).encode())
    st.put(f"cohort/{run_id}/summary.json", json.dumps(summary, indent=1).encode())
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fleet", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-id", default=time.strftime("cohort-%Y%m%d-%H%M%S"))
    ap.add_argument("--check", action="store_true",
                    help="run the full driver path locally with a stub worker (validates "
                         "fan-out options, gates, harvesting, aggregation; no cluster)")
    a = ap.parse_args()
    if a.check:
        import unittest.mock as _m
        stub = lambda s, u, r: {"sample": s, "run_id": r, "n_reads": 9999,
                                "published_alt_sites": 100, "tp": 99, "fn": 1, "recall": 0.99,
                                "gt_match": 99, "gt_match_rate": 1.0, "extra_calls": 2,
                                "fn_sites": [], "timings": {}, "note": ""}
        sk8s.configure(cluster=sk8s.LocalCluster())
        with _m.patch(f"{__name__}.eval_sample", stub), \
             _m.patch(f"{__name__}.load_samples", lambda limit: [(f"S{i}", "u") for i in range(limit or 4)]):
            out = main("check", limit=4)
        sk8s.configure()
        assert out["samples_ok"] == 4 and out["samples_failed"] == 0, out
        print("driver path OK: fan-out options, gates, harvest, aggregation all exercised locally")
        sys.exit(0)
    if not a.fleet and a.limit is None:
        a.limit = 8
    budget = sk8s.Budget(max_estimated_usd=600, max_concurrent_pods=95, assumed_cpu_when_unset=2)
    if a.dry_run:
        prev = sk8s.submit_workflow(main, a.run_id, limit=a.limit, dry_run=True, budget=budget)
        print(json.dumps(prev.to_dict(), indent=1))
        sys.exit(0)
    wf = sk8s.submit_workflow(main, a.run_id, limit=a.limit, budget=budget)
    print(f"workflow {wf.id}  run_id {a.run_id}  samples "
          f"{a.limit if a.limit else 'ALL unrelated (2504)'}")
    print(f"watch:   sk8s explain {wf.id}")
    print(f"result:  sk8s.attach('{wf.id}').result(delete=False)")
