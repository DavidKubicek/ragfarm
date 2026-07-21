# Embedding Model Record

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-m3 |
| Revision   | 50f9396f75618b3389c1fd1068a1ff58dc7b5b26 |
| Backend    | FlagEmbedding 1.4.0 (BGEM3FlagModel), CPU, FP32 (use_fp16=False) |
| Load flag  | model_kwargs={"use_safetensors": True} |
| Weights    | ~/.cache/huggingface/hub/models--BAAI--bge-m3 (safetensors, ~6.4 GB; not in repo) |
| Output     | dense 1024-dim (L2-normalised) + sparse (lexical weights) |
| Languages  | 100+ incl. Czech and English |
| Max tokens | 8192 |
| Service    | services/embedder/server.py — POST http://127.0.0.1:8090/embed (embeddings only) |
| Unit       | manifests/ragfarm-embedder.service (host, CPU) |
| Updated    | 2026-06-16 |

The sibling cross-encoder reranker is recorded separately in `../reranker/MODEL.md`
(it moved out of the embedder to its own GPU service, ADR-0008); the generative LLM
in `../llm/MODEL.md`.
