# Reranker Model Record (ADR-0008)

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-reranker-v2-m3 |
| Revision   | latest (not pinned; fetched by `scripts/fetch-encoder.sh`) |
| Backend    | llama.cpp `--reranking`, **Vulkan / iGPU** (Radeon 890M, RADV GFX1150), f16 GGUF |
| Weights    | models/reranker/bge-reranker-v2-m3/bge-reranker-v2-m3-f16.gguf (~1.15 GB, gitignored); path in `.env` `RERANK_MODEL_PATH` |
| HF source  | BAAI/bge-reranker-v2-m3 (latest) — downloaded + converted to GGUF by `scripts/fetch-encoder.sh` |
| Output     | one relevance score per (query, document). llama.cpp returns the **raw logit**; rag-retrieval applies `sigmoid` → [0,1] (identical to FlagReranker `normalize=True`) |
| Role       | cross-encoder rerank of the fused RRF candidate pool in `search_corpus` |
| Languages  | 100+ incl. Czech and English (XLM-RoBERTa-large family, sibling of bge-m3) |
| Latency    | **~1.7 s / 40 candidates on the iGPU** (was ~36 s on CPU inside the embedder) |
| Service    | dedicated llama.cpp server — POST http://127.0.0.1:8081/reranking |
| Unit       | manifests/ragfarm-reranker.service (host, iGPU/Vulkan) |
| Updated    | 2026-07-21 |

## Why a second llama.cpp server (not an embedder sub-endpoint)
The reranker shares nothing with the embedder anymore — different model, different
device (iGPU vs CPU), different purpose. It is a plain `llama-server --reranking`
instance, so there is no first-party code and no `services/reranker/` directory: the
unit file is the whole deliverable. See ADR-0008.

## Regenerating the GGUF (weights are gitignored)
The download + f16 GGUF conversion live in the fetch script — one command:
```bash
scripts/fetch-encoder.sh            # fetches bge-m3 + converts this reranker to GGUF
scripts/fetch-encoder.sh --force    # re-fetch + re-convert
scripts/fetch-encoder.sh --list     # other embedder+reranker pairs
```
It writes `RERANK_MODEL_PATH` into `.env`, which `ragfarm-reranker.service` reads.
