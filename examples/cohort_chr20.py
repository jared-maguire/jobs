"""CRAM -> FASTQ -> alignment -> variants -> concordance for every 1kG sample, whole chr20,
window-scattered: chr20 is split into fixed genomic windows and each (sample, window) is an
independent task, so one sample's chromosome is called in parallel ("multiple chunks per
sample for speed" — Jared, 2026-10-14).

Differences from the BRCA2 pipeline (examples/cohort_brca2.py):
  - alignment is against the FULL chr20 contig (pre-built bwa index mirrored in GCS), so
    there is no slice/lift step; calling is confined per task with `-r <window>`
  - reads are fetched with a small pad around the window so boundary-spanning pairs align,
    but calls and the panel subset both use the exact window partition (keyed by POS), so a
    variant belongs to exactly one window on both sides of the comparison
  - a window inside an assembly gap (centromere/telomere) legitimately has ~no reads and
    ~no panel sites: that is a "desert" result, not a failure

Concordance semantics are unchanged: the published 2020-10 phased panel is PASS-only
MAC>=2, so recall of published alt genotypes is well-defined and "extras" are never
counted as false positives.

Run:  python examples/cohort_chr20.py --check
      python examples/cohort_chr20.py --pilot             # 2 samples, all windows
      python examples/cohort_chr20.py --limit 8           # first 8 samples
      python examples/cohort_chr20.py --fleet             # all 2,504 unrelated samples
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import sk8s

# --- pinned inputs (mirrored; provenance in MANIFEST.json beside each) -------------------
BUCKET = "jared-genome-analysis-sk8s"
CHR = "chr20"
CHR_LEN = 64_444_167                         # GRCh38 chr20
REF_FA = f"gs://{BUCKET}/demo-data/cohort-eval/ref/chr20.fa"
REF_EXTS = ("", ".fai", ".amb", ".ann", ".bwt", ".pac", ".sa")   # fa + faidx + bwa index
PANEL_VCF = f"gs://{BUCKET}/demo-data/cohort-eval/published/panel.chr20.vcf.gz"
SAMPLES_TSV = f"gs://{BUCKET}/demo-data/cohort-eval/samples/samples.tsv"

WINDOW = 2_000_000                           # genomic window per task
PAD = 1_000                                  # fetch pad; calling stays inside the window
CACHE = Path("/tmp/chr20-cache")             # shared inside a pod across a chunk


def windows() -> list[tuple[int, int]]:
    return [(s, min(s + WINDOW - 1, CHR_LEN)) for s in range(1, CHR_LEN + 1, WINDOW)]


def _sh(cmd: str) -> str:
    p = subprocess.run(["bash", "-ceo", "pipefail", cmd], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.stdout


def _gs_fetch(uri: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    from google.cloud import storage
    bucket_name, _, key = uri[len("gs://"):].partition("/")
    tmp = dest.with_suffix(dest.suffix + ".part")
    storage.Client().bucket(bucket_name).blob(key).download_to_filename(str(tmp))
    tmp.rename(dest)
    return dest


def _prep_shared() -> dict[str, str]:
    """Reference + bwa index + panel, once per pod (idempotent across a chunk)."""
    CACHE.mkdir(exist_ok=True)
    for ext in REF_EXTS:
        _gs_fetch(REF_FA + ext, CACHE / ("chr20.fa" + ext))
    _gs_fetch(PANEL_VCF, CACHE / "panel.vcf.gz")
    _gs_fetch(PANEL_VCF + ".tbi", CACHE / "panel.vcf.gz.tbi")
    return {"ref": str(CACHE / "chr20.fa"), "panel": str(CACHE / "panel.vcf.gz")}


def _norm_gt(g: str) -> tuple[str, ...]:
    return tuple(sorted(g.replace("|", "/").split("/")))


def _read_sites(vcf_text: str) -> dict[str, tuple[str, ...]]:
    sites: dict[str, tuple[str, ...]] = {}
    for line in vcf_text.splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        gt = f[9].split(":", 1)[0] if len(f) > 9 else "."
        sites[f"{f[0]}:{f[1]}:{f[3]}:{f[4]}"] = _norm_gt(gt)
    return sites


def eval_window(sample: str, cram_url: str, ws: int, we: int, run_id: str) -> dict:
    """One (sample, window) task. Runs in the worker."""
    refs = _prep_shared()
    wd = Path(f"/tmp/{sample}_{ws}")
    wd.mkdir(exist_ok=True)
    t: dict[str, float] = {}
    note = ""
    win = f"{CHR}:{ws}-{we}"
    fetch_region = f"{CHR}:{max(1, ws - PAD)}-{min(CHR_LEN, we + PAD)}"

    t0 = time.time()
    # ranged CRAM fetch; the S3 mirror serves zero-byte .crai for a few samples, so fetch
    # the index ourselves (gzip-validated) with an EBI fallback
    import urllib.request
    crai = wd / "aln.crai"
    ebi_run = cram_url.rstrip("/").rsplit("/", 2)[-2]
    ebi_crai = (f"https://ftp.sra.ebi.ac.uk/vol1/run/{ebi_run[:6]}/{ebi_run}/"
                f"{sample}.final.cram.crai")

    def _crai_ok(p: Path) -> bool:
        return p.exists() and p.stat().st_size > 0 and p.read_bytes()[:2] == b"\x1f\x8b"

    for url in (f"{cram_url}.crai", ebi_crai):
        try:
            urllib.request.urlretrieve(url, crai)
            if _crai_ok(crai):
                if url == ebi_crai:
                    note = "crai: EBI fallback"
                break
        except Exception:
            continue
    if not _crai_ok(crai):
        raise RuntimeError(f"no usable .crai for {sample} (tried S3 + EBI)")

    _sh(f"cd {wd} && samtools view -b -T {refs['ref']} "
        f"-X '{cram_url}' aln.crai {fetch_region} -o region.bam && samtools index region.bam")
    n_reads = int(_sh(f"samtools view -c {wd}/region.bam").strip())
    t["fetch_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    _sh(f"cd {wd} && samtools collate -O region.bam | "
        f"samtools fastq -n -F 0x900 -1 r1.fq -2 r2.fq -s rs.fq - 2>/dev/null")
    if n_reads > 0:
        _sh(f"cd {wd} && bwa mem -t 2 -R '@RG\\tID:{sample}\\tSM:{sample}' {refs['ref']} "
            f"r1.fq r2.fq 2>/dev/null | samtools fixmate -m - - | samtools sort - | "
            f"samtools markdup - aln.bam && samtools index aln.bam")
    t["align_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    if n_reads > 0:
        _sh(f"cd {wd} && bcftools mpileup -r {win} -f {refs['ref']} aln.bam 2>/dev/null | "
            f"bcftools call -mv 2>/dev/null | "
            f"bcftools norm -f {refs['ref']} -m -any -Ov 2>/dev/null > calls.vcf")
    else:
        (wd / "calls.vcf").write_text("")
    t["call_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    # -s and -i must be separate stages (-i evaluates before the -s subset)
    pub = _sh(f"bcftools view -r {win} -s {sample} {refs['panel']} 2>/dev/null | "
              f"bcftools view -i 'GT[0]=\"alt\"' 2>/dev/null | "
              f"bcftools norm -f {refs['ref']} -m -any -Ov 2>/dev/null")
    published = _read_sites(pub)
    ours = _read_sites((wd / "calls.vcf").read_text())
    tp = {k for k in published if k in ours}
    fn = {k for k in published if k not in ours}
    gt_match = sum(1 for k in tp if published[k] == ours[k])
    extras = len([k for k in ours if k not in published])
    t["conc_s"] = round(time.time() - t0, 1)

    n_pub = len(published)
    return {
        "sample": sample, "window": win, "run_id": run_id, "n_reads": n_reads,
        "published_alt_sites": n_pub, "tp": len(tp), "fn": len(fn),
        "recall": round(len(tp) / n_pub, 4) if n_pub else None,
        "gt_match": gt_match,
        "gt_match_rate": round(gt_match / len(tp), 4) if tp else None,
        "extra_calls": extras, "timings": t, "note": note,
    }


def gate(result: dict) -> str | None:
    """Per-task QC. A desert window (assembly gap: no reads AND no panel sites) is valid;
    plenty of panel sites with almost no reads means the fetch broke."""
    if result["n_reads"] < 1000 and result["published_alt_sites"] >= 50:
        return (f"{result['sample']} {result['window']}: only {result['n_reads']} reads "
                f"against {result['published_alt_sites']} published sites")
    return None


def load_samples(limit: int | None) -> list[tuple[str, str]]:
    from google.cloud import storage
    bucket_name, _, key = SAMPLES_TSV[len("gs://"):].partition("/")
    text = storage.Client().bucket(bucket_name).blob(key).download_as_text()
    rows = [r for r in csv.DictReader(io.StringIO(text), delimiter="\t")
            if r["cohort"] == "unrelated2504"]
    rows.sort(key=lambda r: r["sample"])
    if limit:
        rows = rows[:limit]
    # NYGC's index has stray spaces inside a few filenames; the real S3 keys have none
    return [(r["sample"].strip(), r["s3_cram_uri"].replace(" ", "")) for r in rows]


def eval_chunk(chunk: list[tuple[str, str, int, int]], run_id: str) -> list[dict]:
    """Several (sample, window) tasks in one pod; per-task harvesting; workdir cleanup."""
    out: list[dict] = []
    for sample, cram_url, ws, we in chunk:
        try:
            r = eval_window(sample, cram_url, ws, we, run_id)
            bad = gate(r)
            if bad:
                r = {"sample": sample, "window": f"{CHR}:{ws}-{we}",
                     "error": f"ExpectationFailed: {bad}"}
            out.append(r)
        except Exception as e:
            out.append({"sample": sample, "window": f"{CHR}:{ws}-{we}",
                        "error": f"{type(e).__name__}: {e}"[:300]})
        finally:
            subprocess.run(["rm", "-rf", f"/tmp/{sample}_{ws}"], check=False)
    return out


def main(run_id: str, limit: int | None = None, chunk_size: int = 8) -> dict:
    """The detached driver: window-scatter, chunked fan-out, harvest, retry, aggregate."""
    sk8s.configure(volume_wait_seconds=3600)
    samples = load_samples(limit)
    wins = windows()
    tasks = [(s, u, ws, we) for s, u in samples for ws, we in wins]

    results: list[dict] = []
    failures: dict[str, str] = {}

    def run_wave(wave: list[tuple[str, str, int, int]], tag: str) -> list:
        chunks = [wave[i:i + chunk_size] for i in range(0, len(wave), chunk_size)]
        futs = sk8s.submit_starmap(
            eval_chunk, [(c, run_id) for c in chunks],
            batch_size=1, max_concurrent=90,
            requests={"cpu": "2", "memory": "4Gi", "ephemeral-storage": "8Gi"},
            memo=True,
            backoff_limit=2,  # eviction tolerance: explicit opt-in, tasks are idempotent+memoized
        )
        lost: list = []
        for c, fut in zip(chunks, futs):
            try:
                for r in fut.result():
                    if "error" in r:
                        failures[f"{r['sample']}@{r['window']}"] = r["error"]
                    else:
                        results.append(r)
            except Exception as e:
                lost.extend(c)
                for s, _, ws, we in c:
                    failures[f"{s}@{CHR}:{ws}-{we}"] = f"chunk[{tag}]: {type(e).__name__}: {e}"[:300]
        return lost

    lost = run_wave(tasks, "wave1")
    # transient per-task errors are baked into memoized chunk results; retry them in
    # freshly shaped chunks (memo miss). Gate rejections are final.
    task_of = {(t[0], t[2]): t for t in tasks}
    retryable = lost + [task_of[(k.split("@")[0], int(k.split(":")[1].split("-")[0]))]
                        for k, msg in list(failures.items())
                        if not msg.startswith(("ExpectationFailed", "chunk["))]
    if retryable:
        for s, _, ws, we in retryable:
            failures.pop(f"{s}@{CHR}:{ws}-{we}", None)
        run_wave(retryable, "retry")

    # headline metrics aggregated over ALL calls in ALL samples (house convention)
    tp = sum(r["tp"] for r in results)
    fn = sum(r["fn"] for r in results)
    gt = sum(r["gt_match"] for r in results)
    extras = sum(r["extra_calls"] for r in results)

    per_sample: dict[str, dict] = {}
    for r in results:
        a = per_sample.setdefault(r["sample"], {"tp": 0, "fn": 0, "gt": 0, "extras": 0, "windows": 0})
        a["tp"] += r["tp"]; a["fn"] += r["fn"]; a["gt"] += r["gt_match"]
        a["extras"] += r["extra_calls"]; a["windows"] += 1
    sample_recalls = sorted(
        (s, round(a["tp"] / (a["tp"] + a["fn"]), 4) if (a["tp"] + a["fn"]) else None)
        for s, a in per_sample.items())
    rec_vals = [v for _, v in sample_recalls if v is not None]

    summary = {
        "run_id": run_id, "region": f"{CHR}:1-{CHR_LEN}", "window_bp": WINDOW,
        "samples_requested": len(samples), "tasks_requested": len(tasks),
        "tasks_ok": len(results), "tasks_failed": len(failures),
        "samples_complete": sum(1 for a in per_sample.values() if a["windows"] == len(wins)),
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "gt_match": round(gt / tp, 4) if tp else None,
        "tp": tp, "fn": fn, "extra_calls": extras,
        "sample_recall_median": round(statistics.median(rec_vals), 4) if rec_vals else None,
        "sample_recall_min": min(rec_vals) if rec_vals else None,
        "worst_windows": sorted((r for r in results if r["recall"] is not None),
                                key=lambda r: r["recall"])[:10],
        "failures": failures,
        "method": f"window-scattered ({WINDOW}bp) CRAM->FQ->bwa(full {CHR})->bcftools -r window"
                  f"->site+GT concordance vs the 1kG 2020-10 phased panel (PASS, MAC>=2: "
                  f"singletons absent; extra_calls are not false positives).",
    }
    from sk8s import store as _store
    cfg = sk8s.load_config()
    st = _store.get_store(cfg.store, namespace=cfg.namespace, endpoint=cfg.store_endpoint)
    st.put(f"cohort/{run_id}/results.json", json.dumps(results).encode())
    st.put(f"cohort/{run_id}/summary.json", json.dumps(summary, indent=1).encode())
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pilot", action="store_true", help="2 samples, all windows")
    ap.add_argument("--fleet", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--chunk-size", type=int, default=8)
    ap.add_argument("--run-id", default=time.strftime("chr20-%Y%m%d-%H%M%S"))
    ap.add_argument("--check", action="store_true",
                    help="full driver path locally with a stub worker; genuinely no cluster")
    a = ap.parse_args()
    if a.check:
        import unittest.mock as _m
        from types import SimpleNamespace
        stub = lambda s, u, ws, we, r: {
            "sample": s, "window": f"{CHR}:{ws}-{we}", "run_id": r, "n_reads": 9999,
            "published_alt_sites": 100, "tp": 99, "fn": 1, "recall": 0.99, "gt_match": 99,
            "gt_match_rate": 1.0, "extra_calls": 2, "timings": {}, "note": ""}
        fake_store: dict[str, bytes] = {}
        from sk8s import store as _store_mod
        sk8s.configure(cluster=sk8s.LocalCluster())
        with _m.patch(f"{__name__}.eval_window", stub), \
             _m.patch(f"{__name__}.load_samples", lambda limit: [(f"S{i}", "u") for i in range(limit or 2)]), \
             _m.patch.object(sk8s, "configure", lambda *args, **kw: None), \
             _m.patch.object(sk8s, "load_config",
                             lambda: SimpleNamespace(store="check", namespace="check", store_endpoint=None)), \
             _m.patch.object(_store_mod, "get_store",
                             lambda *args, **kw: SimpleNamespace(put=fake_store.__setitem__)):
            out = main("check", limit=2)
        sk8s.configure()
        n_tasks = 2 * len(windows())
        assert out["tasks_ok"] == n_tasks and out["tasks_failed"] == 0, out
        assert out["samples_complete"] == 2, out
        assert "cohort/check/summary.json" in fake_store
        print(f"driver path OK: {n_tasks} window tasks fanned out, harvested, aggregated — all local")
        sys.exit(0)
    if a.pilot:
        a.limit = 2
    if not a.fleet and a.limit is None:
        a.limit = 2
    budget = sk8s.Budget(max_estimated_usd=600, max_concurrent_pods=95, assumed_cpu_when_unset=2)
    if a.dry_run:
        prev = sk8s.submit_workflow(main, a.run_id, limit=a.limit, chunk_size=a.chunk_size,
                                    dry_run=True, budget=budget)
        print(json.dumps(prev.to_dict(), indent=1))
        sys.exit(0)
    wf = sk8s.submit_workflow(main, a.run_id, limit=a.limit, chunk_size=a.chunk_size, budget=budget)
    n = a.limit if a.limit else 2504
    print(f"workflow {wf.id}  run_id {a.run_id}  {n} samples x {len(windows())} windows "
          f"= {n * len(windows())} tasks, chunk_size {a.chunk_size}")
    print(f"watch:   sk8s explain {wf.id}")
