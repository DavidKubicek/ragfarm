# ADR-0001: Split the APU by role (iGPU=LLM, NPU=embeddings), not within one model
Author: David Kubicek (david.kubicek@eywo.cz)

Status: **SUPERSEDED by ADR-0013** (2026-08-03). Accepted 2026-06-10 and correct
for its hardware; the AMD Ryzen AI 9 HX 370 target it reasons about is being
replaced by an NVIDIA DGX Spark (GB10). The iGPU/NPU role-split, llama.cpp+Vulkan,
and the NPU decode numbers below are **historical context only** — do not act on
them. See `ADR-0013-spark-engine-split-vllm-nvfp4.md` for the live decision.
Context owner: David Kubicek

## Context
The deployment target is an AMD Ryzen AI 9 HX 370 (Strix Point) on Ubuntu 24.04,
Linux only. The initial intent was to use AMD's "hybrid" NPU+iGPU flow to run a
single LLM with prefill on the NPU and decode on the iGPU.

Investigation of AMD Ryzen AI Software 1.7.1 (docs last updated 2026-04-19)
established the following facts:

1. The hybrid single-model NPU+iGPU LLM flow (OGA hybrid) is **Windows-only**.
   The Linux release explicitly supports an **NPU-only LLM flow**.
2. AMD's own software-stack table states **llama.cpp is supported for the iGPU
   only** — llama.cpp never targets the NPU on any OS.
3. The Linux NPU LLM flow is real but asymmetric. Measured on this class of part
   (AMD's own Phi-3.5-mini NPU benchmark): prefill / time-to-first-token ~865
   tok/s, but token **generation only ~17.6 tok/s**. The NPU is excellent at the
   compute-bound prefill/encode phase and weak at the memory-bound decode phase.
4. ROCm support for the 890M (gfx1150) is partial/unofficial. **Vulkan** is the
   dependable llama.cpp iGPU backend on this part; ROCm is opt-in, validate
   separately, do not assume.

## Decision
Split the APU by role:
- **iGPU (Radeon 890M) via llama.cpp + Vulkan** runs the generative LLM
  (`Qwen2.5-7B-Instruct`, GGUF Q4_K_M). The iGPU's memory bandwidth suits the
  decode phase, and `llama-server` gives an OpenAI-compatible API with native
  tool-calling — exactly what the MCP/agent layer needs.
- **NPU (XDNA 2) via RyzenAI EP** runs the **embedding/encoder model** for the
  ingester. Encoders are prefill-shaped — the NPU's strength — and run at very
  low power, ideal for bulk-embedding thousands of documents.
- **CPU (Zen 5)** runs Qdrant, the MCP microservices, and orchestration.

## Consequences
- We get native tool-calling + OpenAI-compatible serving for the agent loop
  immediately, instead of fighting the immature Linux OGA tool-calling path.
- The NPU is not idle: it does the heavy, repetitive embedding work.
- We forgo the marketed "hybrid" single-model speedup. If that ever becomes a
  hard requirement, it forces a move to Windows and we lose the Linux MCP/agent
  ergonomics — explicitly rejected here.
- Embedding model is quantized for the NPU with **AMD Quark** (PyTorch/ONNX →
  BF16/INT8). See ADR-0002.

## Alternatives rejected
- **NPU-only LLM (Phi-3.5 / Llama-8B NPU builds):** decode ~17 tok/s is too slow
  for an interactive agent doing multi-turn tool calls; narrow model support;
  account-gated tooling.
- **Windows hybrid flow:** fastest single-model path, but abandons the Linux
  agent/MCP stack the project is built around.
