# iGPU LLM — llama.cpp + Vulkan on Radeon 890M

Per ADR-0001 the generative LLM runs on the **iGPU via Vulkan** (not ROCm, which
is unofficial on gfx1150). This serves an OpenAI-compatible API with tool-calling.

## Build (Vulkan backend)
```bash
sudo apt-get install -y libvulkan-dev vulkan-tools glslc cmake build-essential git
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

## Notes
- `-ngl 999` pushes all layers to Vulkan; verify VRAM/UMA headroom with
  `llama-server` startup logs.
- If Vulkan underperforms, benchmark a CPU build as a floor, and only then
  evaluate experimental ROCm for gfx1150 — do not assume ROCm works.
- This server is the single LLM endpoint the MCP gateway/agent talks to.
