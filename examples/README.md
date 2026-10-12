# sk8s examples

## NA12878 / BRCA2: reads to VCF (`na12878_brca2.py`)

The request: *fetch NA12878 reads from a small region (BRCA2), align them, call SNVs and indels,
make a VCF.* One script does it as a detached sk8s workflow on a GKE cluster, with QC gates, and is
re-runnable verbatim.

**Data (pinned, public provenance).** `gs://jared-genome-analysis-sk8s/demo-data/na12878-brca2/`
holds 26,841 reads of 1000 Genomes NYGC 30x NA12878 (GRCh38) over BRCA2 +/-10 kb
(`chr13:32,305,086-32,410,268`) and the matching 105,183 bp reference slice (contig
`chr13_32305086_32410268`). `MANIFEST.json` there records exactly how it was cut from the public
CRAM and reference, with md5s; the first pipeline stage re-verifies them. The bucket is private, so
workers read it through Workload Identity.

**Run it.**

```bash
export KUBECONFIG=/path/to/kubeconfig-gke                  # 1. cluster
export SK8S_CONFIG=/path/to/.sk8s-demo.toml                # 2. namespace + gs:// store (setup below)
export SK8S_IMAGE=us-central1-docker.pkg.dev/jared-genome-analysis/sk8s/sk8s-demo-bio:py3.14-5fd7361   # 3. image
sk8s doctor                                                # all PASS first
python examples/na12878_brca2.py --repeat 2                # two fresh runs; asserts identical counts
```

`--preview-only` stops after the `describe` + `dry_run` plan. `--prove-gate gs://.../results/<run-id>/work/`
shows an `expect=` gate rejecting a bad result.

**Cold setup** (one-time, cluster admin; already standing in this project as namespace `sk8s-demo`):

```bash
sk8s config --provider gke --project jared-genome-analysis --region us-central1 \
  --namespace sk8s-demo --gsa sk8s-worker \
  --store gs://jared-genome-analysis-sk8s/sk8s-demo/ --apply-cloud   # incl. the per-namespace WI member
echo 'image = "<the sk8s-demo-bio tag you pushed>"' >> "$SK8S_CONFIG" # sk8s config does not write image=
sk8s doctor                                                           # red right after apply = IAM lag; retry ~60s
```

Notes: results land under `gs://jared-genome-analysis-sk8s/sk8s-demo/results/` — the prefix is
hardcoded in the script, so a different store means editing `RESULTS` there. The image tag in step 3
must be one you actually pushed (the standing one is `py3.14-5fd7361`; a rebuild tags with your
current HEAD — keep them consistent). The 187 variants both runs produce are raw, unfiltered
`bcftools call -mv` output, deterministic on this input — not filtered calls and not compared to
GIAB truth; the lifted VCF's `##reference=` header line still names the slice FASTA.

**Image.** The worker's Python minor must equal the client's (3.14 for this venv). Build a base from
your checkout, then add the tools with `examples/Dockerfile.na12878` (Debian 13: bwa 0.7.18,
samtools 1.21, bcftools 1.21):

```bash
REG=us-central1-docker.pkg.dev/jared-genome-analysis/sk8s; T=py3.14-$(git rev-parse --short HEAD)
docker build -f docker/Dockerfile.worker --build-arg PYTHON_VERSION=3.14 -t $REG/sk8s-demo-base:$T . && docker push $REG/sk8s-demo-base:$T
docker build -f examples/Dockerfile.na12878 --build-arg BASE=$REG/sk8s-demo-base:$T -t $REG/sk8s-demo-bio:$T . && docker push $REG/sk8s-demo-bio:$T
```

**Shape.** The client calls `sk8s.describe` on the input, `dry_run`s the whole plan, then submits a
detached driver that chains four stages, each an sk8s job with an `expect=` gate:

| stage | what | gate |
|---|---|---|
| `fetch_fastq` | stage BAM + reference from the mirror, md5-verify, mate-aware `samtools collate` then `fastq` | >= 10,000 complete pairs |
| `align` | `bwa index` + `mem` (read group, `-K` fixed), `fixmate`, `sort`, `markdup`, index | >= 20,000 reads aligned and > 90% mapped |
| `call_window` x4 | `bcftools mpileup` then `call -mv` over four disjoint windows via `sk8s.starmap(pilot=1)` | >= 10 calls per window |
| `merge_publish` | `concat`, `bcftools norm -m -any`, lift to chr13, bgzip + tabix, `stats.json` | >= 50 records, non-empty, ts/tv in [1.0, 4.0] |

Output: `gs://.../sk8s-demo/results/<run-id>/NA12878.BRCA2.vcf.gz` (+ `.tbi`, `stats.json`;
intermediates under `work/`). Watch a run with `sk8s explain <wf-id>` (add `--json`).

**Choices to know about.**

- *Coordinates.* Alignment and calling run against the slice, so VCF positions are slice-local.
  `bcftools annotate` can rename contigs but not shift `POS`, so the merge stage does a few lines of
  text processing: `CHROM=chr13`, `POS += 32,305,085`, the `##contig` header rewritten, and a
  `##liftover` header line added. `norm` runs before the lift because it needs the slice reference.
  Header `##bcftoolsCommand` lines keep slice-local `-r` coordinates.
- *Windows.* The fan-out is real but modest: calling is per-position over the one full BAM, so four
  windows give the same sites as one whole-region call (checked on this data: 187 records both ways).
  Indels starting in a window belong to it; none are called twice.
- *Pairs only.* 280 orphan reads (mate outside the region) are not realigned. Duplicates are marked
  (not removed), and `mpileup` skips them by default.
- *Intermediates go through GCS*, not a volume: the data is small, and it keeps the example free of
  PVC provisioning.
- Variant count is deterministic run to run (same tools, same inputs, `bwa -K`); `--repeat 2`
  asserts it and also compares an md5 of the record bodies (headers carry dates).
