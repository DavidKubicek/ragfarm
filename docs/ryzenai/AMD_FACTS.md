# AMD Ryzen AI 1.7.1 — operative facts (digest)

Mirrored/summarised from ryzenai.docs.amd.com (release 1.7.1, docs updated
2026-04-19) so this repo is self-contained. Source URLs at the bottom.

## Linux support scope
- Linux release supports running models on the **NPU** only. Supported platforms:
  **STX and KRK** (Strix Point / Krackan). The HX 370 is STX.
- Supported model formats on Linux: CNN INT8, CNN BF16, NLP/encoder (BERT-class)
  BF16, and **LLMs via an NPU-only flow**.
- The hybrid NPU+iGPU LLM flow and the OGA hybrid path are **Windows-only**.

## LLM software stack (AMD's own framing)
- Three interfaces: high-level Python API, Server Interface (REST), native
  (OGA C++ or llama.cpp C++ headers).
- High-level Python API + Server Interface use the **Lemonade SDK**.
- Lowest level: **OnnxRuntime GenAI (OGA)** OR **llama.cpp** — and AMD states
  **llama.cpp is "only supported for iGPU"**. NPU = OGA/VitisAI EP path.

## Linux install essentials
- Ubuntu 24.04 LTS, kernel >= 6.10, Python 3.12.x, 64 GB RAM recommended.
- NPU driver = XRT base/base-dev/npu .debs + `amdxdna` plugin .deb. Account-gated:
  `RAI_1.7.1_Linux_NPU_XRT.zip` and `ryzen_ai-1.7.1.tgz` from account.amd.com.
- Verify with `xrt-smi examine` → device should show `NPU Strix`.
- Strix/Krackan NPU PCI id: `1022:17f0` (use to detect on Linux via `lspci -nn`).
- Installer creates its own venv: `./install_ryzen_ai.sh -a yes -p <PATH>/venv`.
- `model-generate==1.7.1` from `https://pypi.amd.com/ryzenai_llm/1.7.1/linux/simple/`.

## Measured NPU LLM behaviour (AMD Phi-3.5-mini NPU benchmark)
- Prefill / TTFT throughput: ~865 tok/s (NPU strong at compute-bound prefill).
- Token generation: **~17.6 tok/s** (NPU weak at memory-bound decode).
- => This is the empirical basis for ADR-0001: generative decode belongs on the
  iGPU; encode/embedding belongs on the NPU.

## Quantization
- **AMD Quark**: cross-platform PyTorch/ONNX quantizer in the RyzenAI stack.
  FP32 CNN/Transformer inputs are internally converted to BF16 and compiled via
  the BF16 flow; INT8 available where accuracy allows.

## Sources
- https://ryzenai.docs.amd.com/en/latest/index.html
- https://ryzenai.docs.amd.com/en/latest/linux.html
- https://ryzenai.docs.amd.com/en/latest/llm_linux.html
- https://github.com/amd/RyzenAI-SW  (examples / sample code)
