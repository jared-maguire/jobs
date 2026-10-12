"""sk8s.describe: format sniffing, cohort summaries, remote range-reads (fake SDK clients)."""
import gzip
import json
import struct
import sys
import types
import zlib
from pathlib import Path
from typing import Any

import pytest

import sk8s
from sk8s import datadescribe
from sk8s.errors import BlobNotFound, Sk8sError, StoreError

# --- fixture builders (everything is synthesized in-test) ---------------------------------------


def bgzf_block(data: bytes) -> bytes:
    """One valid BGZF block (gzip member with the BC extra subfield), fixed MTIME."""
    comp = zlib.compressobj(6, zlib.DEFLATED, -15)
    body = comp.compress(data) + comp.flush()
    bsize = 18 + len(body) + 8 - 1
    header = b"\x1f\x8b\x08\x04" + struct.pack("<IBB", 0, 0, 0xFF) + struct.pack("<H", 6)
    header += b"BC" + struct.pack("<HH", 2, bsize)
    return header + body + struct.pack("<II", zlib.crc32(data), len(data))


BGZF_EOF = bgzf_block(b"")


def bam_header_bytes(text: str, refs: list[tuple[str, int]]) -> bytes:
    raw = text.encode()
    out = b"BAM\x01" + struct.pack("<i", len(raw)) + raw + struct.pack("<i", len(refs))
    for name, length in refs:
        nb = name.encode() + b"\x00"
        out += struct.pack("<i", len(nb)) + nb + struct.pack("<i", length)
    return out


BAM_TEXT = (
    "@HD\tVN:1.6\tSO:coordinate\n"
    "@SQ\tSN:chr1\tLN:248956422\n"
    "@SQ\tSN:chr2\tLN:242193529\n"
    "@RG\tID:rg1\tSM:S1\tPL:ILLUMINA\tLB:lib1\n"
    "@PG\tID:bwa\tPN:bwa\tVN:0.7.17-r1188\tCL:bwa mem -t 8 ref.fa r1.fq r2.fq\n"
    "@PG\tID:samtools\tPN:samtools\tVN:1.17\tPP:bwa\n"
)
BAM_REFS = [("chr1", 248956422), ("chr2", 242193529)]


def make_bam(text: str = BAM_TEXT, refs: list[tuple[str, int]] = BAM_REFS) -> bytes:
    return bgzf_block(bam_header_bytes(text, refs)) + BGZF_EOF


def fastq_text(n: int, length: int = 8, qual: str = "IIIIIIII") -> str:
    qual = (qual * length)[:length]
    return "".join(f"@read{i} 1:N:0:ACGT\n{'ACGTACGTAC'[:length]}\n+\n{qual}\n" for i in range(n))


VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##source=fakecaller\n"
    "##reference=file:///refs/GRCh38.fa\n"
    "##contig=<ID=chr1,length=248956422>\n"
    "##contig=<ID=chr2,length=242193529>\n"
    "##contig=<ID=chrM,length=16569>\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\tS3\n"
    "chr1\t100\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\t1/1\t0/0\n"
)


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def fasta_text(n_contigs: int = 7, per_contig: int = 130, width: int = 60) -> str:
    out = []
    for i in range(n_contigs):
        seq = ("ACGTTGCA" * (per_contig // 8 + 1))[:per_contig]
        out.append(f">chr{i + 1} desc of contig {i + 1}\n")
        out.extend(seq[j : j + width] + "\n" for j in range(0, len(seq), width))
    return "".join(out)


def fai_text(rows: list[tuple[str, int]], cols: int = 5) -> str:
    return "".join(f"{n}\t{ln}\t{10 + i * 1000}\t60\t61" + ("\t0" if cols == 6 else "") + "\n" for i, (n, ln) in enumerate(rows))


def bai_bytes(n_ref: int = 3) -> bytes:
    return b"BAI\x01" + struct.pack("<i", n_ref) + b"\x00" * 24


# --- single files: formats ----------------------------------------------------------------------


def test_exported_at_top_level() -> None:
    assert sk8s.describe is datadescribe.describe
    assert "describe" in sk8s.__all__


def test_fastq_plain(tmp_path: Path) -> None:
    p = write(tmp_path / "s1.fastq", fastq_text(10, length=8))
    d = sk8s.describe(str(p))
    assert d["kind"] == "file"
    assert d["format"] == "fastq"
    assert d["confidence"] == "content"
    assert d["compression"] is None
    assert d["size_bytes"] == p.stat().st_size
    f = d["facts"]
    assert f["read_length"] == 8
    assert f["phred_offset_guess"] == 33
    assert f["records"] == 10  # small file: counted
    assert f["first_read_id"] == "read0"


def test_fastq_gz_small_is_counted(tmp_path: Path) -> None:
    p = write(tmp_path / "s1_R1.fastq.gz", gzip.compress(fastq_text(25).encode(), mtime=0))
    d = sk8s.describe(str(p))
    assert d["format"] == "fastq"
    assert d["compression"] == "gzip"
    assert d["facts"]["records"] == 25


def test_fastq_large_not_counted(tmp_path: Path) -> None:
    p = write(tmp_path / "big.fastq", fastq_text(40))
    d = datadescribe.describe(str(p), small_file_bytes=100)  # pretend 'small' is tiny
    assert d["format"] == "fastq"
    assert d["facts"]["records"] == "not counted"
    assert d["facts"]["read_length"] == 8  # still sniffed from the head


def test_fastq_phred64_guess(tmp_path: Path) -> None:
    p = write(tmp_path / "old.fq", fastq_text(5, qual="hhhhhhgh"))  # 'h' = 104, min 'g' = 103
    assert sk8s.describe(str(p))["facts"]["phred_offset_guess"] == 64


def test_fastq_phred33_when_low_chars(tmp_path: Path) -> None:
    p = write(tmp_path / "x.fq", fastq_text(5, qual="#IIIIIII"))
    assert sk8s.describe(str(p))["facts"]["phred_offset_guess"] == 33


def test_fastq_variable_read_lengths(tmp_path: Path) -> None:
    text = "@a\nACGT\n+\nIIII\n@b\nACGTAC\n+\nIIIIII\n"
    f = sk8s.describe(str(write(tmp_path / "v.fastq", text)))["facts"]
    assert f["read_length"] == 4  # the first read
    assert (f["read_length_min"], f["read_length_max"]) == (4, 6)


def test_fastq_by_content_when_extension_lies(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "mystery.dat", fastq_text(3))))
    assert d["format"] == "fastq"
    assert d["confidence"] == "content"


def test_fastq_truncated_record_count_warns(tmp_path: Path) -> None:
    p = write(tmp_path / "t.fastq", fastq_text(3) + "@partial\nACGT\n")
    d = sk8s.describe(str(p))
    assert d["facts"]["records"] == 3
    assert any("not a multiple of 4" in n for n in d["notes"])


def test_bam_header_facts(tmp_path: Path) -> None:
    p = write(tmp_path / "s1.bam", make_bam())
    d = sk8s.describe(str(p))
    assert d["format"] == "bam"
    assert d["confidence"] == "magic"
    assert d["compression"] == "bgzf"
    f = d["facts"]
    assert f["sort_order"] == "coordinate"
    assert f["sam_version"] == "1.6"
    assert f["reference_count"] == 2
    assert f["references"] == [
        {"name": "chr1", "length": 248956422},
        {"name": "chr2", "length": 242193529},
    ]
    assert f["read_group_count"] == 1
    assert f["read_groups"] == [{"id": "rg1", "sample": "S1", "platform": "ILLUMINA", "library": "lib1"}]
    assert f["aligner"]["name"] == "bwa"
    assert f["aligner"]["version"] == "0.7.17-r1188"
    assert f["aligner"]["command_line"].startswith("bwa mem")
    assert f["programs"] == ["bwa", "samtools"]
    assert f["header_truncated"] is False


def test_bam_references_capped_at_five(tmp_path: Path) -> None:
    refs = [(f"c{i}", 1000 + i) for i in range(12)]
    text = "@HD\tVN:1.6\tSO:unsorted\n" + "".join(f"@SQ\tSN:{n}\tLN:{ln}\n" for n, ln in refs)
    d = sk8s.describe(str(write(tmp_path / "x.bam", make_bam(text, refs))))
    assert d["facts"]["reference_count"] == 12
    assert [r["name"] for r in d["facts"]["references"]] == ["c0", "c1", "c2", "c3", "c4"]
    assert d["facts"]["aligner"] is None
    assert d["facts"]["sort_order"] == "unsorted"


def test_bam_header_spanning_two_bgzf_blocks(tmp_path: Path) -> None:
    refs = [(f"contig{i}", 5000 + i) for i in range(200)]
    text = "@HD\tVN:1.6\tSO:coordinate\n" + "".join(f"@SQ\tSN:{n}\tLN:{ln}\n" for n, ln in refs)
    raw = bam_header_bytes(text, refs)
    half = len(raw) // 2
    data = bgzf_block(raw[:half]) + bgzf_block(raw[half:]) + BGZF_EOF
    d = sk8s.describe(str(write(tmp_path / "split.bam", data)))
    assert d["facts"]["reference_count"] == 200


def test_bam_with_truncated_head_still_reports(tmp_path: Path) -> None:
    refs = [(f"contig{i}", 5000 + i) for i in range(3000)]
    text = "@HD\tVN:1.6\tSO:coordinate\n" + "".join(f"@SQ\tSN:{n}\tLN:{ln}\n" for n, ln in refs)
    raw = bam_header_bytes(text, refs)
    # Many small blocks, then cut the file mid-way: the head ends inside the header.
    blocks = b"".join(bgzf_block(raw[i : i + 20000]) for i in range(0, len(raw), 20000))
    p = write(tmp_path / "big.bam", blocks + BGZF_EOF)
    d = datadescribe.describe(str(p), head_bytes=len(blocks) // 3, small_file_bytes=1)
    assert d["format"] == "bam"
    assert d["facts"]["header_truncated"] is True
    assert d["facts"]["sort_order"] == "coordinate"
    assert d["facts"]["references"][0] == {"name": "contig0", "length": 5000}


def test_cram_magic_and_version(tmp_path: Path) -> None:
    data = b"CRAM" + bytes([3, 1]) + b"file-id-0123456789ab".ljust(20, b"\x00") + b"\x00" * 50
    d = sk8s.describe(str(write(tmp_path / "s1.cram", data)))
    assert d["format"] == "cram"
    assert d["confidence"] == "magic"
    assert d["facts"]["version"] == "3.1"
    assert d["facts"]["file_id"] == "file-id-0123456789ab"


def test_vcf_plain(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "calls.vcf", VCF_TEXT)))
    assert d["format"] == "vcf"
    assert d["confidence"] == "magic"
    f = d["facts"]
    assert f["fileformat"] == "VCFv4.2"
    assert f["reference"] == "file:///refs/GRCh38.fa"
    assert f["contig_count"] == 3
    assert f["sample_count"] == 3
    assert f["samples"] == ["S1", "S2", "S3"]
    assert f["source"] == "fakecaller"


def test_vcf_gz_bgzf_many_samples(tmp_path: Path) -> None:
    names = [f"S{i}" for i in range(9)]
    text = VCF_TEXT.replace("S1\tS2\tS3", "\t".join(names))
    d = sk8s.describe(str(write(tmp_path / "c.vcf.gz", bgzf_block(text.encode()) + BGZF_EOF)))
    assert d["format"] == "vcf"
    assert d["compression"] == "bgzf"
    assert d["facts"]["sample_count"] == 9
    assert d["facts"]["samples"] == names[:5]


def test_csv_sniff(tmp_path: Path) -> None:
    text = "sample,count,ratio,label\nS1,10,0.5,a\nS2,20,0.25,b\nS3,30,1.5,c\n"
    d = sk8s.describe(str(write(tmp_path / "t.csv", text)))
    assert d["format"] == "csv"
    assert d["confidence"] == "content"
    f = d["facts"]
    assert f["delimiter"] == ","
    assert f["has_header"] is True
    assert f["column_count"] == 4
    assert [(c["name"], c["type"]) for c in f["columns"]] == [
        ("sample", "string"), ("count", "int"), ("ratio", "float"), ("label", "string"),
    ]


def test_tsv_sniff_headerless(tmp_path: Path) -> None:
    text = "1\t2.5\n3\t4.5\n5\t6.5\n"
    d = sk8s.describe(str(write(tmp_path / "t.tsv", text)))
    assert d["format"] == "tsv"
    f = d["facts"]
    assert f["delimiter"] == "\t"
    assert f["has_header"] is False
    assert [c["type"] for c in f["columns"]] == ["int", "float"]
    assert f["columns"][0]["name"] == "col1"


def test_tsv_gz(tmp_path: Path) -> None:
    text = "gene\tcount\nA\t1\nB\t2\n"
    d = sk8s.describe(str(write(tmp_path / "counts.tsv.gz", gzip.compress(text.encode(), mtime=0))))
    assert d["format"] == "tsv"
    assert d["compression"] == "gzip"
    assert d["facts"]["column_count"] == 2


def test_csv_quoted_commas_do_not_split(tmp_path: Path) -> None:
    text = 'id,note\n1,"a, b"\n2,"c, d"\n3,"e, f"\n'
    f = sk8s.describe(str(write(tmp_path / "q.csv", text)))["facts"]
    assert f["column_count"] == 2


def test_gzip_of_unknown_content(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "blob.bin.gz", gzip.compress(b"\x00\x01\x02" * 50, mtime=0))))
    assert d["format"] == "gzip"
    assert d["compression"] == "gzip"
    assert d["confidence"] == "magic"
    assert any("compressed" in n for n in d["notes"])


def test_unknown_binary_reports_magic_and_size(tmp_path: Path) -> None:
    p = write(tmp_path / "mystery.xyz", b"\xde\xad\xbe\xef" + b"\x00" * 60)
    d = sk8s.describe(str(p))
    assert d["format"] == "unknown"
    assert d["facts"]["magic_hex"] == "deadbeef00000000"
    assert d["size_bytes"] == 64


def test_plain_text_is_text(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "notes.txt", "just some words\nand more words\n")))
    assert d["format"] == "text"


def test_empty_file_with_fastq_extension_is_extension_confidence(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "empty.fastq", b"")))
    assert d["format"] == "fastq"
    assert d["confidence"] == "extension"
    assert d["size_bytes"] == 0


def test_bam_extension_with_wrong_content_is_not_trusted(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "fake.bam", b"not a bam at all")))
    assert d["format"] != "bam"
    assert any(".bam" in n for n in d["notes"])


def test_missing_path_is_clean_error(tmp_path: Path) -> None:
    with pytest.raises(Sk8sError, match="no such"):
        sk8s.describe(str(tmp_path / "nope.bam"))


def test_unsupported_scheme(tmp_path: Path) -> None:
    with pytest.raises(Sk8sError, match="http"):
        sk8s.describe("http://example.com/x.bam")


def test_everything_is_json_serializable(tmp_path: Path) -> None:
    write(tmp_path / "a.bam", make_bam())
    write(tmp_path / "b.vcf", VCF_TEXT)
    write(tmp_path / "c.fastq", fastq_text(3))
    write(tmp_path / "d.csv", "a,b\n1,2\n3,4\n")
    write(tmp_path / "e.cram", b"CRAM\x03\x00" + b"\x00" * 40)
    for d in (sk8s.describe(str(tmp_path)), sk8s.describe(str(tmp_path / "a.bam"))):
        assert json.loads(json.dumps(d)) == d


# --- cohorts (local directory) ------------------------------------------------------------------


def _fastq_cohort(root: Path, samples: int = 6) -> None:
    for i in range(1, samples + 1):
        for r in (1, 2):
            write(root / f"S{i}_S{i}_L001_R{r}_001.fastq.gz", gzip.compress(fastq_text(4).encode(), mtime=0))


def test_cohort_summary(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=6)
    write(tmp_path / "manifest.csv", "sample,path\nS1,x\nS2,y\n")
    d = sk8s.describe(str(tmp_path))
    assert d["kind"] == "cohort"
    assert d["file_count"] == 13
    assert d["listing_capped"] is False
    assert d["formats"]["fastq"]["count"] == 12
    assert d["formats"]["csv"]["count"] == 1
    assert d["formats"]["fastq"]["confidence"] == "extension"
    assert d["total_bytes"] == sum(p.stat().st_size for p in tmp_path.iterdir())
    s = d["size_stats"]
    assert s["min"] <= s["median"] <= s["max"]
    assert s["total"] == d["total_bytes"]


def test_cohort_samples_are_first_n_in_deterministic_order(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=6)
    d = sk8s.describe(str(tmp_path))
    names = [s["name"] for s in d["samples"]]
    assert len(names) == 5
    assert names == sorted(names)
    assert names[0] == "S1_S1_L001_R1_001.fastq.gz"
    assert all(s["format"] == "fastq" and s["facts"]["read_length"] == 8 for s in d["samples"])
    again = sk8s.describe(str(tmp_path))
    assert [s["path"] for s in again["samples"]] == [s["path"] for s in d["samples"]]


def test_cohort_sample_count_param(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=6)
    assert len(datadescribe.describe(str(tmp_path), samples=2)["samples"]) == 2


def test_cohort_paired_end_detection(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=6)
    write(tmp_path / "orphan_R1_001.fastq.gz", gzip.compress(fastq_text(1).encode(), mtime=0))
    fq = sk8s.describe(str(tmp_path))["patterns"]["fastq"]
    assert fq["files"] == 13
    assert fq["paired_end_pairs"] == 6
    assert fq["unpaired_files"] == 1
    assert fq["layout"] == "mixed"
    assert fq["samples_estimate"] == 7


def test_cohort_paired_end_layout_pure(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=3)
    fq = sk8s.describe(str(tmp_path))["patterns"]["fastq"]
    assert fq["layout"] == "paired-end"
    assert fq["samples_estimate"] == 3


@pytest.mark.parametrize(
    "r1,r2",
    [("a_1.fastq.gz", "a_2.fastq.gz"), ("a.R1.fq.gz", "a.R2.fq.gz"), ("a_R1.fastq", "a_R2.fastq")],
)
def test_pair_naming_conventions(tmp_path: Path, r1: str, r2: str) -> None:
    write(tmp_path / r1, fastq_text(1))
    write(tmp_path / r2, fastq_text(1))
    assert sk8s.describe(str(tmp_path))["patterns"]["fastq"]["paired_end_pairs"] == 1


def test_lanes_are_separate_pairs_one_sample(tmp_path: Path) -> None:
    for lane in ("L001", "L002"):
        for r in (1, 2):
            write(tmp_path / f"X_S1_{lane}_R{r}_001.fastq.gz", fastq_text(1))
    fq = sk8s.describe(str(tmp_path))["patterns"]["fastq"]
    assert fq["paired_end_pairs"] == 2
    assert fq["samples_estimate"] == 1


def test_single_end_layout(tmp_path: Path) -> None:
    for i in range(3):
        write(tmp_path / f"sample{i}.fastq.gz", fastq_text(1))
    fq = sk8s.describe(str(tmp_path))["patterns"]["fastq"]
    assert fq["layout"] == "single-end"
    assert fq["samples_estimate"] == 3


def test_bam_index_pairing(tmp_path: Path) -> None:
    write(tmp_path / "a.bam", make_bam())
    write(tmp_path / "a.bam.bai", b"BAI\x01" + b"\x00" * 8)
    write(tmp_path / "b.bam", make_bam())
    d = sk8s.describe(str(tmp_path))
    assert d["patterns"]["bam"] == {"files": 2, "with_index": 1}
    assert d["formats"]["index"]["count"] == 1


def test_cohort_recurses_and_names_are_relative(tmp_path: Path) -> None:
    write(tmp_path / "run1" / "a.fastq", fastq_text(1))
    write(tmp_path / "run2" / "b.fastq", fastq_text(1))
    d = sk8s.describe(str(tmp_path))
    assert d["file_count"] == 2
    assert [s["name"] for s in d["samples"]] == ["a.fastq", "b.fastq"]
    assert d["samples"][0]["relative_path"] == "run1/a.fastq"


def test_cohort_listing_cap_is_reported(tmp_path: Path) -> None:
    for i in range(12):
        write(tmp_path / f"f{i:02d}.txt", "hello world\n")
    d = datadescribe.describe(str(tmp_path), max_objects=10)
    assert d["listing_capped"] is True
    assert d["listing_cap"] == 10
    assert d["file_count"] == 10
    assert any("capped" in n and "10" in n for n in d["notes"])


def test_cohort_headline_matches_the_napkin(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=4)
    d = sk8s.describe(str(tmp_path))
    assert d["headline"].startswith("Found 4 samples (FASTQ, paired-end, 8 files, ")


def test_headline_prefers_data_over_sidecars(tmp_path: Path) -> None:
    write(tmp_path / "a.bam", make_bam())
    write(tmp_path / "a.bam.bai", b"BAI\x01")
    write(tmp_path / "b.bam.bai", b"BAI\x01")
    write(tmp_path / "ref.fa", ">chr1\nACGT\n")
    write(tmp_path / "notes.json", "{}")
    d = sk8s.describe(str(tmp_path))
    assert d["headline"].startswith("Found 1 BAM files (")
    assert d["formats"]["fasta"]["count"] == 1


def test_empty_directory(tmp_path: Path) -> None:
    d = sk8s.describe(str(tmp_path))
    assert d["file_count"] == 0
    assert d["samples"] == []
    assert d["size_stats"] is None


def test_unreadable_sample_is_reported_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "a.fastq", fastq_text(1))
    write(tmp_path / "b.fastq", fastq_text(1))
    real = datadescribe._read_local_head

    def flaky(path: Path, *a: Any, **k: Any) -> Any:
        if path.name == "a.fastq":
            raise PermissionError("denied")
        return real(path, *a, **k)

    monkeypatch.setattr(datadescribe, "_read_local_head", flaky)
    d = sk8s.describe(str(tmp_path))
    assert "denied" in d["samples"][0]["error"]
    assert d["samples"][1]["format"] == "fastq"


def test_sample_format_mismatch_listed(tmp_path: Path) -> None:
    write(tmp_path / "liar.bam", b"this is plain text, not a BAM\n")
    d = sk8s.describe(str(tmp_path))
    assert d["sample_format_mismatches"] == [
        {"relative_path": "liar.bam", "by_extension": "bam", "by_content": "text"}
    ]


def test_fasta_named_txt_gets_the_mismatch_note_and_cohort_entry(tmp_path: Path) -> None:
    fa = b">chr_test\nACGTACGTACGT\nACGT\n"
    write(tmp_path / "ref.txt", fa)
    single = sk8s.describe(str(tmp_path / "ref.txt"))
    assert single["format"] == "fasta"
    assert any("looks like" in n or ".txt" in n for n in single["notes"])
    cohort = sk8s.describe(str(tmp_path))
    assert cohort["sample_format_mismatches"] == [
        {"relative_path": "ref.txt", "by_extension": "text", "by_content": "fasta"}
    ]


def test_human_rendering_mentions_key_facts(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=2)
    out = datadescribe.format_description(sk8s.describe(str(tmp_path)))
    assert "Found 2 samples" in out
    assert "fastq" in out and "read length 8" in out
    single = datadescribe.format_description(sk8s.describe(str(write(tmp_path / "x.bam", make_bam()))))
    assert "bam" in single and "coordinate" in single and "chr1" in single


# --- remote (gs:// and s3://) through fake SDK clients --------------------------------------------


class _RangeLog:
    """What the fake object stores were asked to read: (key, start, end_inclusive)."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, int, int]] = []
        self.served = 0

    def record(self, key: str, start: int, end: int, served: int) -> None:
        self.reads.append((key, start, end))
        self.served += served


class RangeBlob:
    def __init__(self, client: "RangeGCS", bucket: str, name: str) -> None:
        self.client, self.bucket, self.name = client, bucket, name
        self.size = len(client.objects.get((bucket, name), b""))

    def download_as_bytes(self, *, start: int | None = None, end: int | None = None, **_: Any) -> bytes:
        data = self.client.objects[(self.bucket, self.name)]
        if start is None and end is None:
            self.client.log.record(self.name, 0, len(data) - 1, len(data))
            return data
        assert start is not None and end is not None
        out = data[start : end + 1]  # GCS: end is inclusive
        self.client.log.record(self.name, start, end, len(out))
        return out


class RangeBucket:
    def __init__(self, client: "RangeGCS", name: str) -> None:
        self.client, self.name = client, name

    def blob(self, name: str) -> RangeBlob:
        return RangeBlob(self.client, self.name, name)


class RangeGCS:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects
        self.log = _RangeLog()
        self.list_kwargs: list[dict[str, Any]] = []

    def bucket(self, name: str) -> RangeBucket:
        return RangeBucket(self, name)

    def list_blobs(self, bucket: str, *, prefix: str | None = None, max_results: int | None = None, **_: Any) -> Any:
        self.list_kwargs.append({"bucket": bucket, "prefix": prefix, "max_results": max_results})
        keys = sorted(k for (b, k) in self.objects if b == bucket and k.startswith(prefix or ""))
        if max_results is not None:
            keys = keys[:max_results]
        return iter([RangeBlob(self, bucket, k) for k in keys])


class RangeS3:
    def __init__(self, objects: dict[tuple[str, str], bytes], page_size: int = 1000) -> None:
        self.objects = objects
        self.page_size = page_size
        self.log = _RangeLog()
        self.list_calls = 0

    def list_objects_v2(
        self, *, Bucket: str, Prefix: str = "", MaxKeys: int = 1000, ContinuationToken: str | None = None, **_: Any
    ) -> dict[str, Any]:
        self.list_calls += 1
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix))
        if ContinuationToken:
            keys = [k for k in keys if k > ContinuationToken]
        n = min(MaxKeys, self.page_size)
        page = keys[:n]
        resp: dict[str, Any] = {
            "Contents": [{"Key": k, "Size": len(self.objects[(Bucket, k)])} for k in page],
            "KeyCount": len(page),
            "IsTruncated": len(keys) > n,
        }
        if len(keys) > n:
            resp["NextContinuationToken"] = page[-1]
        return resp

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None, **_: Any) -> dict[str, Any]:
        import io

        data = self.objects[(Bucket, Key)]
        if Range is None:
            self.log.record(Key, 0, len(data) - 1, len(data))
            return {"Body": io.BytesIO(data)}
        assert Range.startswith("bytes=")
        lo, hi = (int(x) for x in Range[len("bytes="):].split("-"))
        out = data[lo : hi + 1]
        self.log.record(Key, lo, hi, len(out))
        return {"Body": io.BytesIO(out)}


def _cohort_objects(bucket: str, prefix: str) -> dict[tuple[str, str], bytes]:
    objs: dict[tuple[str, str], bytes] = {}
    for i in range(1, 4):
        for r in (1, 2):
            objs[(bucket, f"{prefix}S{i}_R{r}.fastq.gz")] = gzip.compress(fastq_text(60).encode(), mtime=0)
    objs[(bucket, f"{prefix}S1.bam")] = make_bam()
    objs[(bucket, f"{prefix}manifest.csv")] = b"sample,path\nS1,x\nS2,y\n"
    objs[(bucket, f"{prefix}sub/")] = b""  # console-style folder placeholder
    objs[(bucket, "other/ignore.txt")] = b"not in the prefix\n"
    objs[(bucket, f"{prefix[:-1]}2/sibling.txt")] = b"sibling prefix, must be excluded\n"
    return objs


@pytest.fixture(params=["gs", "s3"])
def remote(request: pytest.FixtureRequest) -> tuple[str, Any]:
    objs = _cohort_objects("bkt", "runs/oct/")
    if request.param == "gs":
        return "gs", RangeGCS(objs)
    return "s3", RangeS3(objs, page_size=4)


def test_remote_cohort_summary(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    d = sk8s.describe(f"{scheme}://bkt/runs/oct/", client=client)
    assert d["kind"] == "cohort"
    assert d["path"] == f"{scheme}://bkt/runs/oct/"
    assert d["file_count"] == 8  # 6 fastq + bam + csv; placeholder, other/ and runs/oct2/ excluded
    assert d["formats"]["fastq"]["count"] == 6
    assert d["patterns"]["fastq"]["paired_end_pairs"] == 3
    assert d["headline"].startswith("Found 3 samples (FASTQ, paired-end, 6 files")
    assert d["samples"][0]["path"] == f"{scheme}://bkt/runs/oct/S1.bam"
    assert d["samples"][0]["format"] == "bam"
    assert d["listing_capped"] is False


def test_remote_prefix_without_trailing_slash_is_a_prefix(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    d = sk8s.describe(f"{scheme}://bkt/runs/oct", client=client)
    assert d["kind"] == "cohort"
    assert d["file_count"] == 8


def test_remote_reads_are_ranges_of_sampled_files_only(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    d = datadescribe.describe(f"{scheme}://bkt/runs/oct/", client=client, head_bytes=128)
    assert len(d["samples"]) == 5
    sampled_keys = {s["relative_path"] for s in d["samples"]}
    read_keys = {k.removeprefix("runs/oct/") for k, _, _ in client.log.reads}
    assert read_keys == sampled_keys  # nothing outside the first 5 was touched
    assert all(start == 0 and end - start + 1 <= 128 for _, start, end in client.log.reads)
    # a FASTQ larger than the head is sniffed but not counted
    fq = next(s for s in d["samples"] if s["format"] == "fastq")
    assert fq["facts"]["records"] == "not counted"
    assert fq["facts"]["read_length"] == 8


def test_remote_small_object_wholly_in_head_is_counted(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    client.objects[("bkt", "tiny/s.fastq")] = fastq_text(7).encode()
    d = sk8s.describe(f"{scheme}://bkt/tiny/s.fastq", client=client)
    assert d["facts"]["records"] == 7


def test_remote_single_file(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    d = sk8s.describe(f"{scheme}://bkt/runs/oct/S1.bam", client=client)
    assert d["kind"] == "file"
    assert d["path"] == f"{scheme}://bkt/runs/oct/S1.bam"
    assert d["format"] == "bam"
    assert d["facts"]["sort_order"] == "coordinate"
    assert d["size_bytes"] == len(client.objects[("bkt", "runs/oct/S1.bam")])
    assert len(client.log.reads) == 1 and client.log.reads[0][1] == 0


def test_remote_never_reads_more_than_head_bytes_of_a_big_object(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    big = gzip.compress(fastq_text(20000).encode(), mtime=0)  # ~ 100s of KB
    client.objects[("bkt", "big/x.fastq.gz")] = big
    d = datadescribe.describe(f"{scheme}://bkt/big/x.fastq.gz", client=client, head_bytes=4096)
    assert d["format"] == "fastq"
    assert d["facts"]["records"] == "not counted"
    assert client.log.served <= 4096 < len(big)


def test_remote_listing_cap(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    d = datadescribe.describe(f"{scheme}://bkt/runs/oct/", client=client, max_objects=3)
    assert d["listing_capped"] is True
    assert d["file_count"] == 3
    assert any("capped" in n for n in d["notes"])


def test_remote_missing_object_or_prefix(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    with pytest.raises(BlobNotFound, match="nothing at"):
        sk8s.describe(f"{scheme}://bkt/no/such/thing", client=client)


def test_remote_result_is_json(remote: tuple[str, Any]) -> None:
    scheme, client = remote
    d = sk8s.describe(f"{scheme}://bkt/runs/oct/", client=client)
    assert json.loads(json.dumps(d)) == d


def test_remote_bad_uri() -> None:
    with pytest.raises(Sk8sError, match="bucket"):
        sk8s.describe("gs://", client=object())


@pytest.mark.parametrize("scheme,module,extra", [("gs", "google.cloud.storage", "gcs"), ("s3", "boto3", "s3")])
def test_missing_extra_names_the_pip_extra(
    monkeypatch: pytest.MonkeyPatch, scheme: str, module: str, extra: str
) -> None:
    monkeypatch.setitem(sys.modules, module, None)  # makes `import module` raise ImportError
    with pytest.raises(Sk8sError, match=rf"sk8s\[{extra}\]"):
        sk8s.describe(f"{scheme}://bkt/x/")


def test_gcs_missing_credentials_is_typed_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.auth.exceptions import DefaultCredentialsError

    def boom() -> Any:
        raise DefaultCredentialsError("Your default credentials were not found")

    monkeypatch.setitem(sys.modules, "google.cloud.storage", types.SimpleNamespace(Client=boom))
    with pytest.raises(StoreError, match="application-default login"):
        sk8s.describe("gs://bkt/x/")


def test_s3_missing_credentials_is_typed_with_hint() -> None:
    from botocore.exceptions import NoCredentialsError

    class NoCreds:
        def list_objects_v2(self, **_: Any) -> Any:
            raise NoCredentialsError()

    with pytest.raises(StoreError, match="AWS credentials"):
        sk8s.describe("s3://bkt/x/", client=NoCreds())


def test_s3_access_denied_is_typed() -> None:
    from botocore.exceptions import ClientError

    class Denied:
        def list_objects_v2(self, **_: Any) -> Any:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListObjectsV2")

    with pytest.raises(StoreError, match="access denied"):
        sk8s.describe("s3://bkt/x/", client=Denied())


def test_gcs_forbidden_is_typed() -> None:
    from google.api_core.exceptions import Forbidden

    class Denied:
        def list_blobs(self, *a: Any, **k: Any) -> Any:
            raise Forbidden("caller does not have storage.objects.list")

    with pytest.raises(StoreError, match="access denied"):
        sk8s.describe("gs://bkt/x/", client=Denied())


def test_s3_endpoint_env_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_client(service: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return RangeS3({("bkt", "x/a.txt"): b"hello\nworld\n"})

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=fake_client))
    monkeypatch.setenv("SK8S_S3_ENDPOINT", "http://minio:9000")
    d = sk8s.describe("s3://bkt/x/")
    assert d["file_count"] == 1
    assert seen == {"endpoint_url": "http://minio:9000"}


def test_human_output_truncates_giant_values_but_json_keeps_them(tmp_path: Path) -> None:
    text = BAM_TEXT.replace("bwa mem -t 8", "bwa mem " + "-x " * 400)
    d = sk8s.describe(str(write(tmp_path / "long.bam", make_bam(text))))
    assert len(d["facts"]["aligner"]["command_line"]) > 1000
    human = datadescribe.format_description(d)
    assert "use --json for all" in human and len(human) < 2500


# --- hardening: corrupt and hostile input must never raise or hang -----------------------------------


def test_head_bytes_is_one_mebibyte() -> None:
    assert datadescribe.HEAD_BYTES == 1 << 20


def _bam_from_inflated(raw: bytes) -> bytes:
    return bgzf_block(raw) + BGZF_EOF


@pytest.mark.timeout(10)
def test_bam_negative_text_length() -> None:
    raw = b"BAM\x01" + struct.pack("<i", -5) + b"x" * 20
    d = datadescribe.sniff("a.bam", _bam_from_inflated(raw), 1000)
    assert d["format"] == "bam"
    assert d["facts"]["header_truncated"] is True
    assert any("negative header text length" in n for n in d["notes"])


@pytest.mark.timeout(10)
def test_bam_negative_name_length_with_huge_reference_count_terminates() -> None:
    raw = b"BAM\x01" + struct.pack("<i", 0) + struct.pack("<i", 2**31 - 1) + (struct.pack("<i", -8) + b"\0" * 4) * 100
    d = datadescribe.sniff("a.bam", _bam_from_inflated(raw), 1000)
    assert d["format"] == "bam"
    assert d["facts"]["references"] == []
    assert any("reference name length -8" in n for n in d["notes"])
    assert "at least" in d["facts"]["reference_count_note"]


@pytest.mark.timeout(10)
def test_bam_zero_length_names_with_huge_reference_count_terminates() -> None:
    raw = b"BAM\x01" + struct.pack("<i", 0) + struct.pack("<i", 1_000_000) + (struct.pack("<ii", 0, 5)) * 100
    d = datadescribe.sniff("a.bam", _bam_from_inflated(raw), 1000)
    assert d["format"] == "bam"
    assert len(d["facts"]["references"]) == 5


def test_bam_huge_text_length_is_just_truncated() -> None:
    raw = b"BAM\x01" + struct.pack("<i", 2**31 - 1) + b"@HD\tVN:1.6\n"
    d = datadescribe.sniff("a.bam", _bam_from_inflated(raw), 1000)
    assert d["facts"]["header_truncated"] is True
    assert d["facts"]["sam_version"] == "1.6"


def test_bam_unicode_digit_in_sq_length_does_not_crash() -> None:
    raw = bam_header_bytes("@SQ\tSN:x\tLN:²\n@SQ\tSN:y\tLN:٣\n@SQ\tSN:z\tLN:7\n", [])
    d = datadescribe.sniff("a.bam", _bam_from_inflated(raw), 1000)
    assert d["format"] == "bam"


def test_bam_reference_count_says_at_least_when_the_table_is_cut(tmp_path: Path) -> None:
    refs = [(f"contig{i}", 5000 + i) for i in range(3000)]
    raw = bam_header_bytes("@HD\tVN:1.6\n", refs)
    data = b"".join(bgzf_block(raw[i : i + 20000]) for i in range(0, len(raw), 20000))
    d = datadescribe.sniff("a.bam", data[: len(data) // 3], len(data))
    f = d["facts"]
    assert f["header_truncated"] is True
    assert f["reference_count"] == len(refs)  # the header itself declares 3000
    assert "at least" not in f.get("reference_count_note", "")  # ...so this one is exact
    # and with the declared count unreachable, the figure is explicitly a lower bound
    d2 = datadescribe.sniff("a.bam", bgzf_block(b"BAM\x01" + struct.pack("<i", 50) + b"@HD\n"[:4] * 5)[:60], 10**6)
    assert "at least" in d2["facts"]["reference_count_note"]


def test_single_line_400kb_fasta_is_fasta_not_a_crash(tmp_path: Path) -> None:
    p = write(tmp_path / "chr.txt", ">chr1\n" + "ACGT" * 100_000 + "\n>chr2\nACGT\n")
    d = sk8s.describe(str(p))
    assert d["format"] == "fasta" and d["facts"]["total_residues"] == 400_004
    p2 = write(tmp_path / "chr.csv", ">chr1\n" + "ACGT" * 100_000 + "\n>chr2\nACGT\n")
    d2 = sk8s.describe(str(p2))
    assert d2["format"] == "fasta"  # the content wins over the .csv name, and says so
    assert any(".csv" in n and "fasta" in n for n in d2["notes"])


# --- FASTA, .fai, .bai --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ref.fa", "ref.fasta", "ref.fna", "ref.txt", "ref", "ref.seq"])
def test_fasta_is_detected_by_content_not_extension(tmp_path: Path, name: str) -> None:
    d = sk8s.describe(str(write(tmp_path / name, fasta_text(7, 130, 60))))
    assert d["format"] == "fasta"
    assert d["confidence"] == "content"
    f = d["facts"]
    assert f["contigs"] == ["chr1", "chr2", "chr3", "chr4", "chr5"]  # first five, header words after the name dropped
    assert f["contig_count"] == 7
    assert f["total_residues"] == 7 * 130
    assert f["sequence_type"] == "nucleotide"
    assert f["line_width"] == 60
    assert "contig_count_note" not in f


def test_fasta_gz_and_lowercase_and_blank_lines(tmp_path: Path) -> None:
    text = ">a\nacgtnn\n\n>b\nACGT\n"
    d = sk8s.describe(str(write(tmp_path / "r.fa.gz", gzip.compress(text.encode()))))
    assert d["format"] == "fasta" and d["compression"] == "gzip"
    assert d["facts"]["contig_count"] == 2 and d["facts"]["total_residues"] == 10


def test_protein_fasta(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "p.faa", ">sp|P1|X\nMKVLAAGIVGLLLAQPSEEDFHW\nLLEPRQ\n")))
    assert d["format"] == "fasta" and d["facts"]["sequence_type"] == "protein"
    assert d["facts"]["contigs"] == ["sp|P1|X"]


def test_fasta_with_a_truncated_head_is_a_lower_bound(tmp_path: Path) -> None:
    text = fasta_text(40, 130, 60).encode()
    d = datadescribe.sniff("big.fa", text[:1500], 50_000_000)  # a huge file of which we hold 1500 bytes
    f = d["facts"]
    assert d["format"] == "fasta"
    assert f["contigs"][:2] == ["chr1", "chr2"]
    assert f["total_residues"] == "not counted"
    assert 1 < f["contig_count"] < 40 and "at least" in f["contig_count_note"]


def test_fasta_ext_with_prose_content_is_not_trusted(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "ref.fa", "This is not a sequence file at all.\nJust prose.\n")))
    assert d["format"] == "text"
    assert any(".fa" in n for n in d["notes"])


def test_empty_fasta_extension_is_extension_confidence(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "empty.fa", b"")))
    assert d["format"] == "fasta" and d["confidence"] == "extension"


def test_markdown_quote_is_not_fasta(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "q.txt", "> a quoted line with spaces\n> and another\n")))
    assert d["format"] == "text"


def test_fai_is_parsed(tmp_path: Path) -> None:
    rows = [(f"chr{i}", 1000 * i) for i in range(1, 9)]
    d = sk8s.describe(str(write(tmp_path / "ref.fa.fai", fai_text(rows))))
    assert d["format"] == "fasta-index (fai)"
    assert d["confidence"] == "content"
    f = d["facts"]
    assert f["contig_count"] == 8
    assert f["contigs"] == [{"name": f"chr{i}", "length": 1000 * i} for i in range(1, 6)]
    assert f["total_length"] == sum(ln for _, ln in rows)


def test_fai_six_columns_fastq_index(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "r.fq.fai", fai_text([("r1", 50)], cols=6))))
    assert d["format"] == "fasta-index (fai)"


def test_fai_name_with_other_content_is_not_trusted(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "x.fai", "a\tb\tc\nd\te\tf\n")))
    assert d["format"] != "fasta-index (fai)"
    assert any(".fai" in n for n in d["notes"])


def test_fai_shaped_table_without_fai_name_stays_a_table(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "t.tsv", fai_text([("chr1", 100), ("chr2", 200)]))))
    assert d["format"] == "tsv"


def test_bai_is_identified_by_magic(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "a.bam.bai", bai_bytes(25))))
    assert d["format"] == "bam-index (bai)"
    assert d["confidence"] == "magic"
    assert d["facts"]["reference_count"] == 25
    assert sk8s.describe(str(write(tmp_path / "noext", bai_bytes(2))))["format"] == "bam-index (bai)"


def test_bai_magic_only_has_no_facts_and_no_crash(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "a.bai", b"BAI\x01")))
    assert d["format"] == "bam-index (bai)" and d["facts"] == {}


def test_new_formats_render_for_humans_and_in_cohorts(tmp_path: Path) -> None:
    write(tmp_path / "a.bam", make_bam())
    write(tmp_path / "a.bam.bai", bai_bytes(2))
    write(tmp_path / "ref.fa", fasta_text(3))
    write(tmp_path / "ref.fa.fai", fai_text([("chr1", 130), ("chr2", 130), ("chr3", 130)]))
    d = sk8s.describe(str(tmp_path))
    by_name = {s["name"]: s for s in d["samples"]}
    assert by_name["ref.fa"]["format"] == "fasta"
    assert by_name["ref.fa.fai"]["format"] == "fasta-index (fai)"
    assert by_name["a.bam.bai"]["format"] == "bam-index (bai)"
    text = datadescribe.format_description(d)
    assert "fasta (content; 3 contigs, nucleotide)" in text
    assert "fasta-index (fai) (content; 3 contigs)" in text
    assert "bam-index (bai) (magic; 2 refs)" in text
    single = datadescribe.format_description(sk8s.describe(str(tmp_path / "ref.fa")))
    assert "contigs: " in single and "chr1" in single and "total_residues: 390" in single


def test_corrupt_sampled_file_is_recorded_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "a.bam", make_bam())
    write(tmp_path / "b.fastq", fastq_text(2))
    real = datadescribe.sniff

    def exploding(name: str, head: bytes, size: int, **kw: Any) -> Any:
        if name == "a.bam":
            raise struct.error("unpack requires a buffer of 4 bytes")
        return real(name, head, size, **kw)

    monkeypatch.setattr(datadescribe, "sniff", exploding)
    d = sk8s.describe(str(tmp_path))
    assert "could not parse header" in d["samples"][0]["error"]
    assert d["samples"][1]["format"] == "fastq"
    with pytest.raises(Sk8sError, match="could not parse header"):
        sk8s.describe(str(tmp_path / "a.bam"))


def _mutations(seed: int, data: bytes, n: int) -> list[bytes]:
    import random

    rng = random.Random(seed)
    out = []
    for _ in range(n):
        b = bytearray(data)
        for _ in range(rng.randint(1, 6)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        if rng.random() < 0.3:
            b = b[: rng.randrange(1, len(b))]
        out.append(bytes(b))
    return out


@pytest.mark.timeout(60)
def test_mini_fuzz_sniff_never_raises() -> None:
    bam_raw = bam_header_bytes(BAM_TEXT, BAM_REFS)
    cases: list[tuple[str, bytes, bool]] = [  # (name, seed bytes, wrap mutated bytes in BGZF)
        ("x.bam", bam_raw, True),
        ("x.vcf", VCF_TEXT.encode(), False),
        ("x.vcf.gz", VCF_TEXT.encode(), True),
        ("x.fastq", fastq_text(6).encode(), False),
        ("x.csv", b"a,b,c\n1,2,3\n4,5,6\n", False),
        ("x.tsv.gz", b"a\tb\n1\t2\n3\t4\n", True),
        ("x.cram", b"CRAM\x03\x01" + b"id" * 10 + b"\x00" * 30, False),
        ("x.fa", fasta_text(4, 90, 30).encode(), False),
        ("x.fasta.gz", fasta_text(4, 90, 30).encode(), True),
        ("x.fa.fai", fai_text([("chr1", 100), ("chr2", 200), ("chr3", 300)]).encode(), False),
        ("x.fai", fai_text([("chr1", 100), ("chr2", 200)], cols=6).encode(), False),
        ("x.bam.bai", bai_bytes(3), False),
    ]
    for seed, (name, raw, wrap) in enumerate(cases):
        for mutated in _mutations(seed, raw, 60):
            data = bgzf_block(mutated) + BGZF_EOF if wrap else mutated
            if wrap and seed % 2 == 0:
                data = data[: max(1, len(data) - seed * 3)]  # also cut the compressed stream
            r = datadescribe.sniff(name, data, len(data))
            json.dumps(r)  # and the result stays serializable
    # and raw random bytes behind every magic
    import random

    rng = random.Random(99)
    for _ in range(200):
        junk = bytes(rng.randrange(256) for _ in range(rng.randint(0, 120)))
        for prefix in (b"", b"\x1f\x8b\x08", b"CRAM", b"BAI\x01", b">chr1\n", b"chr1\t100\t", b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00BC\x02\x00"):
            for fuzz_name in ("x.bam", "x.fa", "x.fai"):
                datadescribe.sniff(fuzz_name, prefix + junk, len(prefix + junk))


def test_header_only_fasta_named_fa_is_extension_confidence(tmp_path: Path) -> None:
    d = sk8s.describe(str(write(tmp_path / "h.fa", ">chr1\n")))
    assert d["format"] == "fasta" and d["confidence"] == "extension"
