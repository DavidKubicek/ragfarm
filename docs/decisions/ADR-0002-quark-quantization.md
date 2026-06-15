# ADR-0002: Use AMD Quark to quantize the NPU embedding model

Status: Accepted (2026-06-10)

## Context
The ingester embeds thousands of documents. Per ADR-0001 this runs on the NPU.
The RyzenAI Vitis AI EP requires models compiled for the NPU; floating-point
encoders are internally converted to BF16 and compiled via the BF16 flow, or can
be quantized explicitly.

## Decision
Use **AMD Quark** (the cross-platform PyTorch/ONNX quantizer shipped with the
RyzenAI stack) to prepare the embedding/encoder model:
- BF16 compilation flow for encoder (BERT-class) models, OR INT8 where the model
  and accuracy budget allow.
- Output consumed by the RyzenAI ONNX Runtime EP (`libonnxruntime_providers_ryzenai.so`).

For the generative LLM on the iGPU we do **not** use Quark — that path uses
**GGUF** quantization (e.g. Q4_K_M) consumed by llama.cpp/Vulkan.

## Notes / open items for the agent
- Pick a BF16-friendly encoder (e.g. a BGE/E5-class or MiniLM-class sentence
  encoder) and verify it compiles cleanly through Quark → RyzenAI EP. Record the
  exact model + revision in `models/embeddings/MODEL.md`.
- The RyzenAI Linux LLM tooling (`model-generate==1.7.1`) installs from AMD's
  private index: `https://pypi.amd.com/ryzenai_llm/1.7.1/linux/simple/`.
- NPU + XRT driver packages are **account-gated** at account.amd.com and cannot
  be fetched non-interactively. Dave downloads them manually; the install script
  consumes them from a local path. See `infra/npu/install_npu.sh`.
