"""
Embedder + reranker HTTP service — :8090
Runs BAAI/bge-m3 on CPU via FlagEmbedding (dense + sparse in one pass), and — on
demand — the sibling cross-encoder BAAI/bge-reranker-v2-m3 for /rerank. Both are
the same BGE-M3 family; co-hosting them keeps one CPU model host (rag-retrieval
stays a thin HTTP client, per ADR-0008).

Start: python services/embedder/server.py
Request:  POST /embed   {"input": ["text1", ...], "kind": "passage"|"query"}
Response: {"dense": [[...1024...]], "sparse": [{"<tok_id>": weight, ...}], "dim": 1024}
Request:  POST /rerank  {"query": "...", "documents": ["d1", ...], "normalize": true}
Response: {"scores": [0.94, 0.01, ...]}   # aligned with documents order
"""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from FlagEmbedding import BGEM3FlagModel

MODEL_NAME = os.environ.get("EMBED_MODEL_PATH")
# Reranker is lazy-loaded on the first /rerank call so embedder startup stays fast
# and the ~2.3GB model costs nothing until retrieval actually uses it.
RERANK_MODEL_NAME = os.environ.get("RERANK_MODEL_PATH", "BAAI/bge-reranker-v2-m3")
HOST = "127.0.0.1"
PORT = 8090

_model: BGEM3FlagModel | None = None
_reranker = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    print(f"Loading {MODEL_NAME} on CPU...", file=sys.stderr, flush=True)
    _model = BGEM3FlagModel(MODEL_NAME, use_fp16=False)
    print("Model ready.", file=sys.stderr, flush=True)
    yield


app = FastAPI(lifespan=lifespan)


def _get_reranker():
    """Lazy-load bge-reranker-v2-m3 (CPU, safetensors) on first /rerank call."""
    global _reranker
    if _reranker is None:
        from FlagEmbedding import FlagReranker
        print(f"Loading reranker {RERANK_MODEL_NAME} on CPU...", file=sys.stderr, flush=True)
        _reranker = FlagReranker(RERANK_MODEL_NAME, use_fp16=False)
        print("Reranker ready.", file=sys.stderr, flush=True)
    return _reranker


class EmbedRequest(BaseModel):
    input: list[str]
    kind: Literal["passage", "query"] = "passage"


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    normalize: bool = True  # sigmoid -> [0,1] so a RAG_MIN_SCORE floor is meaningful


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/embed")
def embed(req: EmbedRequest):
    if not req.input:
        raise HTTPException(status_code=400, detail="input must be a non-empty list")

    output = _model.encode(
        req.input,
        batch_size=12,
        max_length=8192,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False,
    )

    dense = [v.tolist() for v in output["dense_vecs"]]
    # lexical_weights keys are token-id ints; stringify for JSON
    sparse = [{str(k): float(v) for k, v in row.items()} for row in output["lexical_weights"]]

    return JSONResponse({"dense": dense, "sparse": sparse, "dim": len(dense[0])})


@app.post("/rerank")
def rerank(req: RerankRequest):
    if not req.documents:
        raise HTTPException(status_code=400, detail="documents must be a non-empty list")

    rr = _get_reranker()
    scores = rr.compute_score([[req.query, d] for d in req.documents], normalize=req.normalize)
    if not isinstance(scores, list):
        scores = [scores]  # single-pair calls return a scalar
    return JSONResponse({"scores": [float(s) for s in scores]})


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
