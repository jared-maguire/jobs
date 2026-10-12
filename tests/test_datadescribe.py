"""sk8s.describe: format sniffing, cohort summaries, remote range-reads (fake SDK clients)."""
import gzip
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

import sk8s
from sk8s import datadescribe
from sk8s.errors import BlobNotFound, Sk8sError

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


def test_human_rendering_mentions_key_facts(tmp_path: Path) -> None:
    _fastq_cohort(tmp_path, samples=2)
    out = datadescribe.format_description(sk8s.describe(str(tmp_path)))
    assert "Found 2 samples" in out
    assert "fastq" in out and "read length 8" in out
    single = datadescribe.format_description(sk8s.describe(str(write(tmp_path / "x.bam", make_bam()))))
    assert "bam" in single and "coordinate" in single and "chr1" in single
