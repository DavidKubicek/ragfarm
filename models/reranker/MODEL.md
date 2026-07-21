# Reranker Model Record (ADR-0008)

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-reranker-v2-m3 |
| Revision   | 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e |
| Backend    | llama.cpp `--reranking`, **Vulkan / iGPU** (Radeon 890M, RADV GFX1150), f16 GGUF |
| Weights    | models/gguf/bge-reranker-v2-m3-f16.gguf (~1.15 GB, gitignored — regenerate below) |
| HF source  | ~/.cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3 (safetensors, ~2.2 GB) |
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
From the cached HF safetensors (no re-download), via llama.cpp's converter:
```bash
SNAP=$(ls -d ~/.cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3/snapshots/*/ | head -1)
.venv/bin/python ~/llama.cpp/convert_hf_to_gguf.py "$SNAP" \
  --outfile models/gguf/bge-reranker-v2-m3-f16.gguf --outtype f16
```
If the HF snapshot is absent, fetch it first (safetensors-only, per the standing
no-pickle rule): `huggingface_hub.snapshot_download("BAAI/bge-reranker-v2-m3",
ignore_patterns=["*.bin"])`.
