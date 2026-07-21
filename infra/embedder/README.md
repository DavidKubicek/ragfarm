# Embedder — BAAI/bge-m3 on CPU (`:8090/embed`)

Per ADR-0002 the embedder runs **BAAI/bge-m3 on CPU** (the NPU path was abandoned —
English-only, seq-limited models don't fit this mixed Czech/English, wide-table
corpus; `quark_quantize.py` here is a vestige of that abandoned NPU attempt).

## One endpoint, one job
This service exposes exactly **one** endpoint: `POST /embed`. It returns BGE-M3's
dense (1024-dim) + sparse vectors in a single pass, for both ingestion and query
embedding. That is all it does.

**Reranking is NOT here.** It used to be a `/rerank` sub-endpoint on this service,
but the reranker and the embedder no longer share a model family, a device, or a
purpose, so co-hosting them was a wart (ADR-0008). The cross-encoder reranker is now
its own dedicated GPU service — a `llama-server --reranking` instance on **:8081** —
with its own unit (`manifests/ragfarm-reranker.service`) and record
(`models/reranker/MODEL.md`). This service stays embeddings-only.

## Contract
```
POST http://127.0.0.1:8090/embed
  {"input": ["text1", ...], "kind": "passage"|"query"}
->{"dense": [[...1024...]], "sparse": [{"<tok_id>": weight, ...}], "dim": 1024}
```

## Run
Code: `services/embedder/server.py` (FastAPI + FlagEmbedding `BGEM3FlagModel`).
Unit: `manifests/ragfarm-embedder.service` (host, CPU; `EMBED_MODEL_PATH` pins the
BGE-M3 snapshot). Model record + revision: `models/embeddings/MODEL.md`.
