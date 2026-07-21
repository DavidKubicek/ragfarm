# iGPU llama.cpp + Vulkan on Radeon 890M — TWO servers

Per ADR-0001 we run llama.cpp on the **iGPU via Vulkan** (not ROCm, which is
unofficial on gfx1150). We now run **two** `llama-server` instances from the same
build, both on the iGPU, both Vulkan:

| server | model | port | endpoint | unit |
|--------|-------|------|----------|------|
| **LLM** | Qwen2.5-7B-Instruct Q4_K_M GGUF | 8080 | OpenAI-compatible (`--jinja`, tool-calling) | `manifests/ragfarm-llama.service` |
| **reranker** | bge-reranker-v2-m3 f16 GGUF | 8081 | `--reranking` (cross-encoder scores) | `manifests/ragfarm-reranker.service` |

The reranker moved here (from a CPU sub-endpoint on the embedder) because
llama.cpp's `--reranking` runs the cross-encoder on the **same Vulkan iGPU** as the
LLM — no ROCm, no torch — cutting rerank latency from ~36 s to ~1.7 s (ADR-0008).
The two are separate processes: a single `llama-server` serves one model, and there
is no shared KV cache to gain (a reranker is a single-pass encoder — no
autoregressive cache — and caches are model-specific anyway). VRAM is ample: ~4.7 GB
LLM + ~1.2 GB reranker in the ~48 GB UMA. Records: `models/llm/MODEL.md`,
`models/reranker/MODEL.md`.

The build below is shared by both servers.

Per ADR-0001 the generative LLM runs on the **iGPU via Vulkan** (not ROCm, which
is unofficial on gfx1150). This serves an OpenAI-compatible API with tool-calling.

## Build (Vulkan backend)
```bash
sudo apt-get install -y libvulkan-dev vulkan-tools glslc cmake build-essential git spirv-headers libshaderc-dev
vulkaninfo | grep -i "deviceName"   # expect Radeon 890M / RADV GFX1150

git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j
```

## Model
`Qwen2.5-7B-Instruct` GGUF, Q4_K_M. Place in `../../models/gguf/`.
(7B Q4_K_M ~4.7 GB; fits comfortably in UMA. Bump iGPU/UMA allocation in BIOS if
needed.)

## Launch (OpenAI-compatible server with tools enabled)
```bash
./build/bin/llama-server \
  -m ../../models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  -ngl 999 \                # offload all layers to the iGPU
  -c 16384 \                # context; raise if your docs need it
  --jinja \                 # enable chat template -> needed for tool-calling
  --alias qwen2.5-7b-instruct
```
Health check: `curl 127.0.0.1:8080/v1/models`

## Launch — reranker (second server, same binary)
```bash
./build/bin/llama-server \
  -m ../../models/gguf/bge-reranker-v2-m3-f16.gguf \
  --reranking \           # cross-encoder scoring endpoint (raw logits)
  --host 127.0.0.1 --port 8081 -ngl 999 --mlock --mmap \
  -b 4096 -ub 4096 \      # each (query,doc) pair scored in ONE physical batch; must exceed the longest pair
  --alias bge-reranker-v2-m3
```
Probe: `curl 127.0.0.1:8081/reranking -d '{"query":"q","documents":["a","b"]}'`
→ `{"results":[{"index":i,"relevance_score":<logit>}, ...]}`. rag-retrieval sigmoids
the logit to [0,1]. The GGUF is generated from cached HF weights — see
`models/reranker/MODEL.md`.

## Notes
- `-ngl 999` pushes all layers to Vulkan; verify VRAM/UMA headroom with
  `llama-server` startup logs.
- If Vulkan underperforms, benchmark a CPU build as a floor, and only then
  evaluate experimental ROCm for gfx1150 — do not assume ROCm works.
- This server is the single LLM endpoint the MCP gateway/agent talks to.
