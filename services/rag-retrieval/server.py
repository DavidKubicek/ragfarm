"""
rag-retrieval — MCP server exposing `search_corpus` over the ingested Qdrant
corpus (step 04). Hybrid retrieval: embed the query via the step-03 embedder
(:8090/embed, kind=query), then run Qdrant's Query API with TWO prefetch
branches — dense (semantic) and sparse (exact host/IP/VLAN token match) — fused
by Reciprocal Rank Fusion (RRF). This one tool serves both Czech/English
semantic search and verbatim identifier lookups, which is why ADR-0003 keeps RAG
in the MCP layer (Option B) instead of Open WebUI's generic document RAG.

Retrieval pipeline (broad-in, narrow-out — the fix for "one dense irrelevant slab
outscores the precise chunk"):
  1. hybrid RRF over a BROAD candidate pool (RAG_CANDIDATES, default 40);
  2. CROSS-ENCODER re-rank (bge-reranker-v2-m3, RAG_USE_RERANKER=1) — each
     (query, chunk) pair is scored directly, so genuine relevance floats to the
     top and topically-different noise sinks, with NO diversity penalty. Legacy
     MMR (RAG_MMR_LAMBDA) is retained behind RAG_USE_RERANKER=0 for A/B only; see
     ADR-0008 for why MMR mis-fires on the small row-per-record chunks (it reads a
     list of near-identical contact rows as "redundant" and evicts the answers);
  3. drop anything below RAG_MIN_SCORE (reranker path only; 0.0 = keep all until
     the floor is calibrated on real dumps), then return the top k (default 8),
     each widened to a bounded SAME-SECTION window (RAG_EXPAND_NEIGHBORS chunks
     each side, capped) so a split section is reunited without dragging in
     unrelated sections;
  4. verbatim payload text (`text` == ingester's text_raw) is what's returned to
     the model — never the embedding-only text_clean — plus section/subsection and
     source line-span metadata for exact citation.

The returned `score` matches the ordering: the reranker's normalized (sigmoid)
relevance on the reranker path, the fused RRF score on the legacy MMR path.

Transport: streamable HTTP (MCP), same pattern as the other services, so mcpo /
the agent layer registers it over HTTP.

Config (env): QDRANT_URL, EMBED_ENDPOINT, RERANK_ENDPOINT, QDRANT_COLLECTION,
RAG_PORT, RAG_PREFETCH, RAG_CANDIDATES, RAG_USE_RERANKER, RAG_MIN_SCORE,
RAG_MMR_LAMBDA, RAG_EXPAND_NEIGHBORS, RAG_EXPAND_MAX_WORDS.
"""
from __future__ import annotations

import os
import re
import time
import math
import logging

import requests
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from mcp.server.fastmcp import FastMCP

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED      = os.environ.get("EMBED_ENDPOINT", "http://localhost:8090/embed")
RERANK_EP  = os.environ.get("RERANK_ENDPOINT", "http://localhost:8090/rerank")  # cross-encoder on the embedder host
COLL       = os.environ.get("QDRANT_COLLECTION", "corpus")
HOST       = os.environ.get("RAG_HOST", "0.0.0.0")
PORT       = int(os.environ.get("RAG_PORT", "8104"))
PREFETCH   = int(os.environ.get("RAG_PREFETCH", "40"))          # candidates per branch before fusion
CANDIDATES = int(os.environ.get("RAG_CANDIDATES", "40"))        # fused pool handed to the re-ranker (broad-in)
USE_RERANK = os.environ.get("RAG_USE_RERANKER", "1") != "0"     # cross-encoder rerank (default) vs legacy MMR
MIN_SCORE  = float(os.environ.get("RAG_MIN_SCORE", "0.0"))      # drop reranked hits below this (0.0 = keep all)
MMR_LAMBDA = float(os.environ.get("RAG_MMR_LAMBDA", "0.3"))     # legacy MMR only: relevance vs diversity
EXPAND     = int(os.environ.get("RAG_EXPAND_NEIGHBORS", "1"))   # same-section neighbor chunks each side (0=off)
EXPAND_MAX = int(os.environ.get("RAG_EXPAND_MAX_WORDS", "600")) # word cap on an expanded window

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("rag-retrieval")

_qc = QdrantClient(url=QDRANT_URL)
mcp = FastMCP("rag-retrieval", host=HOST, port=PORT)


def _embed_query(text: str) -> tuple[list[float], qm.SparseVector]:
    """Embed one query string -> (dense vector, sparse vector) via the embedder."""
    r = requests.post(EMBED, json={"input": [text], "kind": "query"}, timeout=120)
    r.raise_for_status()
    d = r.json()
    dense = d["dense"][0]
    sp = d["sparse"][0]  # {"<token_id>": weight, ...}
    sparse = qm.SparseVector(
        indices=[int(k) for k in sp.keys()],
        values=[float(v) for v in sp.values()],
    )
    return dense, sparse


# --- cross-encoder re-rank (default path) ------------------------------------
def _rerank(query: str, cands: list, k: int, min_score: float) -> list[tuple]:
    """Score every (query, chunk) pair with the cross-encoder (bge-reranker-v2-m3,
    served by the embedder host at /rerank), sort by that normalized relevance,
    drop anything below `min_score`, and return the top k as (point, score)
    tuples. Unlike MMR there is no diversity term, so N distinct records that
    share a template (e.g. contact rows) all keep their true scores instead of
    being suppressed as 'redundant'. The model runs on the embedder service
    (ADR-0008); this file still owns the ranking POLICY (pool, sort, floor, k)."""
    if not cands or k <= 0:
        return []
    docs = [(p.payload or {}).get("text_clean") or (p.payload or {}).get("text") or ""
            for p in cands]
    r = requests.post(RERANK_EP, json={"query": query, "documents": docs, "normalize": True}, timeout=120)
    r.raise_for_status()
    scores = r.json()["scores"]
    ranked = sorted(zip(cands, scores), key=lambda t: t[1], reverse=True)
    out = [(p, float(s)) for p, s in ranked if s >= min_score]
    return out[:k]


# --- MMR re-rank (legacy fallback, RAG_USE_RERANKER=0) ------------------------
def _dense_of(point) -> list[float] | None:
    v = getattr(point, "vector", None)
    if isinstance(v, dict):
        return v.get("dense")
    return v


def _cosine(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def _mmr(cands: list, k: int, lam: float) -> list:
    """Maximal Marginal Relevance selection. Relevance is the fused RRF score
    (normalized), redundancy is dense cosine against already-picked chunks, so a
    cluster of near-identical high-density slabs contributes ONE representative
    instead of monopolizing the top-k."""
    if not cands or k <= 0:
        return []
    top = max((c.score or 0.0) for c in cands) or 1.0
    selected: list = []
    pool = list(cands)
    while pool and len(selected) < k:
        best, best_score = None, -math.inf
        for c in pool:
            rel = (c.score or 0.0) / top
            cv = _dense_of(c)
            div = 0.0
            for s in selected:
                sv = _dense_of(s)
                if cv and sv:
                    div = max(div, _cosine(cv, sv))
            score = lam * rel - (1.0 - lam) * div
            if score > best_score:
                best_score, best = score, c
        selected.append(best)
        pool.remove(best)
    return selected


# --- same-section window expansion ------------------------------------------
def _concat_dedup(a: str, b: str) -> str:
    """Append b to a, dropping the longest (>=10 char) prefix of b that already
    ends a — chunks carry ~15% sentence overlap, so this reunites neighbors
    without repeating the shared boundary text. Formatting is preserved verbatim
    (a substring is dropped; nothing is re-flowed)."""
    m = min(len(a), len(b))
    for n in range(m, 9, -1):
        if a[-n:] == b[:n]:
            return a + b[n:]
    return a + "\n\n" + b


def _expand(payload: dict) -> tuple[str, int | None, int | None]:
    """Widen a doc_text hit to a bounded window of its SAME-SECTION neighbors
    (chunk_index +/- EXPAND, same source_file + section_title), capped at
    EXPAND_MAX words. Whole-section chunks have no such neighbors and pass through
    unchanged; only split large sections actually widen. Never crosses a section
    boundary, so no unrelated text leaks in. Returns (text, start_line, end_line)."""
    text = payload.get("text", "")
    s0, e0 = payload.get("chunk_start_line"), payload.get("chunk_end_line")
    cidx = payload.get("chunk_index")
    if EXPAND <= 0 or payload.get("kind") != "doc_text" or cidx is None:
        return text, s0, e0

    flt = qm.Filter(must=[
        qm.FieldCondition(key="source_file", match=qm.MatchValue(value=payload.get("source_file"))),
        qm.FieldCondition(key="section_title", match=qm.MatchValue(value=payload.get("section_title", ""))),
        qm.FieldCondition(key="chunk_index", range=qm.Range(gte=cidx - EXPAND, lte=cidx + EXPAND)),
    ])
    pts, _ = _qc.scroll(COLL, scroll_filter=flt, limit=2 * EXPAND + 1, with_payload=True)
    by_idx = {p.payload["chunk_index"]: p.payload for p in pts if "chunk_index" in (p.payload or {})}
    by_idx[cidx] = payload  # ensure the center is present even if scroll missed it

    # Greedily add the center, then nearest neighbors, until the word cap.
    chosen = [cidx]
    words = len(text.split())
    for d in range(1, EXPAND + 1):
        for j in (cidx - d, cidx + d):
            if j in by_idx and j not in chosen:
                w = len(by_idx[j].get("text", "").split())
                if words + w <= EXPAND_MAX:
                    chosen.append(j); words += w
    chosen.sort()

    merged = by_idx[chosen[0]].get("text", "")
    for j in chosen[1:]:
        merged = _concat_dedup(merged, by_idx[j].get("text", ""))
    starts = [by_idx[j].get("chunk_start_line") for j in chosen if by_idx[j].get("chunk_start_line") is not None]
    ends   = [by_idx[j].get("chunk_end_line")   for j in chosen if by_idx[j].get("chunk_end_line")   is not None]
    return merged, (min(starts) if starts else s0), (max(ends) if ends else e0)


@mcp.tool()
def search_corpus(query: str, k: int = 8) -> dict:
    """Search the infrastructure corpus and return the most relevant chunks.

    Use for questions about VMs, hosts, hostnames, IP addresses, VLANs, and any
    documented infra facts. Handles BOTH exact identifier lookups (e.g. a
    hostname like 'hsmbvxip001ts') and natural-language/semantic questions in
    Czech or English. Returns verbatim document chunks / table rows with their
    source file, section, and line span, so answers can be grounded and cited.

    Args:
        query: the user's question or an identifier to look up.
        k: number of chunks to return after re-ranking (default 8). Raise it for
           "list all …" questions where many distinct records match.
    """
    # Per-stage timing (ms) — returned as `_timing_ms` so any client (agent.py,
    # the OWUI result panel) can see where a slow query went; the cross-encoder
    # rerank is usually the dominant cost. Tiny payload; clients ignore it freely.
    tm = {}
    _t = time.perf_counter()
    dense, sparse = _embed_query(query)
    tm["embed_ms"] = round((time.perf_counter() - _t) * 1000, 1)

    # Broad-in: fuse a large candidate pool. Dense vectors are still fetched so the
    # legacy MMR path can measure redundancy; the reranker path ignores them.
    _t = time.perf_counter()
    res = _qc.query_points(
        collection_name=COLL,
        prefetch=[
            qm.Prefetch(query=dense, using="dense", limit=PREFETCH),
            qm.Prefetch(query=sparse, using="sparse", limit=PREFETCH),
        ],
        query=qm.FusionQuery(fusion=qm.Fusion.RRF),
        limit=max(CANDIDATES, k),
        with_payload=True,
        with_vectors=["dense"],
    )
    tm["fuse_ms"] = round((time.perf_counter() - _t) * 1000, 1)

    # Narrow-out: cross-encoder rerank (default) or legacy MMR. Both yield
    # (point, score) so the returned score always matches the ordering.
    _t = time.perf_counter()
    if USE_RERANK:
        selected = _rerank(query, res.points, k, MIN_SCORE)
    else:
        selected = [(p, p.score) for p in _mmr(res.points, k, MMR_LAMBDA)]
    tm["rerank_ms"] = round((time.perf_counter() - _t) * 1000, 1)

    _t = time.perf_counter()
    hits = []
    for p, score in selected:
        pl = p.payload or {}
        text, s_line, e_line = _expand(pl)
        hits.append({
            "score": score,
            "text": text,  # verbatim text_raw (possibly widened to a same-section window)
            "source_file": pl.get("source_file"),
            "section_title": pl.get("section_title"),
            "subsection_title": pl.get("subsection_title"),
            "location": pl.get("sheet") or pl.get("section_title") or pl.get("heading"),
            "lines": [s_line, e_line] if s_line is not None else None,
            "kind": pl.get("kind"),
            "lang": pl.get("lang"),
        })
    tm["expand_ms"] = round((time.perf_counter() - _t) * 1000, 1)
    log.info("search_corpus q=%r k=%d -> %d/%d cands  embed=%.0f fuse=%.0f rerank=%.0f expand=%.0f ms",
             query[:80], k, len(hits), len(res.points),
             tm["embed_ms"], tm["fuse_ms"], tm["rerank_ms"], tm["expand_ms"])
    return {"query": query, "count": len(hits), "results": hits, "_timing_ms": tm}


if __name__ == "__main__":
    log.info("rag-retrieval on %s:%d  (qdrant=%s coll=%s embed=%s) prefetch=%d cands=%d rerank=%s%s min_score=%.3f expand=%d",
             HOST, PORT, QDRANT_URL, COLL, EMBED, PREFETCH, CANDIDATES,
             USE_RERANK, (" via=%s" % RERANK_EP) if USE_RERANK else (" mmr_lambda=%.2f" % MMR_LAMBDA),
             MIN_SCORE, EXPAND)
    # Streamable HTTP transport so mcpo / the agent layer can register it over HTTP.
    mcp.run(transport="streamable-http")
