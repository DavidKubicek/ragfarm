"""
Embedder HTTP service — :8090/embed
Runs BAAI/bge-m3 on CPU via FlagEmbedding (dense + sparse in one pass).

Start: python services/embedder/server.py
Request:  POST /embed  {"input": ["text1", ...], "kind": "passage"|"query"}
Response: {"dense": [[...1024...]], "sparse": [{"<tok_id>": weight, ...}], "dim": 1024}
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

MODEL_NAME = "BAAI/bge-m3"
HOST = "127.0.0.1"
PORT = 8090

_model: BGEM3FlagModel | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    print(f"Loading {MODEL_NAME} on CPU...", file=sys.stderr, flush=True)
    _model = BGEM3FlagModel(MODEL_NAME, use_fp16=False, model_kwargs={"use_safetensors": True})
    print("Model ready.", file=sys.stderr, flush=True)
    yield


app = FastAPI(lifespan=lifespan)


class EmbedRequest(BaseModel):
    input: list[str]
    kind: Literal["passage", "query"] = "passage"


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


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
