"""
Embedder HTTP service — :8090/embed
Runs BAAI/bge-m3 on CUDA via FlagEmbedding (dense + sparse in one pass). This is the
service's ONLY endpoint. Reranking is a *separate*, GPU-accelerated service — a
dedicated llama.cpp `--reranking` server on :8081 (ADR-0008). The embedder and the
cross-encoder reranker no longer share a model family, a device, or a purpose, so
they no longer share a process: one endpoint here, embeddings only.

Start: python services/embedder/server.py
Request:  POST /embed  {"input": ["text1", ...], "kind": "passage"|"query"}
Response: {"dense": [[...1024...]], "sparse": [{"<tok_id>": weight, ...}], "dim": 1024}
"""
import os
import sys
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from FlagEmbedding import BGEM3FlagModel

MODEL_NAME = os.environ.get("EMBED_MODEL_PATH")
HOST = "127.0.0.1"
PORT = 8090

_model: BGEM3FlagModel | None = None
_device: str = "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model, _device
    # ADR-0013 §4 moved this service CPU -> CUDA. Ask for the device explicitly
    # rather than letting FlagEmbedding auto-detect: a silent CPU fallback here
    # reads as "it works, just slowly" and gets blamed on the wrong component.
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA is not available — this service is CUDA-only per ADR-0013 §4. "
            "Refusing to start rather than silently serving from CPU."
        )
    _device = f"cuda:0 ({torch.cuda.get_device_name(0)})"
    print(f"Loading {MODEL_NAME} on {_device}...", file=sys.stderr, flush=True)
    # use_fp16 is the normal CUDA setting; it halves bandwidth per embed on a
    # box where all three GPU consumers share one memory pipe.
    _model = BGEM3FlagModel(MODEL_NAME, use_fp16=True, devices="cuda:0")
    print(f"Model ready on {_device}.", file=sys.stderr, flush=True)
    yield


app = FastAPI(lifespan=lifespan)


class EmbedRequest(BaseModel):
    input: list[str]
    kind: Literal["passage", "query"] = "passage"


@app.get("/health")
def health():
    # device is reported so the step-03 gate (and anyone debugging slow ingest)
    # can confirm CUDA from the endpoint, not just from the startup log.
    return {"status": "ok", "device": _device}


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


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
