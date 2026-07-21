# Embedding Model Record

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-m3 |
| Revision   | latest (not pinned; fetched by `scripts/fetch-encoder.sh`) |
| Backend    | FlagEmbedding 1.4.0 (BGEM3FlagModel), CPU, FP32 (use_fp16=False) |
| Weights    | models/embeddings/bge-m3/ — `model.safetensors` (~2.3 GB) + `sparse_linear.pt` head (load-bearing for sparse); path in `.env` `EMBED_MODEL_PATH` |
| Output     | dense 1024-dim (L2-normalised) + sparse (lexical weights) |
| Languages  | 100+ incl. Czech and English |
| Max tokens | 8192 |
| Service    | services/embedder/server.py — POST http://127.0.0.1:8090/embed (embeddings only) |
| Unit       | manifests/ragfarm-embedder.service (host, CPU) |
| Updated    | 2026-07-21 |

The sibling cross-encoder reranker is recorded separately in `../reranker/MODEL.md`
(it moved out of the embedder to its own GPU service, ADR-0008); the generative LLM
in `../llm/MODEL.md`.
