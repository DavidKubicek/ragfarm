#!/usr/bin/env python3
"""
rag_pool_inspect.py — show the retrieval candidate pool for one or more queries.

WHY: when a chunk is returned at the wrong rank (or not at all), you need to know
whether it is a RANKING problem (the chunk is in the fused pool but scored low ->
a re-ranker can help) or a RECALL problem (the chunk never entered the pool -> you
need better first-stage recall / query expansion). This dumps the fused RRF pool,
and with --branches the dense-only and sparse-only lists too, so you can see which
signal surfaced (or failed to surface) each chunk.

This mirrors what rag-retrieval/server.py does BEFORE the MMR re-rank + window
expansion — it is deliberately the raw first-stage view.

USAGE
  .venv/bin/python scripts/rag_pool_inspect.py "Jak se přihlásím do EPC?"
  .venv/bin/python scripts/rag_pool_inspect.py --branches "hsmbvxip001ts" "Zabbix"
  POOL=30 RAG_PREFETCH=30 .venv/bin/python scripts/rag_pool_inspect.py "..."

ENV (defaults match the loopback deployment):
  QDRANT_URL, EMBED_ENDPOINT, QDRANT_COLLECTION, RAG_PREFETCH, POOL
Run with the project venv (.venv) — it has qdrant_client + requests.
"""
import os
import sys
import argparse

try:
    import requests
    from qdrant_client import QdrantClient
    from qdrant_client import models as qm
except ImportError as e:
    sys.exit(f"missing dep ({e}); run with the project venv: .venv/bin/python {sys.argv[0]} ...")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
EMBED      = os.environ.get("EMBED_ENDPOINT", "http://127.0.0.1:8090/embed")
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


def main():
    ap = argparse.ArgumentParser(description="inspect the RAG candidate pool (recall vs ranking)")
    ap.add_argument("queries", nargs="*", default=[], help="queries (default: a built-in set)")
    ap.add_argument("--branches", action="store_true", help="also dump dense-only and sparse-only lists")
    args = ap.parse_args()
    queries = args.queries or DEFAULT_QUERIES
    qc = QdrantClient(url=QDRANT_URL)
    print(f"collection={COLL} prefetch={PREFETCH} pool={POOL} embed={EMBED}")
    for q in queries:
        dump(qc, q, args.branches)


if __name__ == "__main__":
    main()
