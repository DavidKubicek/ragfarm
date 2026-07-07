"""
rag-retrieval — MCP server exposing `search_corpus` over the ingested Qdrant
corpus (step 04). Hybrid retrieval: embed the query via the step-03 embedder
(:8090/embed, kind=query), then run Qdrant's Query API with TWO prefetch
branches — dense (semantic) and sparse (exact host/IP/VLAN token match) — fused
by Reciprocal Rank Fusion (RRF). This one tool serves both Czech/English
semantic search and verbatim identifier lookups, which is why ADR-0003 keeps RAG
in the MCP layer (Option B) instead of Open WebUI's generic document RAG.

Transport: streamable HTTP (MCP), same pattern as the other services, so mcpo /
the agent layer registers it over HTTP.

Config (env): QDRANT_URL, EMBED_ENDPOINT, QDRANT_COLLECTION, RAG_PORT.
"""
from __future__ import annotations

import os
import logging

import requests
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from mcp.server.fastmcp import FastMCP

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED      = os.environ.get("EMBED_ENDPOINT", "http://localhost:8090/embed")
COLL       = os.environ.get("QDRANT_COLLECTION", "corpus")
HOST       = os.environ.get("RAG_HOST", "0.0.0.0")
PORT       = int(os.environ.get("RAG_PORT", "8104"))
PREFETCH   = int(os.environ.get("RAG_PREFETCH", "20"))  # candidates per branch before fusion

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


@mcp.tool()
def search_corpus(query: str, k: int = 5) -> dict:
    """Search the infrastructure corpus and return the most relevant chunks.

    Use for questions about VMs, hosts, hostnames, IP addresses, VLANs, and any
    documented infra facts. Handles BOTH exact identifier lookups (e.g. a
    hostname like 'hsmbvxip001ts') and natural-language/semantic questions in
    Czech or English. Returns verbatim table rows / document chunks with their
    source, so answers can be grounded and cited.

    Args:
        query: the user's question or an identifier to look up.
        k: number of chunks to return (default 5).
    """
    dense, sparse = _embed_query(query)
    res = _qc.query_points(
        collection_name=COLL,
        prefetch=[
            qm.Prefetch(query=dense, using="dense", limit=PREFETCH),
            qm.Prefetch(query=sparse, using="sparse", limit=PREFETCH),
        ],
        query=qm.FusionQuery(fusion=qm.Fusion.RRF),
        limit=k,
        with_payload=True,
    )
    hits = []
    for p in res.points:
        pl = p.payload or {}
        hits.append({
            "score": p.score,
            "text": pl.get("text", ""),
            "source_file": pl.get("source_file"),
            "location": pl.get("sheet") or pl.get("heading"),
            "kind": pl.get("kind"),
            "lang": pl.get("lang"),
        })
    log.info("search_corpus q=%r k=%d -> %d hits", query[:80], k, len(hits))
    return {"query": query, "count": len(hits), "results": hits}


if __name__ == "__main__":
    log.info("rag-retrieval on %s:%d  (qdrant=%s coll=%s embed=%s)", HOST, PORT, QDRANT_URL, COLL, EMBED)
    # Streamable HTTP transport so mcpo / the agent layer can register it over HTTP.
    mcp.run(transport="streamable-http")
