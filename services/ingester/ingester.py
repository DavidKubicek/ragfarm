"""
ingester — walk CORPUS_PATH, route by file type, chunk per docs/ingestion-pipeline.md,
embed (dense + sparse) via the step-03 embedder, upsert into Qdrant with named
vectors for hybrid retrieval.

Reference implementation against docs/ingestion-pipeline.md. CPU embedder (BGE-M3),
NOT the NPU. See ADR-0002.

Routing:
    .xlsx/.xls/.csv          -> table path (row-per-chunk, multi-table aware)
    .docx/.txt/.md/.markdown -> prose path (semantic chunks w/ overlap)
    .pdf                     -> prose path (text-layer extraction; scanned PDFs
                                with no text layer are skipped with a warning)
    else                     -> skip with warning
Vectors:    named 'dense' (1024, cosine) + named sparse 'sparse'
Idempotent: point ID = hash(source_file + sheet|heading + index); re-ingest overwrites.

XLSX is NOT clean tables. The table path handles, all observed in real infra specs:
  - summary/legend blocks ABOVE the real header (scored header detection)
  - a sheet whose used range starts below row 1
  - MULTIPLE stacked tables per sheet, each with its own header (often different
    width) — split on a repeated header-shaped row
  - blank separator rows between groups (all-blank across the table span)
  - duplicate column names disambiguated by a merged super-header band
  - three grouping encodings, all meaning "this label applies to a run of rows":
      (1) dense repetition  — label on every row; nothing to do
      (2) sparse carry-forward — label once, blank below; carried by a state
          machine (arm on real value, carry across non-blank rows, DISARM on an
          all-blank separator row)
      (3) vertical merged range — label once in a merged cell spanning the rows;
          propagated deterministically from merge metadata (rotation is display-
          only). Common. Never combined with separators in the same region.
    Grouping columns live in the left half (first ceil(width/2) cols).
  - numbers stored as float (phone 603423146.0, VLAN 606.0) -> int cleanup
"""
import os
import re
import sys
import math
import csv
import uuid
import logging
import pathlib
from typing import Iterator, Optional

import requests
from qdrant_client import QdrantClient
from qdrant_client import models as qm

# --- config -----------------------------------------------------------------
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED      = os.environ.get("EMBED_ENDPOINT", "http://localhost:8090/embed")
ROOT       = pathlib.Path(os.environ.get("CORPUS_PATH", "/srv/corpus"))
COLL       = os.environ.get("QDRANT_COLLECTION", "corpus")
DENSE_DIM  = 1024
BATCH      = 64

HEADER_SCAN_ROWS = 25
MAX_TABLES_PER_SHEET = 50

CHUNK_MIN_TOK = 256
CHUNK_MAX_TOK = 384
OVERLAP_FRAC  = 0.15

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("ingester")

try:
    from docx import Document
except ImportError:
    Document = None
try:
    import openpyxl
    from openpyxl import load_workbook
    from openpyxl.utils import range_boundaries
except ImportError:
    openpyxl = None
try:
    import pdfplumber
except ImportError:
    pdfplumber = None
try:
    from langdetect import detect as _detect
except ImportError:
    _detect = None


# --- embedding --------------------------------------------------------------
def embed(texts: list[str], kind: str = "passage") -> tuple[list[list[float]], list[dict]]:
    r = requests.post(EMBED, json={"input": texts, "kind": kind}, timeout=300)
    r.raise_for_status()
    data = r.json()
    dense = data["dense"]
    sparse = data.get("sparse", [{} for _ in texts])
    if len(dense) != len(texts) or len(sparse) != len(texts):
        raise RuntimeError(f"embedder returned {len(dense)}/{len(sparse)} for {len(texts)} inputs")
    return dense, sparse


def to_sparse_vector(sparse_map: dict) -> qm.SparseVector:
    if not sparse_map:
        return qm.SparseVector(indices=[], values=[])
    idx, val = [], []
    for k, v in sparse_map.items():
        idx.append(int(k)); val.append(float(v))
    return qm.SparseVector(indices=idx, values=val)


def detect_lang(text: str) -> str:
    if _detect is None:
        return "unknown"
    try:
        code = _detect(text)
    except Exception:
        return "unknown"
    return code if code in ("cs", "en") else "other"


def point_id(*parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join(str(p) for p in parts)))


def clean_cell(v) -> str:
    """Stringify verbatim, fixing openpyxl's int-as-float (603423146.0 -> 603423146,
    VLAN 606.0 -> 606). Identifiers must be verbatim for sparse exact-match."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# ============================================================================
# TABLE PATH
# ============================================================================
_NUM_RE = re.compile(r"-?\d+(\.\d+)?$")


def _is_numeric(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return True
    return bool(_NUM_RE.match(str(v).strip().replace(",", "")))


def _fill_rgb(styled_cell) -> Optional[str]:
    f = styled_cell.fill
    if f and f.patternType:
        rgb = f.fgColor.rgb
        return rgb if isinstance(rgb, str) else None
    return None


def _row_nonempty_cols(wsv, ri, cmax) -> list:
    return [ci for ci in range(1, cmax + 1)
            if (v := wsv.cell(row=ri, column=ci).value) is not None and str(v).strip() != ""]


def _detect_header_row(wsv, wss, start_ri, cmax, scan=HEADER_SCAN_ROWS) -> int:
    """Scored header detection from start_ri downward. A header is recognized by
    being more DISTINCT than its neighbours, not by uniform in-line formatting
    (real headers carry random bold/font cells — that is noise). Signals:
      + textual (non-numeric) and short labels  — headers are labels
      + distinctness vs the FOLLOWING row (fill/bold differs => header/data edge)
      + distinctness vs the PREVIOUS row         — same, catches directly-stacked
        tables with no blank separator between them
      + spans the data width                     — rejects narrow summary rows
    Intra-row formatting uniformity is deliberately NOT required."""
    rmax = wsv.max_row or start_ri
    hi = min(start_ri + scan, rmax)
    dens = {ri: len(_row_nonempty_cols(wsv, ri, cmax)) for ri in range(start_ri, min(hi + 5, rmax) + 1)}
    maxdens = max(dens.values()) if dens else 0
    best_ri, best = start_ri, -1.0
    for ri in range(start_ri, hi + 1):
        cols = _row_nonempty_cols(wsv, ri, cmax)
        if not cols:
            continue
        n = len(cols)
        vals = [wsv.cell(row=ri, column=ci).value for ci in cols]
        nbold = sum(1 for ci in cols if wss.cell(row=ri, column=ci).font.bold)
        nstr = sum(1 for v in vals if not _is_numeric(v))
        nshort = sum(1 for v in vals if len(str(v).strip()) <= 40)
        # distinctness vs following row: fill OR bold differs per column
        def _distinct(other_ri):
            d = 0
            for ci in cols:
                a = wss.cell(row=ri, column=ci)
                b = wss.cell(row=other_ri, column=ci)
                if _fill_rgb(a) != _fill_rgb(b) or bool(a.font.bold) != bool(b.font.bold):
                    d += 1
            return d / n
        dist_below = _distinct(ri + 1) if ri + 1 <= rmax else 1.0
        dist_above = _distinct(ri - 1) if ri - 1 >= 1 else 1.0
        dmatch = maxdens > 0 and n >= 0.6 * maxdens
        score = (2 * (nstr / n) + (nshort / n)
                 + 1.5 * dist_below + 0.8 * dist_above
                 + 0.5 * (nbold / n) + (2 if dmatch else 0))
        if score > best:
            best_ri, best = ri, score
    return best_ri


def _header_span(wsv, header_ri, cmax) -> tuple:
    """Return (col_start, col_end) of the header's populated span. COL_START is the
    FIRST non-empty header cell — tables do NOT necessarily begin at column 1;
    people often start at col 2-3 to keep left-expansion room (SMAX starts at
    col 2). Leading blanks are skipped regardless of their formatting."""
    cols = _row_nonempty_cols(wsv, header_ri, cmax)
    return (min(cols), max(cols)) if cols else (1, 1)


def _is_all_blank(wsv, ri, c1, c2) -> bool:
    """Separator test: every cell empty across the FULL table span [c1,c2]."""
    return all(
        (v := wsv.cell(row=ri, column=ci).value) is None or str(v).strip() == ""
        for ci in range(c1, c2 + 1)
    )


def _is_new_table_header(wsv, wss, ri, c1, c2) -> bool:
    """Boundary trigger for stacked tables: a row that is header-SHAPED —
    formatting (bold/fill) AND text-or-blank only (no numbers) AND text>blank.
    Detects a sub-table's own header regardless of whether its labels match the
    first table's (old DC's second table is narrower with different columns)."""
    cells = [wsv.cell(row=ri, column=ci).value for ci in range(c1, c2 + 1)]
    ntext = sum(1 for v in cells if v is not None and str(v).strip() != "" and not _is_numeric(v))
    nnum = sum(1 for v in cells if v is not None and str(v).strip() != "" and _is_numeric(v))
    nblank = sum(1 for v in cells if v is None or str(v).strip() == "")
    if nnum > 0:
        return False
    if ntext <= nblank:
        return False
    nbold = sum(1 for ci in range(c1, c2 + 1) if wss.cell(row=ri, column=ci).font.bold)
    nfill = sum(1 for ci in range(c1, c2 + 1) if _fill_rgb(wss.cell(row=ri, column=ci)) is not None)
    return (nbold >= ntext * 0.5) or (nfill >= ntext * 0.5)


def _merged_bands(wss, header_ri, c1, c2) -> dict:
    """col -> super-header label from a horizontal merge directly ABOVE the header
    (disambiguates duplicate 'Hostname'/'IP address' host-vs-VM trios)."""
    bands = {}
    for mr in wss.merged_cells.ranges:
        a, r1, b, r2 = range_boundaries(str(mr))
        if r1 == r2 == header_ri - 1 and b > a:
            label = wss.cell(row=r1, column=a).value
            if label and str(label).strip():
                for ci in range(a, b + 1):
                    bands[ci] = str(label).strip()
    return bands


def _build_header(wsv, wss, header_ri, c1, c2) -> dict:
    bands = _merged_bands(wss, header_ri, c1, c2)
    keys, seen = {}, {}
    for ci in range(c1, c2 + 1):
        v = wsv.cell(row=header_ri, column=ci).value
        if v is None or str(v).strip() == "":
            continue
        k = str(v).strip()
        if ci in bands:
            k = f"{bands[ci]} {k}"
        if k in seen:
            seen[k] += 1; k = f"{k} #{seen[k]}"
        else:
            seen[k] = 1
        keys[ci] = k
    return keys


def _vmerge_map(wss, c1, c2, row_lo, row_hi) -> dict:
    """col -> {row: value} for VERTICAL merged ranges, CLAMPED to [row_lo,row_hi]
    so a merge in one sub-table never leaks into the next. Encoding (3)."""
    m = {}
    for mr in wss.merged_cells.ranges:
        a, r1, b, r2 = range_boundaries(str(mr))
        if a == b and r2 > r1 and c1 <= a <= c2:
            lo, hi = max(r1, row_lo), min(r2, row_hi)
            if lo > hi:
                continue
            val = wss.cell(row=r1, column=a).value
            if val is None or str(val).strip() == "":
                continue
            m.setdefault(a, {})
            for ri in range(lo, hi + 1):
                m[a][ri] = val
    return m


def _cell_style(c) -> tuple:
    """Minimal style fingerprint for the col[1]-vs-col[2] look-aside: bold,
    italic, font color, fill. Any difference distinguishes a group-label column."""
    try:
        color = c.font.color.rgb if (c.font.color and isinstance(c.font.color.rgb, str)) else None
    except Exception:
        color = None
    fill = None
    if c.fill and c.fill.patternType and isinstance(c.fill.fgColor.rgb, str):
        fill = c.fill.fgColor.rgb
    return (bool(c.font.bold), bool(c.font.italic), color, fill)


def _ff_candidate_cols(wsv, wss, keys, header_ri, end) -> set:
    """Carry-forward is restricted to the FIRST column only, and only when that
    column is a VISUALLY DISTINGUISHED group-label column — its formatting differs
    from the second column. Rationale: grouping-by-omission is a leftmost-column
    idiom and a minority feature; letting it touch any other column fabricates
    data (e.g. smearing a one-off 'Application Name' down following rows). The
    formatting gate (one cell look-aside, col1 vs col2) confirms col1 is a label
    column, not just the first data column. If col1 looks like col2, carry-forward
    is OFF for this table — no fabrication possible.

    The style check is done on the first DATA row where col1 is populated (that is
    where the group-label styling lives — bold/colored label vs plain data),
    NOT the header row (where all cells share header styling)."""
    cols = sorted(keys)
    if len(cols) < 2:
        return set()
    col1, col2 = cols[0], cols[1]
    probe = None
    for rr in range(header_ri + 1, end + 1):
        v = wsv.cell(row=rr, column=col1).value
        if v is not None and str(v).strip() != "":
            probe = rr
            break
    if probe is None:
        return set()                               # col1 never populated -> nothing to carry
    if _cell_style(wss.cell(row=probe, column=col1)) != _cell_style(wss.cell(row=probe, column=col2)):
        return {col1}
    return set()


def _serialize_kv(keys: dict, values: dict, sheet: Optional[str]) -> Optional[str]:
    parts = []
    if sheet:
        parts.append(f"sheet: {sheet}")
    for ci, k in keys.items():
        val = clean_cell(values.get(ci))
        if val == "":
            continue
        parts.append(f"{k}: {val}")
    return ", ".join(parts) if len(parts) > (1 if sheet else 0) else None


def _iter_sheet_tables(wsv, wss, sheet, cmax, source_file) -> Iterator[dict]:
    """Walk one sheet, splitting into stacked sub-tables, recovering grouping per
    table. Yields serialized row records."""
    rmax = wsv.max_row or 1
    ri = wsv.min_row or 1
    tnum = 0
    guard = 0
    while ri <= rmax and guard < MAX_TABLES_PER_SHEET:
        guard += 1
        header_ri = _detect_header_row(wsv, wss, ri, cmax)
        c1, c2 = _header_span(wsv, header_ri, cmax)
        keys = _build_header(wsv, wss, header_ri, c1, c2)
        if not keys:
            break
        end = rmax
        scan = header_ri + 1
        while scan <= rmax:
            if _is_new_table_header(wsv, wss, scan, c1, c2):
                end = scan - 1
                break
            scan += 1
        ff_cols = _ff_candidate_cols(wsv, wss, keys, header_ri, end)
        vm = _vmerge_map(wss, c1, c2, header_ri + 1, end)
        log.info("%s[%s] table#%d: header R%d span=%d..%d cols=%d ff=%d vmerge=%s rows<=%d",
                 source_file, sheet, tnum, header_ri, c1, c2, len(keys), len(ff_cols),
                 sorted(vm.keys()), end - header_ri)

        carry = {}
        ridx = 0
        for rr in range(header_ri + 1, end + 1):
            if _is_all_blank(wsv, rr, c1, c2):
                carry = {}
                continue
            ridx += 1
            values = {ci: wsv.cell(row=rr, column=ci).value for ci in keys}
            for ci in keys:
                if ci in vm and rr in vm[ci]:
                    cur = values.get(ci)
                    if cur is None or str(cur).strip() == "":
                        values[ci] = vm[ci][rr]
            for ci in ff_cols:
                v = values.get(ci)
                if v is not None and str(v).strip() != "":
                    carry[ci] = v
                elif ci in carry:
                    values[ci] = carry[ci]
            text = _serialize_kv(keys, values, sheet)
            if not text:
                continue
            yield {
                "id": point_id(source_file, sheet, tnum, ridx),
                "text": text,
                "payload": {
                    "source_file": source_file, "sheet": sheet, "table": tnum,
                    "row_index": ridx, "header_row": header_ri, "kind": "table_row",
                    "lang": "n/a", "text": text,
                },
            }
        tnum += 1
        ri = end + 1
        while ri <= rmax and _is_all_blank(wsv, ri, 1, cmax):
            ri += 1


def iter_xlsx(path: pathlib.Path) -> Iterator[dict]:
    if openpyxl is None:
        log.error("openpyxl not installed; cannot read %s", path)
        return
    wbv = openpyxl.load_workbook(path, data_only=True)
    wbs = load_workbook(path)
    for sheet in wbv.sheetnames:
        wsv = wbv[sheet]; wss = wbs[sheet]
        cmax = wsv.max_column or 1
        if (wsv.max_row or 0) < 1 or cmax < 1:
            continue
        yield from _iter_sheet_tables(wsv, wss, sheet, cmax, path.name)


def iter_csv(path: pathlib.Path) -> Iterator[dict]:
    with path.open(newline="", encoding="utf-8", errors="ignore") as fh:
        reader = csv.reader(fh)
        rows = iter(reader)
        header = None
        for r in rows:
            if r and any(str(c).strip() for c in r):
                header = [str(c).strip() for c in r]
                break
        if not header:
            log.warning("%s: no header row, skipping", path.name)
            return
        keys = {i: h for i, h in enumerate(header) if h}
        ridx = 0
        for r in rows:
            ridx += 1
            values = {i: (r[i] if i < len(r) else None) for i in keys}
            text = _serialize_kv(keys, values, None)
            if not text:
                continue
            yield {
                "id": point_id(path.name, "csv", ridx),
                "text": text,
                "payload": {
                    "source_file": path.name, "sheet": "csv", "row_index": ridx,
                    "kind": "table_row", "lang": "n/a", "text": text,
                },
            }


# ============================================================================
# PROSE PATH
# ============================================================================
def _approx_tokens(s: str) -> int:
    return len(s.split())


def _chunk_paragraphs(paras: list[str]) -> Iterator[str]:
    """~256-384 token chunks, ~15% overlap, on paragraph boundaries."""
    buf, buf_tok = [], 0
    for p in paras:
        pt = _approx_tokens(p)
        if buf and buf_tok + pt > CHUNK_MAX_TOK:
            yield "\n".join(buf)
            keep, keep_tok = [], 0
            for q in reversed(buf):
                qt = _approx_tokens(q)
                if keep_tok + qt > OVERLAP_FRAC * CHUNK_MAX_TOK:
                    break
                keep.insert(0, q); keep_tok += qt
            buf, buf_tok = keep, keep_tok
        buf.append(p); buf_tok += pt
        if buf_tok >= CHUNK_MAX_TOK:
            yield "\n".join(buf); buf, buf_tok = [], 0
    if buf:
        yield "\n".join(buf)


def _emit_sections(path: pathlib.Path, sections: list) -> Iterator[dict]:
    """sections: list of (heading, [paragraphs]). Chunk each, emit doc_text."""
    cidx = 0
    for heading, paras in sections:
        for chunk in _chunk_paragraphs(paras):
            chunk = chunk.strip()
            if not chunk:
                continue
            yield {
                "id": point_id(path.name, heading or "_", cidx),
                "text": chunk,
                "payload": {
                    "source_file": path.name, "heading": heading,
                    "chunk_index": cidx, "kind": "doc_text",
                    "lang": detect_lang(chunk), "text": chunk,
                },
            }
            cidx += 1


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _sections_from_lines(lines: list, markdown: bool) -> list:
    """Split plain/markdown text into (heading, paragraphs). For markdown, '#'
    lines start sections; plain txt has no headings (paragraphs split on blanks)."""
    sections, heading, paras, buf = [], "", [], []

    def flush_para():
        if buf:
            paras.append(" ".join(buf)); buf.clear()

    for ln in lines:
        s = ln.rstrip("\n")
        m = _MD_HEADING.match(s) if markdown else None
        if m:
            flush_para()
            if paras or heading:
                sections.append((heading, paras)); paras = []
            heading = m.group(2).strip()
        elif s.strip() == "":
            flush_para()
        else:
            buf.append(s.strip())
    flush_para()
    if paras or heading:
        sections.append((heading, paras))
    return sections or [("", [])]


def iter_text(path: pathlib.Path, markdown: bool) -> Iterator[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    sections = _sections_from_lines(raw.splitlines(), markdown)
    yield from _emit_sections(path, sections)


def iter_pdf(path: pathlib.Path) -> Iterator[dict]:
    """Text-layer extraction (pdfplumber, layout-aware). A scanned PDF with no
    text layer yields nothing -> skipped with a warning rather than silently
    ingesting empty content. OCR is deliberately out of scope for batch ingest."""
    if pdfplumber is None:
        log.error("pdfplumber not installed; cannot read %s", path)
        return
    page_texts = []
    with pdfplumber.open(str(path)) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text() or ""
            if t.strip():
                page_texts.append(t)
    if not page_texts:
        log.warning("%s: no extractable text layer (scanned?) — skipping", path.name)
        return
    lines = "\n\n".join(page_texts).splitlines()
    sections = _sections_from_lines(lines, markdown=False)
    yield from _emit_sections(path, sections)


def iter_docx(path: pathlib.Path) -> Iterator[dict]:
    if Document is None:
        log.error("python-docx not installed; cannot read %s", path)
        return
    doc = Document(str(path))

    for ti, table in enumerate(doc.tables):
        rows = table.rows
        if not rows:
            continue
        keys = {i: c.text.strip() for i, c in enumerate(rows[0].cells) if c.text.strip()}
        for ridx, row in enumerate(rows[1:], start=1):
            values = {i: row.cells[i].text for i in keys if i < len(row.cells)}
            text = _serialize_kv(keys, values, f"table{ti}")
            if not text:
                continue
            yield {
                "id": point_id(path.name, f"table{ti}", ridx),
                "text": text,
                "payload": {
                    "source_file": path.name, "sheet": f"table{ti}", "row_index": ridx,
                    "kind": "table_row", "lang": "n/a", "text": text,
                },
            }

    sections, heading, paras = [], "", []
    for p in doc.paragraphs:
        txt = p.text.strip()
        if not txt:
            continue
        if p.style and p.style.name and p.style.name.lower().startswith("heading"):
            if paras or heading:
                sections.append((heading, paras)); paras = []
            heading = txt
        else:
            paras.append(txt)
    if paras or heading:
        sections.append((heading, paras))
    yield from _emit_sections(path, sections)


# --- routing ----------------------------------------------------------------
def iter_file(path: pathlib.Path) -> Iterator[dict]:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        yield from iter_xlsx(path)
    elif ext == ".csv":
        yield from iter_csv(path)
    elif ext == ".docx":
        yield from iter_docx(path)
    elif ext == ".txt":
        yield from iter_text(path, markdown=False)
    elif ext in (".md", ".markdown"):
        yield from iter_text(path, markdown=True)
    elif ext == ".pdf":
        yield from iter_pdf(path)
    else:
        log.warning("skipping unsupported file: %s", path.name)


# ============================================================================
# COLLECTION + MAIN
# ============================================================================
def ensure_collection(qc: QdrantClient, recreate: bool):
    exists = qc.collection_exists(COLL)
    if exists and recreate:
        qc.delete_collection(COLL); exists = False
    if not exists:
        qc.create_collection(
            COLL,
            vectors_config={"dense": qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE)},
            sparse_vectors_config={"sparse": qm.SparseVectorParams()},
        )
        log.info("created collection %s (dense %d + sparse)", COLL, DENSE_DIM)


def flush_batch(qc: QdrantClient, batch: list):
    if not batch:
        return
    dense, sparse = embed([b["text"] for b in batch], kind="passage")
    points = []
    for b, dv, sv in zip(batch, dense, sparse):
        points.append(qm.PointStruct(
            id=b["id"],
            vector={"dense": dv, "sparse": to_sparse_vector(sv)},
            payload=b["payload"],
        ))
    qc.upsert(COLL, points)


def main():
    recreate = "--recreate" in sys.argv
    if "--corpus" in sys.argv:
        global ROOT
        ROOT = pathlib.Path(sys.argv[sys.argv.index("--corpus") + 1])

    if not ROOT.exists():
        log.error("CORPUS_PATH does not exist: %s", ROOT)
        sys.exit(1)

    qc = QdrantClient(url=QDRANT_URL)
    ensure_collection(qc, recreate)

    batch, total = [], 0
    for f in sorted(ROOT.rglob("*")):
        if not f.is_file():
            continue
        for rec in iter_file(f):
            batch.append(rec)
            if len(batch) >= BATCH:
                flush_batch(qc, batch); total += len(batch); batch = []
    if batch:
        flush_batch(qc, batch); total += len(batch)

    log.info("ingested %d chunks into %s", total, COLL)


if __name__ == "__main__":
    main()
