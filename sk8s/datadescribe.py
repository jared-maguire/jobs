"""``sk8s.describe``: what is this data? Format sniffing and cohort summaries, stdlib only.

``describe(path_or_uri)`` answers the first question of any analysis ("what do I have?") from
the bytes themselves: for one file, the detected format plus the facts its header carries
(read length, BAM sort order and references, VCF samples, CSV columns, FASTA contigs, FASTA / BAM
index contents, ...); for a directory
or ``gs://`` / ``s3://`` prefix, a cohort summary (counts by format, sizes, naming patterns such
as FASTQ R1/R2 pairing, and per-file facts for the first few files).

Design rules:

- Dependency-light: only gzip/zlib/struct/csv/json. No pysam/pyarrow.
- Remote files are never read whole: only the first ``head_bytes`` (default 1 MiB) of each
  *sampled* file are range-read; the rest of a cohort is classified by extension.
- Every detection carries a ``confidence``: ``"magic"`` (a format signature), ``"content"``
  (the bytes parse as the format) or ``"extension"`` (only the file name says so).
- The result is plain JSON-serializable data.
"""
from __future__ import annotations

import codecs
import csv
import importlib
import json
import os
import re
import statistics
import struct
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from sk8s.errors import BlobNotFound, Sk8sError, StoreError
from sk8s.store.s3 import ENDPOINT_ENV

HEAD_BYTES = 1 << 20  # how much of a file is read for sniffing (1 MiB)
SMALL_FILE_BYTES = 10_000_000  # records are counted only for files smaller than this
MAX_OBJECTS = 10_000  # listing cap for cohorts
SAMPLES = 5  # files whose facts are reported for a cohort
_MAX_INFLATE = 64 * 1024 * 1024  # never inflate more than this from one head
_HUMAN_VALUE_MAX = 300  # longer fact values (aligner command lines) are cut in human output
_FIRST_N = 5  # "first 5" lists inside facts (references, samples, read groups)
_FASTQ_SAMPLE_RECORDS = 1000
_TABLE_SAMPLE_ROWS = 200
_MAX_COLUMNS_LISTED = 20

_KNOWN_ALIGNERS = (
    "bwa-mem2", "bwa", "minimap2", "bowtie2", "bowtie", "hisat2", "star", "novoalign",
    "dragmap", "dragen", "snap", "gsnap", "tophat", "last", "ngmlr", "pbmm2",
)

Facts = dict[str, Any]


# --- extensions ---------------------------------------------------------------------------------

_EXT_FORMATS = {
    ".fastq": "fastq", ".fq": "fastq", ".bam": "bam", ".cram": "cram", ".vcf": "vcf",
    ".csv": "csv", ".tsv": "tsv", ".tab": "tsv", ".txt": "text",
    ".fa": "fasta", ".fasta": "fasta", ".fna": "fasta",
    ".bai": "index", ".crai": "index", ".tbi": "index", ".csi": "index", ".fai": "index",
}
_GZ_EXTS = (".gz", ".bgz")
_PRIORITY = ("fastq", "bam", "cram", "vcf", "csv", "tsv", "fasta")  # headline tie-break, most interesting first
_AUXILIARY_FORMATS = {"index", "unknown", "text"}
_CHECKED_FORMATS = {"fastq", "bam", "cram", "vcf", "csv", "tsv", "fasta"}  # extensions we hold content to


def extension_format(name: str) -> str:
    """The format a file *name* claims: fastq, bam, cram, vcf, csv, tsv, fasta, text, index, gzip, unknown."""
    lower = name.lower()
    gz = lower.endswith(_GZ_EXTS)
    if gz:
        lower = lower.rsplit(".", 1)[0]
    ext = os.path.splitext(lower)[1]
    fmt = _EXT_FORMATS.get(ext)
    if fmt is not None:
        return fmt
    return "gzip" if gz else "unknown"


# --- gzip / BGZF --------------------------------------------------------------------------------


@dataclass
class _Inflated:
    data: bytes
    eof: bool  # the compressed input was consumed to a clean end (not truncated, not capped)
    error: str | None = None


def _is_bgzf(head: bytes) -> bool:
    """gzip with a 'BC' extra subfield (the BGZF block-size marker)."""
    if len(head) < 18 or head[:3] != b"\x1f\x8b\x08" or not head[3] & 4:
        return False
    xlen = struct.unpack_from("<H", head, 10)[0]
    extra = head[12 : 12 + xlen]
    i = 0
    while i + 4 <= len(extra):
        si, slen = extra[i : i + 2], struct.unpack_from("<H", extra, i + 2)[0]
        if si == b"BC" and slen == 2:
            return True
        i += 4 + slen
    return False


def _gunzip_prefix(data: bytes, limit: int = _MAX_INFLATE) -> _Inflated:
    """Inflate as much of a (multi-member, possibly truncated) gzip stream as ``data`` holds."""
    out: list[bytes] = []
    total = 0
    rest = data
    while True:
        if not rest:
            return _Inflated(b"".join(out), eof=True)
        if rest[:2] != b"\x1f\x8b":
            # Trailing garbage after a complete member: tolerated only if it is zero padding.
            ok = not rest.strip(b"\x00")
            return _Inflated(b"".join(out), eof=ok, error=None if ok else "trailing bytes after gzip stream")
        d = zlib.decompressobj(wbits=31)
        try:
            chunk = d.decompress(rest, limit - total)
        except zlib.error as exc:
            return _Inflated(b"".join(out), eof=False, error=f"corrupt gzip stream ({exc})")
        out.append(chunk)
        total += len(chunk)
        if not d.eof:  # truncated input, or the inflate limit was reached
            return _Inflated(b"".join(out), eof=False)
        if total >= limit:
            return _Inflated(b"".join(out), eof=False)
        rest = d.unused_data


# --- text helpers -------------------------------------------------------------------------------


def _decode_text(data: bytes) -> str | None:
    """UTF-8 text, or None when the bytes are binary. A multibyte char cut by truncation is fine."""
    if b"\x00" in data[:65536]:
        return None
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(data, final=False)
    except UnicodeDecodeError:
        return None


def _text_lines(data: bytes, complete: bool) -> list[str] | None:
    """Decoded lines of ``data``; when the head cuts the file short, the trailing partial line is dropped."""
    text = _decode_text(data)
    if text is None:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    elif not complete and lines:
        lines.pop()  # cut mid-line
    return lines


# --- FASTQ --------------------------------------------------------------------------------------

_SEQ_CHARS = re.compile(r"^[A-Za-z.*\-]+$")


def _fastq_records(lines: list[str]) -> list[tuple[str, str, str, str]]:
    recs = []
    for i in range(0, len(lines) - 3, 4):
        recs.append((lines[i], lines[i + 1], lines[i + 2], lines[i + 3]))
    return recs


def _looks_like_fastq(lines: list[str]) -> bool:
    if len(lines) < 4:
        return False
    head, seq, plus, qual = lines[0], lines[1], lines[2], lines[3]
    return (
        head.startswith("@") and plus.startswith("+") and len(seq) == len(qual) > 0
        and _SEQ_CHARS.match(seq) is not None
    )


def _phred_guess(quals: list[str]) -> tuple[int, str]:
    chars = "".join(quals)
    if not chars:
        return 33, "no quality data"
    lo, hi = min(map(ord, chars)), max(map(ord, chars))
    if lo < 59:
        return 33, f"lowest quality char {chr(lo)!r} ({lo}) is below 59, only possible with offset 33"
    if hi > 74:
        return 64, f"highest quality char {chr(hi)!r} ({hi}) is above 'J' (74), the ceiling for offset 33"
    return 33, f"quality chars span {lo}-{hi}, valid for either offset; 33 is the modern default"


def _fastq_facts(lines: list[str], *, countable: bool, notes: list[str]) -> Facts:
    sampled = _fastq_records(lines[: _FASTQ_SAMPLE_RECORDS * 4])
    lengths = [len(r[1]) for r in sampled]
    offset, evidence = _phred_guess([r[3] for r in sampled])
    facts: Facts = {
        "first_read_id": sampled[0][0][1:].split(" ", 1)[0].split("\t", 1)[0],
        "read_length": lengths[0],
        "read_length_min": min(lengths),
        "read_length_max": max(lengths),
        "sampled_records": len(sampled),
        "phred_offset_guess": offset,
        "phred_evidence": evidence,
    }
    if countable:
        facts["records"] = len(lines) // 4
        if len(lines) % 4:
            notes.append(f"line count ({len(lines)}) is not a multiple of 4: truncated or malformed FASTQ")
    else:
        facts["records"] = "not counted"
    return facts


# --- FASTA and its indexes ----------------------------------------------------------------------

_FASTA_SEQ = re.compile(r"^[A-Za-z*\-.]+$")
_FASTA_PROBE_LINES = 5000  # lines checked to decide "this is FASTA"; counting then trusts the rest
_NUCLEOTIDES = frozenset("ACGTUNacgtun")
PARTIAL_HEAD_NOTE = "at least this many: only the first part of the file was read"
FASTA_INDEX_FORMAT = "fasta-index (fai)"
BAM_INDEX_FORMAT = "bam-index (bai)"


def _looks_like_fasta(lines: list[str]) -> bool:
    probe = [ln.rstrip() for ln in lines[:_FASTA_PROBE_LINES] if ln.strip()]
    # at least one sequence line: a file of nothing but "> ..." lines is quoted text, not FASTA
    return bool(probe) and probe[0].startswith(">") and any(not ln.startswith(">") for ln in probe) and all(
        ln.startswith(">") or _FASTA_SEQ.match(ln) is not None for ln in probe
    )


def _fasta_facts(lines: list[str], *, countable: bool) -> Facts:
    names: list[str] = []
    contigs = 0
    residues = 0
    nucleotides = 0
    width_votes: Counter[int] = Counter()
    prev_len: int | None = None  # the previous sequence line of the current record (not its last line)
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            contigs += 1
            if len(names) < _FIRST_N:
                header = line[1:].split(None, 1)
                names.append(header[0] if header else "")
            prev_len = None
            continue
        if prev_len is not None:
            width_votes[prev_len] += 1
        prev_len = len(line)
        residues += len(line)
        if residues <= 1_000_000:  # the alphabet guess needs a sample, not every residue
            nucleotides += sum(1 for c in line if c in _NUCLEOTIDES)
    sampled = min(residues, 1_000_000)
    facts: Facts = {
        "contigs": names,
        "contig_count": contigs,
        "sequence_type": "nucleotide" if sampled and nucleotides / sampled >= 0.9 else ("protein" if sampled else "none"),
        "line_width": width_votes.most_common(1)[0][0] if width_votes else None,
    }
    if countable:
        facts["total_residues"] = residues
    else:
        facts["total_residues"] = "not counted"
        facts["contig_count_note"] = PARTIAL_HEAD_NOTE
    return facts


def _parse_fai(lines: list[str]) -> list[tuple[str, int]] | None:
    """(name, length) per row of a FASTA index (name, length, offset, linebases, linewidth), or None."""
    rows: list[tuple[str, int]] = []
    for line in lines:
        if not line.strip():
            continue
        cols = line.rstrip("\r").split("\t")
        if len(cols) not in (5, 6) or not cols[0]:
            return None
        try:
            length, *rest = (int(c) for c in cols[1:5])
        except ValueError:
            return None
        if length < 0 or any(v < 0 for v in rest):
            return None
        rows.append((cols[0], length))
    return rows or None


def _fai_facts(rows: list[tuple[str, int]], *, complete: bool) -> Facts:
    facts: Facts = {
        "contig_count": len(rows),
        "contigs": [{"name": n, "length": ln} for n, ln in rows[:_FIRST_N]],
        "total_length": sum(ln for _, ln in rows),
    }
    if not complete:
        facts["contig_count_note"] = PARTIAL_HEAD_NOTE
    return facts


def _bai_facts(head: bytes) -> Facts:
    facts: Facts = {}
    if len(head) >= 8:
        facts["reference_count"] = struct.unpack_from("<i", head, 4)[0]
    return facts


# --- SAM header text (shared by BAM) ------------------------------------------------------------


def _parse_sam_header(text: str) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {"HD": [], "SQ": [], "RG": [], "PG": [], "CO": []}
    for line in text.split("\n"):
        if not line.startswith("@") or len(line) < 3:
            continue
        kind = line[1:3]
        if kind == "CO":
            out["CO"].append({"text": line[4:]})
            continue
        fields: dict[str, str] = {}
        for part in line.split("\t")[1:]:
            if len(part) >= 3 and part[2] == ":":
                fields[part[:2]] = part[3:]
        out.setdefault(kind, []).append(fields)
    return out


def _find_aligner(pgs: list[dict[str, str]]) -> dict[str, str] | None:
    for pg in pgs:
        ident = pg.get("ID", "").lower()
        prog = pg.get("PN", "").lower()
        for aligner in _KNOWN_ALIGNERS:
            if prog == aligner or ident == aligner or ident.startswith(aligner + "-") or prog.startswith(aligner):
                found = {"name": pg.get("PN", pg.get("ID", aligner)), "id": pg.get("ID", "")}
                if "VN" in pg:
                    found["version"] = pg["VN"]
                if "CL" in pg:
                    found["command_line"] = pg["CL"]
                return found
    return None


def _bam_facts(data: bytes, notes: list[str]) -> Facts:
    """Parse the BAM header from inflated bytes (which may end mid-header, or be corrupt).

    Never raises and always terminates: every length field is validated against the bytes in hand and
    the reference loop is bounded by what the data could hold. A violation stops parsing, keeps what was
    parsed so far and appends a note.
    """
    truncated = False  # ran out of sniffed bytes (benign: the head is only a prefix)
    corrupt = False  # a field was invalid (the file itself is suspect)
    text_bytes = b""
    refs: list[dict[str, Any]] = []
    n_ref: int | None = None

    def bad(why: str) -> None:
        nonlocal corrupt
        corrupt = True
        notes.append(f"BAM header looks corrupt ({why}); stopped parsing it")

    if len(data) < 8:
        truncated = True
    else:
        l_text = struct.unpack_from("<i", data, 4)[0]
        if l_text < 0:
            bad(f"negative header text length {l_text}")
        else:
            text_bytes = data[8 : 8 + l_text]
            pos = 8 + l_text
            if len(text_bytes) < l_text or len(data) < pos + 4:
                truncated = True
            else:
                declared = struct.unpack_from("<i", data, pos)[0]
                pos += 4
                if declared < 0:
                    bad(f"negative reference count {declared}")
                else:
                    n_ref = declared
                    for _ in range(min(declared, len(data) // 9)):  # each reference needs >= 9 bytes
                        if len(data) < pos + 4:
                            truncated = True
                            break
                        l_name = struct.unpack_from("<i", data, pos)[0]
                        if l_name < 0 or l_name > len(data):
                            bad(f"reference name length {l_name}")
                            break
                        end = pos + 4 + l_name + 4  # > pos always, so the loop strictly advances
                        if end > len(data):
                            truncated = True
                            break
                        name = data[pos + 4 : pos + 4 + l_name].rstrip(b"\x00").decode("utf-8", "replace")
                        refs.append({"name": name, "length": struct.unpack_from("<i", data, end - 4)[0]})
                        pos = end
                    if len(refs) < declared and not corrupt:
                        truncated = True
    text = text_bytes.decode("utf-8", "replace")
    if truncated and "\n" in text:
        text = text[: text.rindex("\n") + 1]  # drop a header line cut in half
    header = _parse_sam_header(text)
    if not refs and header["SQ"]:  # references from @SQ when the binary table was not reached
        refs = [
            {"name": sq.get("SN", ""), "length": int(sq["LN"])}
            for sq in header["SQ"]
            if sq.get("LN", "").isascii() and sq.get("LN", "").isdigit()
        ]
    exact = n_ref is not None and not corrupt
    hd = header["HD"][0] if header["HD"] else {}
    rgs = [
        {k: v for k, v in (("id", rg.get("ID")), ("sample", rg.get("SM")), ("platform", rg.get("PL")), ("library", rg.get("LB"))) if v is not None}
        for rg in header["RG"]
    ]
    facts: Facts = {
        "sam_version": hd.get("VN"),
        "sort_order": hd.get("SO"),
        "reference_count": n_ref if exact and n_ref is not None else len(refs),
        "references": refs[:_FIRST_N],
        "read_group_count": len(rgs),
        "read_groups": rgs[:_FIRST_N],
        "aligner": _find_aligner(header["PG"]),
        "programs": [pg.get("ID", "") for pg in header["PG"]][:10],
        "header_truncated": truncated or corrupt,
    }
    if not exact:
        facts["reference_count_note"] = (
            f"at least {len(refs)} references: the reference table was not fully read from the sniffed bytes"
        )
    return facts


# --- VCF ----------------------------------------------------------------------------------------


def _vcf_facts(lines: list[str]) -> Facts:
    meta: Facts = {"fileformat": lines[0].split("=", 1)[1]}
    contigs = 0
    samples: list[str] | None = None
    for line in lines[1:]:
        if line.startswith("##contig="):
            contigs += 1
        elif line.startswith("##reference="):
            meta["reference"] = line.split("=", 1)[1]
        elif line.startswith("##source="):
            meta["source"] = line.split("=", 1)[1]
        elif line.startswith("#CHROM"):
            cols = line.split("\t")
            samples = cols[9:]
            break
        elif not line.startswith("#"):
            break
    meta["contig_count"] = contigs
    meta["header_truncated"] = samples is None
    if samples is not None:
        meta["sample_count"] = len(samples)
        meta["samples"] = samples[:_FIRST_N]
    return meta


# --- delimited tables ---------------------------------------------------------------------------

_NULLS = {"", "na", "nan", "null", "none", "n/a", "."}
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _is_numeric(cell: str) -> bool:
    return bool(_INT.match(cell.strip()) or _FLOAT.match(cell.strip()))


def _type_guess(values: list[str]) -> str:
    vals = [v.strip() for v in values if v.strip().lower() not in _NULLS]
    if not vals:
        return "empty"
    if all(_INT.match(v) for v in vals):
        return "int"
    if all(_INT.match(v) or _FLOAT.match(v) for v in vals):
        return "float"
    if all(v.lower() in ("true", "false") for v in vals):
        return "bool"
    return "string"


def _parse_table(lines: list[str], delimiter: str) -> list[list[str]]:
    return list(csv.reader(lines, delimiter=delimiter))


def _sniff_delimiter(lines: list[str]) -> str | None:
    rows_text = [ln for ln in lines if ln.strip()][:_TABLE_SAMPLE_ROWS]
    if len(rows_text) < 2:
        return None
    best: tuple[float, int, str] | None = None
    for delim in (",", "\t", ";", "|"):
        try:
            counts = [len(r) for r in _parse_table(rows_text, delim)]
        except csv.Error:  # e.g. a field over csv's 128 KiB limit (an unwrapped FASTA line): not a table
            return None
        mode, freq = Counter(counts).most_common(1)[0]
        frac = freq / len(counts)
        if mode >= 2 and frac >= 0.9 and (best is None or (frac, mode) > best[:2]):
            best = (frac, mode, delim)
    return best[2] if best else None


def _table_facts(lines: list[str], delimiter: str) -> Facts:
    rows = [r for r in _parse_table([ln for ln in lines if ln.strip()][:_TABLE_SAMPLE_ROWS], delimiter) if r]
    first = rows[0]
    ncols = Counter(len(r) for r in rows).most_common(1)[0][0]
    distinct = len(set(first)) == len(first) and all(c.strip() for c in first)
    has_header = len(rows) >= 2 and distinct and not any(_is_numeric(c) for c in first)
    body = rows[1:] if has_header else rows
    names = [c.strip() for c in first] if has_header else [f"col{i + 1}" for i in range(ncols)]
    columns = []
    for i in range(min(ncols, _MAX_COLUMNS_LISTED)):
        values = [r[i] for r in body if i < len(r)]
        columns.append({"name": names[i] if i < len(names) else f"col{i + 1}", "type": _type_guess(values)})
    facts: Facts = {
        "delimiter": delimiter,
        "has_header": has_header,
        "column_count": ncols,
        "columns": columns,
        "sampled_rows": len(body),
    }
    if ncols > _MAX_COLUMNS_LISTED:
        facts["columns_truncated"] = True
    return facts


# --- the sniffer --------------------------------------------------------------------------------


def _result(fmt: str, confidence: str, compression: str | None, facts: Facts, notes: list[str]) -> dict[str, Any]:
    return {"format": fmt, "confidence": confidence, "compression": compression, "facts": facts, "notes": notes}


def sniff(name: str, head: bytes, size: int, *, small_file_bytes: int = SMALL_FILE_BYTES) -> dict[str, Any]:
    """Identify a file from its first bytes. ``head`` is the file's start (all of it if ``len(head) >= size``)."""
    complete = len(head) >= size
    notes: list[str] = []
    ext_fmt = extension_format(name)
    result = _sniff(name, head, size, complete, small_file_bytes, ext_fmt, notes)
    if ext_fmt in _CHECKED_FORMATS and result["format"] != ext_fmt:
        suffix = os.path.splitext(name.lower().removesuffix(".gz").removesuffix(".bgz"))[1]
        notes.append(f"name ends in {suffix} but the content looks like {result['format']}")
    return result


def _sniff(
    name: str, head: bytes, size: int, complete: bool, small_file_bytes: int, ext_fmt: str, notes: list[str]
) -> dict[str, Any]:
    if head[:4] == b"CRAM" and len(head) >= 6:
        facts: Facts = {"version": f"{head[4]}.{head[5]}"}
        if len(head) >= 26:
            facts["file_id"] = head[6:26].rstrip(b"\x00").decode("ascii", "replace")
        return _result("cram", "magic", None, facts, notes)

    if head[:4] == b"BAI\x01":
        return _result(BAM_INDEX_FORMAT, "magic", None, _bai_facts(head), notes)

    compression: str | None = None
    data = head
    data_complete = complete
    if head[:2] == b"\x1f\x8b":
        compression = "bgzf" if _is_bgzf(head) else "gzip"
        inflated = _gunzip_prefix(head)
        data = inflated.data
        data_complete = complete and inflated.eof
        if inflated.error:
            notes.append(inflated.error)
        if data[:4] == b"BAM\x01":
            return _result("bam", "magic", compression, _bam_facts(data, notes), notes)

    lines = _text_lines(data, data_complete)
    if lines is not None:
        if lines and lines[0].startswith("##fileformat=VCF"):
            return _result("vcf", "magic", compression, _vcf_facts(lines), notes)
        if _looks_like_fastq(lines):
            countable = data_complete and size < small_file_bytes
            return _result("fastq", "content", compression, _fastq_facts(lines, countable=countable, notes=notes), notes)
        is_fai_name = name.lower().removesuffix(".gz").removesuffix(".bgz").endswith(".fai")
        if is_fai_name:  # a headerless 5-column table is only called an index when its name says so
            fai = _parse_fai(lines)
            if fai is not None:
                return _result(FASTA_INDEX_FORMAT, "content", compression, _fai_facts(fai, complete=data_complete), notes)
            if any(ln.strip() for ln in lines):
                notes.append(
                    "name ends in .fai but the content is not a FASTA index "
                    "(tab-separated name, length, offset, line bases, line width)"
                )
        if _looks_like_fasta(lines):
            countable = data_complete and size < small_file_bytes
            return _result("fasta", "content", compression, _fasta_facts(lines, countable=countable), notes)
        delim = _sniff_delimiter(lines)
        if delim is not None:
            fmt = {",": "csv", "\t": "tsv"}.get(delim, "delimited")
            return _result(fmt, "content", compression, _table_facts(lines, delim), notes)
        # Nothing matched: lean on the extension only where the content is too thin to contradict it.
        thin = not any(ln.strip() for ln in lines)
        if ext_fmt in ("fastq", "vcf", "csv", "tsv", "fasta") and (
            thin or (ext_fmt == "fastq" and lines[0].startswith("@"))
            or (ext_fmt == "fasta" and lines[0].startswith(">")) or (ext_fmt in ("csv", "tsv") and lines)
        ):
            notes.append("too little content to confirm the format; identified from the file extension")
            facts = {}
            if ext_fmt in ("csv", "tsv") and not thin:
                try:
                    facts = _table_facts(lines, "," if ext_fmt == "csv" else "\t")
                except csv.Error as exc:
                    notes.append(f"could not parse as a table: {exc}")
            return _result(ext_fmt, "extension", compression, facts, notes)
        if not thin:
            return _result("text", "content", compression, {"lines_sampled": len(lines)}, notes)

    if compression is not None:
        notes.append(f"{compression}-compressed; inner content not recognized")
        return _result("gzip", "magic", compression, {"magic_hex": head[:8].hex()}, notes)
    return _result("unknown", "content", None, {"magic_hex": head[:8].hex()}, notes)


# --- sources: where entries come from -----------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    rel: str  # path relative to the described root, '/'-separated
    size: int


class _Source(Protocol):
    def list(self, limit: int) -> tuple[list[_Entry], bool]:
        """Up to ``limit`` entries (sorted by ``rel``) and whether more existed."""
        ...

    def read_head(self, entry: _Entry, head_bytes: int, small_file_bytes: int) -> bytes: ...

    def uri(self, entry: _Entry) -> str: ...


def _read_local_head(path: Path, size: int, head_bytes: int, small_file_bytes: int) -> bytes:
    want = size if size < small_file_bytes else head_bytes  # small files are read whole (record counts)
    with open(path, "rb") as fh:
        return fh.read(max(want, head_bytes))


class _LocalSource:
    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self, limit: int) -> tuple[list[_Entry], bool]:
        entries: list[_Entry] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                try:
                    if not full.is_file():
                        continue
                    size = full.stat().st_size
                except OSError:
                    continue
                if len(entries) == limit:
                    return sorted(entries, key=lambda e: e.rel), True
                entries.append(_Entry(full.relative_to(self.root).as_posix(), size))
        return sorted(entries, key=lambda e: e.rel), False

    def read_head(self, entry: _Entry, head_bytes: int, small_file_bytes: int) -> bytes:
        return _read_local_head(self.root / entry.rel, entry.size, head_bytes, small_file_bytes)

    def uri(self, entry: _Entry) -> str:
        return str(self.root / entry.rel)


# --- describing ---------------------------------------------------------------------------------


def _describe_entry(
    source: _Source, entry: _Entry, *, head_bytes: int, small_file_bytes: int, swallow: bool
) -> dict[str, Any]:
    name = entry.rel.rsplit("/", 1)[-1]
    base: dict[str, Any] = {
        "path": source.uri(entry), "name": name, "relative_path": entry.rel, "size_bytes": entry.size,
    }
    try:
        head = source.read_head(entry, head_bytes, small_file_bytes) if entry.size else b""
    except Exception as exc:  # noqa: BLE001 — one unreadable sample must not sink the cohort
        if not swallow:
            raise
        base["error"] = f"{type(exc).__name__}: {exc}"
        return base
    try:
        found = sniff(name, head, entry.size, small_file_bytes=small_file_bytes)
    except Exception as exc:  # noqa: BLE001 — a corrupt sampled file must not sink the cohort
        if not swallow:
            raise Sk8sError(f"could not parse header of {base['path']}: {type(exc).__name__}: {exc}") from exc
        base["error"] = f"could not parse header: {type(exc).__name__}: {exc}"
        return base
    base.update(found)
    base["sniffed_bytes"] = len(head)
    return base


_FASTQ_NAME = re.compile(
    r"^(?P<prefix>.+?)(?P<sep>[_.\-])R?(?P<read>[12])(?P<tail>(?:_\d{3})?\.(?:fastq|fq)(?:\.gz)?)$", re.IGNORECASE
)
_LANE_SUFFIX = re.compile(r"(_S\d+)?(_L\d{3})?$")


def _fastq_pattern(entries: list[_Entry]) -> dict[str, Any]:
    fq = [e for e in entries if extension_format(e.rel) == "fastq"]
    groups: dict[tuple[str, str, str], set[str]] = {}
    sample_ids: set[str] = set()
    matched = 0
    for e in fq:
        directory, _, name = e.rel.rpartition("/")
        m = _FASTQ_NAME.match(name)
        if m is None:
            sample_ids.add(re.sub(r"\.(fastq|fq)(\.gz)?$", "", name, flags=re.IGNORECASE))
            continue
        matched += 1
        groups.setdefault((directory, m["prefix"], m["tail"].lower()), set()).add(m["read"])
        sample_ids.add(_LANE_SUFFIX.sub("", m["prefix"]))
    pairs = sum(1 for reads in groups.values() if reads == {"1", "2"})
    paired_files = pairs * 2
    unpaired = matched - paired_files
    unmatched = len(fq) - matched
    if pairs and not unpaired and not unmatched:
        layout = "paired-end"
    elif not pairs:
        layout = "single-end"
    else:
        layout = "mixed"
    return {
        "files": len(fq),
        "paired_end_pairs": pairs,
        "unpaired_files": unpaired,
        "layout": layout,
        "samples_estimate": len(sample_ids),
    }


def _bam_pattern(entries: list[_Entry]) -> dict[str, Any]:
    names = {e.rel for e in entries}
    bams = [e.rel for e in entries if extension_format(e.rel) == "bam" and e.rel.lower().endswith(".bam")]
    indexed = sum(1 for b in bams if b + ".bai" in names or b[:-4] + ".bai" in names or b + ".csi" in names)
    return {"files": len(bams), "with_index": indexed}


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            text = f"{n:.0f}" if unit == "B" else f"{n:.1f}".removesuffix(".0")
            return f"{text} {unit}"
        n /= 1000
    raise AssertionError("unreachable")


def _size_stats(sizes: list[int]) -> dict[str, int] | None:
    if not sizes:
        return None
    return {
        "min": min(sizes), "median": int(statistics.median(sizes)), "mean": round(statistics.fmean(sizes)),
        "max": max(sizes), "total": sum(sizes),
    }


def _headline(formats: dict[str, dict[str, Any]], patterns: dict[str, Any], entries: list[_Entry], capped: bool) -> str:
    if not entries:
        return "No files found"
    # Headline the data, not its sidecars: indexes / unrecognized files only if nothing else exists.
    data_formats = [f for f in formats if f not in _AUXILIARY_FORMATS] or list(formats)
    dominant = max(data_formats, key=lambda f: (formats[f]["count"], -_PRIORITY.index(f) if f in _PRIORITY else -99))
    sizes = [e.size for e in entries if extension_format(e.rel) == dominant]
    stats = _size_stats(sizes)
    assert stats is not None
    med = stats["median"]
    if med and (stats["max"] > 1.5 * med or stats["min"] < med / 1.5):
        size_txt = f"median {_human_bytes(med)}, range {_human_bytes(stats['min'])} to {_human_bytes(stats['max'])}"
    else:
        size_txt = f"~{_human_bytes(med)} each"
    if dominant == "fastq" and "fastq" in patterns:
        p = patterns["fastq"]
        layout = p["layout"] if p["layout"] != "mixed" else "mixed layout"
        text = f"Found {p['samples_estimate']} samples (FASTQ, {layout}, {p['files']} files, {size_txt})"
    else:
        text = f"Found {formats[dominant]['count']} {dominant.upper()} files ({size_txt})"
    others = len(entries) - formats[dominant]["count"]
    if others:
        text += f"; plus {others} other file{'s' if others != 1 else ''}"
    if capped:
        text += " [listing capped]"
    return text


def _describe_cohort(
    source: _Source, root: str, *, samples: int, max_objects: int, head_bytes: int, small_file_bytes: int,
    extra_notes: list[str], must_exist: bool = False,
) -> dict[str, Any]:
    entries, capped = source.list(max_objects)
    if must_exist and not entries:
        raise BlobNotFound(f"nothing at {root} (no such object, and no objects under that prefix)")
    notes = list(extra_notes)
    if capped:
        notes.append(
            f"listing capped at {max_objects} objects: counts and sizes cover only the first {max_objects} "
            f"(sorted by path); there are more"
        )
    by_fmt: dict[str, list[_Entry]] = {}
    for e in entries:
        by_fmt.setdefault(extension_format(e.rel), []).append(e)
    formats: dict[str, dict[str, Any]] = {}
    for fmt in sorted(by_fmt, key=lambda f: (-len(by_fmt[f]), f)):
        formats[fmt] = {
            "count": len(by_fmt[fmt]), "total_bytes": sum(e.size for e in by_fmt[fmt]), "confidence": "extension",
        }
    patterns: dict[str, Any] = {}
    if "fastq" in by_fmt:
        patterns["fastq"] = _fastq_pattern(entries)
    if "bam" in by_fmt:
        patterns["bam"] = _bam_pattern(entries)
    sampled = [
        _describe_entry(source, e, head_bytes=head_bytes, small_file_bytes=small_file_bytes, swallow=True)
        for e in entries[:samples]
    ]
    mismatches = [
        {"relative_path": s["relative_path"], "by_extension": extension_format(s["name"]), "by_content": s["format"]}
        for s in sampled
        if "format" in s and extension_format(s["name"]) in _CHECKED_FORMATS and s["format"] != extension_format(s["name"])
    ]
    return {
        "kind": "cohort",
        "path": root,
        "headline": _headline(formats, patterns, entries, capped),
        "file_count": len(entries),
        "total_bytes": sum(e.size for e in entries),
        "listing_capped": capped,
        "listing_cap": max_objects,
        "formats": formats,
        "size_stats": _size_stats([e.size for e in entries]),
        "patterns": patterns,
        "samples": sampled,
        "sample_format_mismatches": mismatches,
        "notes": notes,
    }


def describe(
    path_or_uri: str | os.PathLike[str],
    *,
    samples: int = SAMPLES,
    max_objects: int = MAX_OBJECTS,
    head_bytes: int = HEAD_BYTES,
    small_file_bytes: int = SMALL_FILE_BYTES,
    client: Any | None = None,
) -> dict[str, Any]:
    """Describe a file, a local directory, or a ``gs://`` / ``s3://`` object or prefix.

    One file -> ``{"kind": "file", "format", "confidence", "compression", "facts", ...}``.
    A directory or prefix -> ``{"kind": "cohort", "headline", "file_count", "formats", "size_stats",
    "patterns", "samples": [per-file descriptions of the first ``samples`` files], ...}``.
    ``client`` injects an SDK-shaped storage client (google-cloud-storage / boto3) for remote paths.
    """
    target = os.fspath(path_or_uri)
    scheme = urlsplit(target).scheme if "://" in target else ""
    kw: dict[str, Any] = {"head_bytes": head_bytes, "small_file_bytes": small_file_bytes}
    if scheme:
        return _describe_remote(target, scheme, samples=samples, max_objects=max_objects, client=client, **kw)
    path = Path(target).expanduser()
    if path.is_file():
        entry = _Entry(path.name, path.stat().st_size)
        out = _describe_entry(_LocalSource(path.parent), entry, swallow=False, **kw)
        out["path"] = str(path)
        return {"kind": "file", **out}
    if path.is_dir():
        return _describe_cohort(
            _LocalSource(path), str(path), samples=samples, max_objects=max_objects, extra_notes=[], **kw
        )
    raise Sk8sError(f"no such file or directory: {target}")


# --- remote sources (gs://, s3://) --------------------------------------------------------------

_GCS_HINT = "no Google Cloud credentials found; run: gcloud auth application-default login (or set GOOGLE_APPLICATION_CREDENTIALS)"
_AWS_HINT = "no AWS credentials found; configure them (aws configure, AWS_PROFILE, or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY)"
_DENIED_CODES = {"AccessDenied", "403", "Forbidden", "AllAccessDisabled"}
_MISSING_CODES = {"NoSuchBucket", "NoSuchKey", "404", "NotFound"}


def _translate(exc: Exception, uri: str, scheme: str) -> Sk8sError:
    """Map an SDK exception (matched by shape, so no SDK import is needed) to a typed sk8s error."""
    if isinstance(exc, Sk8sError):
        return exc
    name = type(exc).__name__
    code = getattr(exc, "code", None)
    response = getattr(exc, "response", None)
    aws_code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    if "Credentials" in name or name == "RefreshError":
        return StoreError(f"{_GCS_HINT if scheme == 'gs' else _AWS_HINT} (reading {uri})")
    if code == 403 or aws_code in _DENIED_CODES:
        return StoreError(f"access denied reading {uri}: {exc}; the credentials need list and read access to the bucket")
    if code == 404 or aws_code in _MISSING_CODES:
        return BlobNotFound(f"nothing at {uri}: {exc}")
    return StoreError(f"cannot read {uri}: {name}: {exc}")


class _RemoteSource:
    """Shared listing/range-read plumbing; subclasses speak one SDK."""

    scheme = ""

    def __init__(self, bucket: str, prefix: str, client: Any) -> None:
        self.bucket, self.prefix, self.client = bucket, prefix, client

    # SDK-specific -----------------------------------------------------------------------------
    def _iter_objects(self, prefix: str, first_page: int | None = None) -> Any:
        """Lazily yield (key, size) for keys under ``prefix`` in key order; ``first_page`` sizes the first request."""
        raise NotImplementedError

    def _read_range(self, key: str, length: int) -> bytes:
        raise NotImplementedError

    # common -----------------------------------------------------------------------------------
    def uri_for_key(self, key: str) -> str:
        return f"{self.scheme}://{self.bucket}/{key}"

    def uri(self, entry: _Entry) -> str:
        return self.uri_for_key(self.prefix + entry.rel)

    def probe(self, key: str) -> int | None:
        """Size of the object at exactly ``key``, or None. Key order puts an exact match first."""
        try:
            for k, size in self._iter_objects(key, first_page=1):
                return size if k == key else None
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, self.uri_for_key(key), self.scheme) from exc
        return None

    def list(self, limit: int) -> tuple[list[_Entry], bool]:
        entries: list[_Entry] = []
        try:
            for key, size in self._iter_objects(self.prefix):
                if key.endswith("/"):  # console-style folder placeholder
                    continue
                if len(entries) == limit:
                    return sorted(entries, key=lambda e: e.rel), True
                entries.append(_Entry(key[len(self.prefix):], size))
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, self.uri_for_key(self.prefix), self.scheme) from exc
        return sorted(entries, key=lambda e: e.rel), False

    def read_head(self, entry: _Entry, head_bytes: int, small_file_bytes: int) -> bytes:
        try:
            return self._read_range(self.prefix + entry.rel, min(head_bytes, entry.size))
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, self.uri(entry), self.scheme) from exc


class _GCSSource(_RemoteSource):
    scheme = "gs"

    def _iter_objects(self, prefix: str, first_page: int | None = None) -> Any:
        extra = {"max_results": first_page} if first_page else {}
        for blob in self.client.list_blobs(self.bucket, prefix=prefix, **extra):
            yield blob.name, int(blob.size or 0)

    def _read_range(self, key: str, length: int) -> bytes:
        data: bytes = self.client.bucket(self.bucket).blob(key).download_as_bytes(start=0, end=length - 1)  # end inclusive
        return data


class _S3Source(_RemoteSource):
    scheme = "s3"

    def _iter_objects(self, prefix: str, first_page: int | None = None) -> Any:
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if first_page:
                kwargs["MaxKeys"] = first_page
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                yield obj["Key"], int(obj.get("Size", 0))
            if not resp.get("IsTruncated"):
                return
            token = resp["NextContinuationToken"]

    def _read_range(self, key: str, length: int) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}")
        data: bytes = resp["Body"].read()
        return data


def _make_client(scheme: str) -> Any:
    """The SDK client for ``scheme`` (lazy import: the extras stay optional)."""
    if scheme == "gs":
        try:
            storage = importlib.import_module("google.cloud.storage")
        except ImportError as exc:
            raise Sk8sError(
                'describing gs:// paths needs google-cloud-storage; install with: pip install "sk8s[gcs]"'
            ) from exc
        try:
            return storage.Client()
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, "gs://", "gs") from exc
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:
        raise Sk8sError('describing s3:// paths needs boto3; install with: pip install "sk8s[s3]"') from exc
    endpoint = os.environ.get(ENDPOINT_ENV)
    return boto3.client("s3", **({"endpoint_url": endpoint} if endpoint else {}))


def _describe_remote(
    target: str, scheme: str, *, samples: int, max_objects: int, client: Any | None, head_bytes: int,
    small_file_bytes: int,
) -> dict[str, Any]:
    if scheme not in ("gs", "s3"):
        raise Sk8sError(f"unsupported scheme {scheme!r} in {target!r}: describe takes a local path, gs://, or s3://")
    parts = urlsplit(target)
    if not parts.netloc:
        raise Sk8sError(f"{target!r} has no bucket: expected {scheme}://BUCKET/PATH")
    bucket, key = parts.netloc, parts.path.lstrip("/")
    cls = _GCSSource if scheme == "gs" else _S3Source
    sdk = client if client is not None else _make_client(scheme)
    kw: dict[str, Any] = {"head_bytes": head_bytes, "small_file_bytes": small_file_bytes}
    if key and not key.endswith("/"):
        size = cls(bucket, "", sdk).probe(key)
        if size is not None:
            folder, _, name = key.rpartition("/")
            src = cls(bucket, folder + "/" if folder else "", sdk)
            out = _describe_entry(src, _Entry(name, size), swallow=False, **kw)
            return {"kind": "file", **out}
    prefix = key.rstrip("/") + "/" if key.strip("/") else ""
    src = cls(bucket, prefix, sdk)
    return _describe_cohort(
        src, src.uri_for_key(prefix), samples=samples, max_objects=max_objects, extra_notes=[],
        must_exist=True, **kw,
    )


# --- human output -------------------------------------------------------------------------------


def _fmt_value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= _HUMAN_VALUE_MAX else text[:_HUMAN_VALUE_MAX] + f"... [{len(text)} chars; use --json for all]"


def _file_lines(d: dict[str, Any], indent: str) -> list[str]:
    lines = []
    if "error" in d:
        return [f"{indent}error: {d['error']}"]
    comp = f", {d['compression']}-compressed" if d.get("compression") else ""
    lines.append(f"{indent}format: {d['format']} (confidence: {d['confidence']}{comp})")
    for key, value in d.get("facts", {}).items():
        lines.append(f"{indent}{key}: {_fmt_value(value)}")
    for note in d.get("notes", []):
        lines.append(f"{indent}note: {note}")
    return lines


def _summary(d: dict[str, Any]) -> str:
    """One line of the most telling facts, for cohort listings."""
    if "error" in d:
        return f"error: {d['error']}"
    f, fmt = d["facts"], d["format"]
    if fmt == "fastq":
        parts = [f"read length {f['read_length']}", f"phred+{f['phred_offset_guess']}", f"records: {f['records']}"]
    elif fmt == "bam":
        aligner = (f["aligner"] or {}).get("name")
        parts = [f"sort {f['sort_order'] or '?'}", f"{f['reference_count']} refs", f"{f['read_group_count']} read groups"]
        if aligner:
            parts.append(f"aligner {aligner}")
    elif fmt == "cram":
        parts = [f"version {f['version']}"]
    elif fmt == "vcf":
        parts = [f["fileformat"], f"{f.get('sample_count', '?')} samples", f"{f['contig_count']} contigs"]
    elif fmt in ("csv", "tsv", "delimited"):
        parts = [f"{f['column_count']} columns", "header" if f["has_header"] else "no header"]
    elif fmt == "fasta":
        parts = [f"{f['contig_count']} contigs", f"{f['sequence_type']}"]
    elif fmt == FASTA_INDEX_FORMAT:
        parts = [f"{f['contig_count']} contigs"]
    elif fmt == BAM_INDEX_FORMAT:
        parts = [f"{f['reference_count']} refs"] if "reference_count" in f else []
    else:
        parts = []
    comp = f"{d['compression']}, " if d.get("compression") else ""
    return f"{fmt} ({d['confidence']}; {comp}{', '.join(parts)})" if parts else f"{fmt} ({d['confidence']})"


def format_description(d: dict[str, Any]) -> str:
    """Human-readable rendering of a ``describe`` result."""
    out: list[str] = []
    if d["kind"] == "file":
        out.append(f"{d['path']}  ({_human_bytes(d['size_bytes'])}, {d['size_bytes']} bytes)")
        out.extend(_file_lines(d, "  "))
        return "\n".join(out)
    out.append(d["headline"])
    out.append(f"{d['path']}")
    s = d["size_stats"]
    out.append(f"  files: {d['file_count']}   total: {_human_bytes(d['total_bytes'])}")
    if s:
        out.append(
            f"  sizes: min {_human_bytes(s['min'])}, median {_human_bytes(s['median'])}, "
            f"mean {_human_bytes(s['mean'])}, max {_human_bytes(s['max'])}"
        )
    if d["formats"]:
        parts = ", ".join(f"{f} x{v['count']}" for f, v in d["formats"].items())
        out.append(f"  formats (by extension): {parts}")
    for name, p in d["patterns"].items():
        out.append(f"  pattern {name}: " + ", ".join(f"{k}={v}" for k, v in p.items()))
    for note in d["notes"]:
        out.append(f"  note: {note}")
    if d["samples"]:
        out.append(f"  first {len(d['samples'])} files (sniffed from content):")
        for sample in d["samples"]:
            out.append(f"    {sample['relative_path']}  ({_human_bytes(sample['size_bytes'])})")
            out.append(f"      {_summary(sample)}")
    for m in d["sample_format_mismatches"]:
        out.append(f"  warning: {m['relative_path']} is named {m['by_extension']} but looks like {m['by_content']}")
    return "\n".join(out)


__all__ = ["describe", "format_description", "sniff", "extension_format"]
