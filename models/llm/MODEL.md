# Generative LLM Model Record (ADR-0001)

| Field      | Value |
|------------|-------|
| Model      | Qwen2.5-7B-Instruct |
| Quant      | GGUF Q4_K_M (2 shards) |
| Weights    | models/gguf/qwen2.5-7b-instruct-q4_k_m-0000{1,2}-of-00002.gguf (~4.7 GB, gitignored) |
| Backend    | llama.cpp `llama-server`, **Vulkan / iGPU** (Radeon 890M, RADV GFX1150) |
| Context    | 32768 (`-c 32768`), `--context-shift`, `--keep 3072` |
| Decoding   | greedy / deterministic: temp 0, top_k 1, top_p/min_p 0, seed 42 |
| Tools      | `--jinja` (chat template) → OpenAI-compatible tool-calling |
| Service    | OpenAI-compatible — http://127.0.0.1:8080/v1 (alias `qwen2.5-7b-instruct`) |
| Unit       | manifests/ragfarm-llama.service (host, iGPU/Vulkan) |
| Build      | infra/llama/README.md (Vulkan backend) |
| Updated    | 2026-07-21 |

The embedder is recorded in `../embeddings/MODEL.md`, the reranker in
`../reranker/MODEL.md`. Both `llama-server` instances (this LLM on :8080 and the
reranker on :8081) run on the iGPU via Vulkan. Prod-hardware swap (CUDA server, a
~30B model) is tracked in `docs/deployment.md` → "What changes on prod NVIDIA
hardware".
