#!/usr/bin/env python3
"""
rag_pool_inspect.py — show the retrieval candidate pool for one or more queries.

WHY: when a chunk is returned at the wrong rank (or not at all), you need to know
whether it is a RANKING problem (the chunk is in the fused pool but scored low ->
a re-ranker can help) or a RECALL problem (the chunk never entered the pool -> you
need better first-stage recall / query expansion). This dumps the fused RRF pool,
and with --branches the dense-only and sparse-only lists too, so you can see which
signal surfaced (or failed to surface) each chunk.

This mirrors what rag-retrieval/server.py does BEFORE the cross-encoder re-rank
(ADR-0008; bge-reranker-v2-m3) + window expansion — it is deliberately the raw
first-stage view, so you can tell a ranking problem from a recall problem.

USAGE
  .venv/bin/python scripts/rag_pool_inspect.py "Jak se přihlásím do EPC?"
  .venv/bin/python scripts/rag_pool_inspect.py --branches "hsmbvxip001ts" "Zabbix"
  POOL=30 RAG_PREFETCH=30 .venv/bin/python scripts/rag_pool_inspect.py "..."

  # ADR-0010 §1 floor calibration — dump every fused candidate WITH its
  # cross-encoder score to CSV so a human can label required/junk and read the
  # right RAG_MIN_SCORE off the boundary. One row per (query, candidate).
  .venv/bin/python scripts/rag_pool_inspect.py --dump-scored calib.csv \
        "Jak se přihlásím do EPC?" "hsmbvxip001ts" "kontakty na Petr"

ENV (defaults match the loopback deployment):
  QDRANT_URL, EMBED_ENDPOINT, RERANK_ENDPOINT, QDRANT_COLLECTION, RAG_PREFETCH, POOL
Run with the project venv (.venv) — it has qdrant_client + requests.
"""
import os
import sys
import csv
import math
import argparse

try:
    import requests
    from qdrant_client import QdrantClient
    from qdrant_client import models as qm
except ImportError as e:
    sys.exit(f"missing dep ({e}); run with the project venv: .venv/bin/python {sys.argv[0]} ...")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
EMBED      = os.environ.get("EMBED_ENDPOINT", "http://127.0.0.1:8090/embed")
RERANK_EP  = os.environ.get("RERANK_ENDPOINT", "http://127.0.0.1:8081/reranking")
COLL       = os.environ.get("QDRANT_COLLECTION", "corpus")
PREFETCH   = int(os.environ.get("RAG_PREFETCH", "20"))   # per-branch candidates before fusion
POOL       = int(os.environ.get("POOL", "20"))           # rows to display

DEFAULT_QUERIES = ["Jak se přihlásím do EPC?", "hsmbvxip001ts", "Zabbix"]


def embed(text: str):
    r = requests.post(EMBED, json={"input": [text], "kind": "query"}, timeout=120)
    r.raise_for_status()
    d = r.json()
    dense = d["dense"][0]
    sp = d["sparse"][0]
    sparse = qm.SparseVector(indices=[int(k) for k in sp], values=[float(v) for v in sp.values()])
    return dense, sparse


def _loc(pl: dict) -> str:
    return pl.get("section_title") or pl.get("sheet") or pl.get("heading") or ""


def _row(i: int, p) -> str:
    pl = p.payload or {}
    snippet = (pl.get("text", "") or "").replace("\n", " ")[:70]
    return (f"  {i:2d}. {p.score:.3f}  {pl.get('kind',''):10}  {_loc(pl)!r}\n"
            f"        {snippet}")


def dump(qc: QdrantClient, query: str, branches: bool):
    dense, sparse = embed(query)
    print(f"\n=== {query!r} ===")

    fused = qc.query_points(
        COLL,
        prefetch=[qm.Prefetch(query=dense, using="dense", limit=PREFETCH),
                  qm.Prefetch(query=sparse, using="sparse", limit=PREFETCH)],
        query=qm.FusionQuery(fusion=qm.Fusion.RRF), limit=POOL, with_payload=True,
    ).points
    print(f"-- fused RRF pool ({len(fused)}) --")
    for i, p in enumerate(fused, 1):
        print(_row(i, p))

    if branches:
        for name, vec, using in (("dense (semantic)", dense, "dense"),
                                 ("sparse (lexical)", sparse, "sparse")):
            pts = qc.query_points(COLL, query=vec, using=using, limit=POOL, with_payload=True).points
            print(f"-- {name} top {len(pts)} --")
            for i, p in enumerate(pts, 1):
                print(_row(i, p))


def rerank_scores(query: str, cands: list) -> list[float]:
    """Score every candidate with the cross-encoder in the same way server.py does
    (bge-reranker-v2-m3, sigmoided). Returns a list aligned with `cands` order."""
    if not cands:
        return []
    docs = [(p.payload or {}).get("text_clean") or (p.payload or {}).get("text") or "" for p in cands]
    r = requests.post(RERANK_EP, json={"query": query, "documents": docs}, timeout=300)
    r.raise_for_status()
    scores = [0.0] * len(cands)
    for item in r.json()["results"]:
        scores[item["index"]] = 1.0 / (1.0 + math.exp(-float(item["relevance_score"])))
    return scores


def dump_scored(qc: QdrantClient, queries: list[str], out_path: str, pool_size: int) -> None:
    """ADR-0010 §1 calibration dump: emit one CSV row per (query, candidate) with
    fused rank, reranker score, source, section, kind, and a text head. The human
    labels each row required/junk in a `label` column, then reads the highest score
    that keeps all required rows (including low-scoring reverse FW rules) while
    cutting junk — that value goes into RAG_MIN_SCORE."""
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query", "rank_fused", "rerank_score", "label",
                    "source_file", "section", "kind", "text_head"])
        for q in queries:
            dense, sparse = embed(q)
            pts = qc.query_points(
                COLL,
                prefetch=[qm.Prefetch(query=dense, using="dense", limit=PREFETCH),
                          qm.Prefetch(query=sparse, using="sparse", limit=PREFETCH)],
                query=qm.FusionQuery(fusion=qm.Fusion.RRF), limit=pool_size, with_payload=True,
            ).points
            scores = rerank_scores(q, pts)
            for i, (p, s) in enumerate(zip(pts, scores)):
                pl = p.payload or {}
                head = (pl.get("text", "") or "").replace("\n", " ").replace("\r", " ")[:200]
                w.writerow([q, i + 1, f"{s:.4f}", "",
                            pl.get("source_file", ""), _loc(pl), pl.get("kind", ""), head])
            print(f"  dumped {len(pts)} candidates for {q!r}")
    print(f"wrote {out_path} — label the rows and read RAG_MIN_SCORE off the required/junk boundary")


def main():
    ap = argparse.ArgumentParser(description="inspect the RAG candidate pool (recall vs ranking)")
    ap.add_argument("queries", nargs="*", default=[], help="queries (default: a built-in set)")
    ap.add_argument("--branches", action="store_true", help="also dump dense-only and sparse-only lists")
    ap.add_argument("--dump-scored", metavar="CSV",
                    help="ADR-0010 §1 floor calibration: write reranker-scored candidates to CSV "
                         "with an empty `label` column for human required/junk labelling.")
    ap.add_argument("--pool", type=int, default=POOL,
                    help=f"candidate pool size for --dump-scored (default {POOL}; server.py uses RAG_CANDIDATES=40)")
    args = ap.parse_args()
    queries = args.queries or DEFAULT_QUERIES
    qc = QdrantClient(url=QDRANT_URL)
    print(f"collection={COLL} prefetch={PREFETCH} pool={POOL} embed={EMBED}")
    if args.dump_scored:
        dump_scored(qc, queries, args.dump_scored, args.pool)
        return
    for q in queries:
        dump(qc, q, args.branches)


if __name__ == "__main__":
    main()
