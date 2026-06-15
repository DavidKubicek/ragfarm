# ragfarm — on-prem RAG + infra-control agent for AMD Ryzen AI 9 HX 370

A fully on-prem retrieval-augmented assistant for a customer VM farm. It answers
questions over thousands of internal documents/notes **and** lets infra admins
ask operational questions in natural language ("where is VM1 running?",
"reboot host X") which are dispatched through MCP microservices to the
infrastructure control plane (OpenNebula).

## Hardware target
- **AceMagic F5X**, AMD **Ryzen AI 9 HX 370** (Strix Point, "STX")
  - 12 cores (4× Zen 5 + 8× Zen 5c), Radeon **890M** iGPU (RDNA 3.5, gfx1150),
    **XDNA 2 NPU** (50 TOPS INT8)
- OS: **Ubuntu 24.04 LTS**, kernel **≥ 6.10**, Python **3.12.x**, 64 GB RAM recommended

## The one architectural fact that drives everything
On **Linux**, AMD Ryzen AI 1.7.1 supports an **NPU-only LLM flow**. There is
**no hybrid single-model NPU+iGPU split on Linux** — that flow is Windows-only.
Also, AMD's own stack states **llama.cpp reaches the iGPU only, never the NPU**.

Therefore we split the APU by *role*, not within one model:
- **iGPU (890M, Vulkan)** runs the generative LLM (good decode bandwidth) — `llama-server`.
- **NPU (XDNA 2)** runs the embedding/encoder model for the ingester (efficient prefill, low watts) — RyzenAI EP.
- **CPU (Zen 5)** runs Qdrant, the MCP services, and orchestration.

See `docs/decisions/ADR-0001-engine-split.md` for the full rationale and the
measured numbers that justify it.

## Layout
- `docs/` — salvaged AMD reference material + architecture decisions
- `infra/` — compose stack, llama build/launch, NPU driver install
- `models/` — GGUF (iGPU LLM) and BF16 OGA encoder (NPU embeddings)
- `services/` — MCP microservices + the ingester
- `manifests/` — systemd units / env manifests per service

## Where to start (humans and agents)
Read `HANDOFF.md` at the repo root. It is the authoritative build plan and is
written so Claude Code can execute it autonomously.
