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
export SK8S_CONFIG=/path/to/.sk8s-demo.toml                # 2. namespace + gs:// store (sk8s config ...)
export SK8S_IMAGE=us-central1-docker.pkg.dev/jared-genome-analysis/sk8s/sk8s-demo-bio:py3.14-2287959   # 3. image
sk8s doctor                                                # all PASS first
python examples/na12878_brca2.py --repeat 2                # run 1 computes, run 2 is all memo hits
```

- `--repeat 2` (or just running it again tomorrow): every stage is `memo=True`, so the second run
  is a memo-hit cascade. The detached driver pod is the only job it creates (about 20 s wall, ~$0.0001);
  `--tethered` drives the pipeline from your process instead and creates **no** job at all.
- `--memo-salt v2` changes every memo key: everything recomputes (proof the cache is not stuck).
- `--preview-only` stops after the `describe` + `dry_run` plan. `--prove-gate` shows an `expect=` gate
  rejecting a bad result (stages 1-2 are memo hits, so it costs one tiny job).

**Image.** The worker must be built from a checkout that has the artifact layer (the pods stage
inputs and upload outputs), and its Python minor must equal the client's (3.14 for this venv). Memo
keys include the image string, so a new tag is a deliberate cache miss. Build a base from
your checkout, then add the tools with `examples/Dockerfile.na12878` (Debian 13: bwa 0.7.18,
samtools 1.21, bcftools 1.21):

```bash
REG=us-central1-docker.pkg.dev/jared-genome-analysis/sk8s; T=py3.14-$(git rev-parse --short HEAD)
docker build -f docker/Dockerfile.worker --build-arg PYTHON_VERSION=3.14 -t $REG/sk8s-demo-base:$T . && docker push $REG/sk8s-demo-base:$T
docker build -f examples/Dockerfile.na12878 --build-arg BASE=$REG/sk8s-demo-base:$T -t $REG/sk8s-demo-bio:$T . && docker push $REG/sk8s-demo-bio:$T
```

**Shape.** The client calls `sk8s.describe` on the input, `dry_run`s the whole plan, then submits a
detached driver that chains the stages below. Every stage is an sk8s job with `memo=True` and an
`expect=` gate; files move between stages as `sk8s.Artifact`s (inputs arrive as real local `Path`s, whatever
the function writes under its `OutDir` and returns becomes an artifact):

| stage | what | gate |
|---|---|---|
| `fetch_fastq` | BAM + reference are `sk8s.artifact()`s pinned by generation and sha256; manifest md5 re-checked; mate-aware `samtools collate` then `fastq` | >= 10,000 complete pairs |
| `align` | `bwa index` + `mem` (read group, `-K` fixed), `fixmate`, `sort`, `markdup`; the BAM is an output artifact | >= 20,000 reads aligned and > 90% mapped |
| `call_window` x4 | `bcftools mpileup` then `call -mv` over four disjoint windows via `submit_starmap(pilot=1)`; takes the BAM artifact by hash | >= 10 calls per window |
| `merge` | `concat`, `bcftools norm -m -any`, lift to chr13, bgzip + tabix, `stats.json` | >= 50 records, non-empty, ts/tv in [1.0, 4.0] |
| publish (driver) | `Artifact.publish(copy=True)` of VCF, index and stats to `results/<run-id>/` | n/a |

Output: `gs://.../sk8s-demo/results/<run-id>/NA12878.BRCA2.vcf.gz` (+ `.tbi`, `stats.json`). Each published
object carries `sk8s-artifact-sha256` and `sk8s-produced-by` (the workflow/job that actually computed it,
which on a memo hit is the original run). Watch a run with `sk8s explain <wf-id>` (add `--json`); the
driver prints one line per stage saying `computed` or `HIT`, visible in `sk8s logs`.

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
- *What is still a plain `gs://` path:* only the pinned input mirror (`DATA`, read via `sk8s.artifact`) and
  the `results/<run-id>/` publish destination. Everything in between (FASTQ, BAM, window VCFs) lives in
  the CAS under the store and is addressed by hash; there is no `work/` prefix any more. The reference
  FASTA is passed to every stage as an artifact; its `.fai` is rebuilt in the pod (cheaper than another
  artifact), and `call_window` re-runs `samtools index` on the BAM for the same reason.
- *Memo hygiene.* Stage functions take no run-id and write no timings, so their keys depend only on code
  and inputs; the run-id lives only in the publish path. The key does NOT see the bio tools' versions,
  so the image string is part of it: bumping the tag (or `--memo-salt`) is how you invalidate. CAS
  objects are collected after the workflow goes unless a memo entry (< 7 days) or a published copy keeps
  them; the final outputs are published with `copy=True` for that reason.
- *Fallback: the pre-artifact form.* The earlier version of this example (plain `gs://` paths and
  in-function `google.cloud.storage` calls, no memo) is `git show 2287959:examples/na12878_brca2.py`.
  It needs only a worker built from 5fd7361 or later, no artifact layer, and recomputes every time.
- Variant count is deterministic run to run (same tools, same inputs, `bwa -K`); `--repeat 2`
  asserts it and also compares an md5 of the record bodies (headers carry dates, so a recomputed VCF
  has different bytes but identical records).
