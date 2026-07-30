# Hardware: NVIDIA DGX Spark — GB10 Grace Blackwell (prod/dev inference box)

Status: ORDERED (2026-07-30), awaiting delivery (~next week). Treat the specs below
as given for architecture planning; transcribe the as-delivered firmware/OS strings
into the "As-delivered record" section once the unit lands (sibling to how
`bios-f5x.md` records the F5X). Until then we soldier on the AceMagic F5X (see
`bios-f5x.md`); this box replaces it as the inference plane.

## What it is

- SoC: **NVIDIA GB10 Grace Blackwell Superchip** — 20-core Arm CPU (10× Cortex-X925 +
  10× Cortex-A725, MediaTek IP) + Blackwell GPU (5th-gen Tensor Cores), joined by
  NVLink-C2C (coherent, not PCIe).
- Memory: **128 GB LPDDR5X unified**, coherent across CPU and GPU.
- **Memory bandwidth: ~273 GB/s** — shared CPU+GPU. **This is THE constraint.** See
  "The bandwidth wall" — it dictates model choice more than the FLOPs do.
- Compute: up to **1 petaFLOP FP4 (sparse)**. FP4/NVFP4 native.
- Networking: integrated **ConnectX-7, 200 Gb/s** (dual QSFP). Two Sparks link to act
  as one for ~256 GB / models up to ~405 B params in FP4.
- Storage: up to 4 TB NVMe (internal 2242 Gen5 is convenient but modest; external
  NVMe-oF over RDMA is the path for heavy sustained I/O).
- Form/power: ~1.1 L, ~240 W, ~$3,999 (Founder's Edition; OEM variants vary).
- Software: DGX OS, CUDA/cuDNN/TensorRT, containers preinstalled.

## The bandwidth wall — read this before choosing models

Decode is **memory-bound**, not compute-bound, on this box. Per-stream token rate ≈
memory-bandwidth ÷ bytes-read-per-token, and bytes-read is a function of **active**
parameters, not total. At ~273 GB/s that means:

- **Dense 70B @ Q4** (~40 GB read/token) → ceiling ~6–7 tok/s single-stream. Painful,
  and no batching trick fixes single-stream latency on a bandwidth wall.
- **120B-class MoE** (e.g. GPT-OSS-120B, ~5 B active) → measured ~40 tok/s on
  comparable GB10-class hardware, because only the active experts are read per token
  even though all ~60 GB stay resident.
- **Qwen3-30B-A3B** (3 B active) → ~483 tok/s at batch 64 (FP8) in NVIDIA's own
  numbers — real concurrency, the agent workhorse.

**Rule: on this box, choose models by *active* parameter count.** A dense 70B is the
worst thing to put here — slower than the 120B MoE *and* it eats more of the latency
budget. Keep dense-70B ambitions for the RTX PRO 6000 box (96 GB GDDR7, far higher
bandwidth) where dense models breathe.

## Sizing decision for ragfarm (brain + agents)

- **Brain: a 120B-class MoE** (e.g. GPT-OSS-120B) — better instruction-following /
  planning than the 7B, and *faster* here than a dense 70B because of low active
  params. This is the planner brain for ADR-0011.
- **Agents: Qwen3-30B-A3B** (or similar low-active MoE) — high concurrency for the
  thin agents.
- **Avoid dense 70B on this box.** ("70B + 30B" only makes sense if the 70B is MoE; a
  dense 70B loses on both latency and budget.)
- Both brain + agents + embedder + reranker fit in 128 GB, but they **contend for the
  same 273 GB/s** under concurrent load. Two consequences:
  - Run **each model as its own serving process** (separate vLLM/SGLang instances
    against the shared GPU) so a reload/crash of one doesn't take the others down, and
    KV-cache pressure is reasoned per model.
  - If contention bites in prod, the clean fix is a **second Spark over ConnectX-7**
    (256 GB, hard bandwidth isolation — brain on one, agents on the other), *not* a
    bigger single model.

## Serving stack

- For concurrent agentic serving, **vLLM or SGLang** > llama.cpp (llama.cpp is what
  the F5X runs today; it stays fine for single-stream and for the reranker). Per
  ADR-0003 the durable layer is HW-agnostic — model + rerank device swap without
  re-architecture; only endpoints/process topology change.
- CUDA rerank makes ADR-0008's ~1.9 s iGPU cross-encoder ~0.2 s, which is what makes
  ADR-0010's floor calibration (a wide query sweep) cheap.
- VL forward passes (ADR-0009 vision; ADR-0012 caption/OCR ingest) are compute-shaped,
  so they use this box well — unlike bandwidth-bound decode.

## As-delivered record (fill in when the unit lands)

```
Model / SKU:        (Founder's Edition / OEM?)
DGX OS version:     
CUDA / driver:      
Serial / asset tag: 
Networking:         ConnectX-7 firmware, link mode, peer Spark? (Y/N)
Storage:            internal NVMe size + any NVMe-oF target
Power/thermal:      observed under sustained load
```

## TODO for the agent (on delivery)
- Transcribe the as-delivered strings above.
- Confirm serving stack choice (vLLM vs SGLang) with a concurrency benchmark: brain
  MoE + N agent streams, measure per-stream tok/s under contention (this validates the
  "separate process per model" decision and the single-vs-dual-Spark question).
- Re-run ADR-0008 rerank latency on CUDA; record the number; then run ADR-0010's floor
  calibration sweep.
- Record the actual brain + agent GGUF/model choices once picked, and their resident
  memory + measured tok/s, back into this file.

## References
- ADR-0001 (engine split), ADR-0003 (HW-agnostic durable layer), ADR-0008 (rerank
  device), ADR-0009 (vision engine), ADR-0010 (floor calibration wants CUDA rerank),
  ADR-0011 (planner/agent model sizing lives on this box).
- `bios-f5x.md` — the current F5X dev box this one supersedes as the inference plane.
