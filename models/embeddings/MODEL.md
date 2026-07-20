# Embedding Model Record

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-m3 |
| Revision   | 50f9396f75618b3389c1fd1068a1ff58dc7b5b26 |
| Backend    | FlagEmbedding 1.4.0 (BGEM3FlagModel), CPU, FP32 (use_fp16=False) |
| Load flag  | model_kwargs={"use_safetensors": True} |
| Output     | dense 1024-dim (L2-normalised) + sparse (lexical weights) |
| Languages  | 100+ incl. Czech and English |
| Max tokens | 8192 |
| Service    | POST http://127.0.0.1:8090/embed |
| Updated    | 2026-06-16 |

## Reranker Model Record (ADR-0008)

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-reranker-v2-m3 |
| Revision   | 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e |
| Backend    | FlagEmbedding (FlagReranker), CPU, FP32 (use_fp16=False) |
| Load flag  | safetensors-only (downloaded with ignore_patterns=["*.bin"]) |
| Output     | one relevance score per (query, document) pair; normalize=True → sigmoid [0,1] |
| Role       | cross-encoder rerank of the fused RRF candidate pool in search_corpus |
| Languages  | 100+ incl. Czech and English (same XLM-RoBERTa family as bge-m3) |
| Service    | POST http://127.0.0.1:8090/rerank (lazy-loaded on first call) |
| Updated    | 2026-07-20 |
