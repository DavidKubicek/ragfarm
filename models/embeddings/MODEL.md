# Embedding Model

**Model:** BAAI/bge-m3  
**Revision:** 5617a9f61b028005a4858fdac845db406aefb181  
**Source:** https://huggingface.co/BAAI/bge-m3  
**Parameters:** 568M  
**Embedding dim:** 1024 (dense), variable-length sparse (lexical weights)  
**Max seq len:** 8192  
**Languages:** 100+ (incl. Czech, English)  
**Quantization:** none — CPU FP32 via FlagEmbedding  
**Runtime:** FlagEmbedding 1.4.0, BGEM3FlagModel, use_fp16=False

## Rationale for switch from bge-small-en-v1.5

Prior NPU build (bge-small-en-v1.5, 384-dim) was English-only and limited to seq=128.
Corpus contains mixed Czech + English with wide structured table rows.  
BGE-M3 handles 100+ languages, 8192-token context, and emits both dense (1024-dim)
and sparse (lexical) vectors — enabling hybrid retrieval in Qdrant (step 04).  
CPU inference is fast enough for batch ingestion; NPU path abandoned (ADR-0002).

## Paths

| Artefact | Path |
|----------|------|
| HuggingFace cache | `~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181/` |
| HTTP service | `services/embedder/server.py` |

## Service contract

Listens on `127.0.0.1:8090`.  
`POST /embed` — body `{"input": ["text", ...], "kind": "passage"|"query"}`,
response `{"dense": [[...1024...]], "sparse": [{"<token_id>": weight, ...}], "dim": 1024}`.  
`GET /health` — returns `{"status": "ok"}`.  
Dense vectors are L2-normalised (norm ≈ 1.0).  
`kind` defaults to `passage`; retrieval (step 07) passes `query`.
