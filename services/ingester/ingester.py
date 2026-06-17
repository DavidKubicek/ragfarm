"""
ingester — walk CORPUS_PATH, route by file type, chunk per docs/ingestion-pipeline.md,
embed (dense + sparse) via the step-03 embedder, upsert into Qdrant with named
vectors for hybrid retrieval.

Reference implementation against docs/ingestion-pipeline.md. CPU embedder (BGE-M3),
NOT the NPU. See ADR-0002.

Routing:
    .xlsx/.xls               -> table path (services/ingester/xlsx_tables.py)
    .csv                     -> table path (single-header CSV)
    .docx/.txt/.md/.markdown -> prose path (semantic chunks w/ overlap)
    .pdf                     -> prose path (text-layer extraction; scanned PDFs
                                with no text layer are skipped with a warning)
    else                     -> skip with warning
Vectors:    named 'dense' (1024, cosine) + named sparse 'sparse'
Idempotent: point ID = hash(source_file + sheet|heading + index); re-ingest overwrites.

ALL messy-XLSX structural handling (multi-table sheets, stacked headers, multi-row
title+band headers, carry-forward and vertical-merge grouping, headerless data,
trailing totals/notes trimming) now lives in xlsx_tables.py. This file keeps prose
routing, embedding, and Qdrant upsert.
"""
import os
import re
import sys
import csv
import logging
import pathlib
from typing import Iterator

import requests
from qdrant_client import QdrantClient
from qdrant_client import models as qm

from xlsx_tables import iter_xlsx, point_id, clean_cell, _serialize_kv

# --- config -----------------------------------------------------------------
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED      = os.environ.get("EMBED_ENDPOINT", "http://localhost:8090/embed")
ROOT       = pathlib.Path(os.environ.get("CORPUS_PATH", "/srv/corpus"))
COLL       = os.environ.get("QDRANT_COLLECTION", "corpus")
DENSE_DIM  = 1024
BATCH      = 64

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


# ============================================================================
# TABLE PATH (CSV stays here; XLSX delegated to xlsx_tables.iter_xlsx)
# ============================================================================
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
