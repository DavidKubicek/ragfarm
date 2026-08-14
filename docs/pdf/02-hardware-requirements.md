# Hardware requirements — how they scale, and what to buy

The previous chapter demonstrated what an 8-billion-parameter vision-language model does today, on an integrated GPU sharing 30 GB of LPDDR5x with the CPU. The ceiling is memory bandwidth (~130 GB/s aggregate → ~8 tokens/second decode). Below is the ladder of currently-sold hardware that removes each successive ceiling and what you get for that money.

Two things worth flagging up front:

- **Model sizes have grown dramatically and continue to.** GPT-3 (2020) shipped at 175 B parameters; ChatGPT-original (2022) collapsed the quality bar down to something in the 20-30 B range. Qwen 2.5, Llama 4, Mistral Nemo — 7 B to 30 B was the productive class through 2024. Qwen 3 (2025-2026) and DeepSeek V3 pushed the useful ceiling to 235 B mixture-of-experts. **Every emergent capability jump — reliable tool-calling, coherent multi-turn reasoning, no-hallucination retrieval, per-domain multilingual competence — happens at a specific parameter threshold.** You don't get "a little bit of" tool-calling by half-fitting a model that's a little too big; you get zero or one, depending on whether you crossed the threshold.
- **What "runs" a model changes as you cross size thresholds.** A 7 B fits on any decent workstation. A 30 B needs 24 GB VRAM. A 72 B needs 48 GB. A 235 B MoE with A22B active needs multi-GPU tensor-parallel or a fabric-connected DC-tier card. **The delta from one tier to the next is not linear** — it's an emergence step, and if the customer's workload is on the wrong side of that step, no amount of squeeze on the current tier recovers it.

This is why the hardware discussion below climbs by rung rather than picking a single "recommended spec." Where you land depends on which emergent capabilities your workload requires.

---

## Tier 0 — the original PoC box (HISTORICAL — kept as the bottom rung)

| Component | Spec | Notes |
|---|---|---|
| **Box** | AceMagic F5X (~€1300) | Mini-PC, ~500 mL volume, single 65 W USB-C PD or barrel. |
| **CPU/iGPU/NPU** | AMD Ryzen AI 9 HX 370 | Strix Point: 12 cores (4× Zen 5 + 8× Zen 5c), Radeon 890M iGPU (gfx1150), XDNA 2 NPU (50 TOPS INT8, currently unused for LLM inference on Linux). |
| **Memory** | 64 GB LPDDR5x-7500 | Shared with iGPU as UMA (BIOS allocates 30 GB nominally, up to ~48 GB visible via GTT extension). |
| **Bandwidth** | ~130 GB/s aggregate | The decode ceiling — everything downstream is bandwidth-bound. |
| **Storage** | 2× 4 TB NVMe Gen 4 | Fine for corpus + Qdrant + several models on disk. |

**What runs here well:** Qwen 2.5-7B greedy for text, Qwen 3-VL-8B-Thinking for vision, BGE-M3 embedder on CPU, bge-reranker on iGPU. **~8 tokens/s decode, ~170 tokens/s prefill.** OCR of a dense receipt takes ~40 s; a Farsi document with translation takes ~3 minutes.

**What doesn't run here:** anything above ~10 B parameters at Q4 (VRAM budget), multi-user concurrent load (single generation slot bottleneck), 32k context with `-np 4` parallelism (RAM squeeze), any 30 B-class model where the emergent-capability step lives.

**Cost profile:** ~€1300 box, ~35 W idle, ~65 W under load. Effectively free to run 24/7.

> **This box is retired.** Everything above is preserved deliberately: it is the
> bottom rung of the ladder and the only tier where we have lived with the
> consequences of being under-provisioned. The numbers in the next chapter that
> quote 8 tok/s and three-minute Farsi OCR all come from here.

---

## The one formula — how to size any of this

Everything below is arithmetic on one relationship, and we have measured the
constant rather than borrowed it.

**Decode is memory-bandwidth-bound.** Generating a token requires reading the
weights it activates out of memory. So:

```
tokens/second  ≈  efficiency × memory_bandwidth ÷ bytes_read_per_token
```

and `bytes_read_per_token` is a function of **active** parameters, not total.
That distinction is the single most important fact on this page.

### The constant, back-solved from our own hardware

Two models measured on the DGX Spark, 2026-08:

| model | active bytes/token | measured tok/s | ⇒ effective bandwidth |
|---|---|---|---|
| Qwen3-VL-30B-A3B FP8 (MoE, ~3 B active) | ~3 GB | **68** | 204 GB/s |
| Qwen3-VL-32B FP8 (dense, all 32 B active) | ~33 GB | **5.9** | 195 GB/s |

Two independent models landing within 5% of each other:
**~200 GB/s effective, about 73% of the Spark's 273 GB/s specification.** Apply
that 0.73 to any vendor bandwidth figure and the estimate holds.

### The first consequence: never buy dense

Those two models are the *same size class* and differ by **11.5×** in speed. The
MoE reads 3 GB per token; the dense one reads 33 GB. Every euro of a hardware
budget should go to bandwidth and to mixture-of-experts models, not to parameter
count. A dense 70 B on this class of machine is not slow — it is unusable, at
roughly 2-3 tok/s, which is an hour and a half for one complex answer.

### Sizing a target

Rearranged, for a model you are considering:

```
bandwidth needed  =  active_bytes_per_token × target_tok/s ÷ 0.73
VRAM needed       =  weights + KV cache + ~3 GB overhead, per resident model
```

Worked, for the first genuinely larger planner class — Qwen3-VL-235B-A22B, 22 B
active:

| quantisation | bytes/token | for 60 tok/s | for 100 tok/s | weights |
|---|---|---|---|---|
| NVFP4 | ~12 GB | ~990 GB/s | ~1.6 TB/s | ~130 GB |
| FP8 | ~22 GB | ~1.8 TB/s | ~3.0 TB/s | ~235 GB |

**Bandwidth buys latency. VRAM buys concurrency.** They are separate purchases
and a proposal that confuses them will be wrong in one direction or the other.

---

## Tier 0.5 — NVIDIA DGX Spark (the current substrate)

What the project actually runs on today, and what every measurement in this
bundle was taken on.

| Component | Spec | Notes |
|---|---|---|
| **Box** | NVIDIA DGX Spark (GB10 Grace Blackwell) | Desktop form factor. |
| **Compute** | sm_121, FP4 tensor cores | ~356 TFLOPS FP4 measured on the flashinfer b12x path. |
| **Memory** | 128 GB unified (~121.7 GiB usable) | CPU and GPU share it. There is no separate VRAM to hide in. |
| **Bandwidth** | ~273 GB/s specified, **~200 GB/s measured effective** | THE constraint. See the formula above. |
| **Networking** | ConnectX-7, 200 Gb/s dual QSFP | Two Sparks can be linked. |

**What runs here well:** two 30 B-class MoE models resident *simultaneously* —
0.70 of a 0.72 memory-budget ceiling — switchable mid-conversation, at **68-76
tok/s** each. The full RAG stack (BGE-M3 embedder, bge-reranker-v2-m3
cross-encoder, Qdrant, seven containers) alongside them. Vision, tool calling,
in-chat diagram rendering, code execution.

**What does not run here:** any dense model above ~30 B at a usable speed;
70 B-class anything; a 235 B MoE at any quantisation (NVFP4 alone is ~130 GB
against 121.7 GiB usable). Serious concurrency — the budget is spent on having
two models resident rather than on KV cache for many users.

**Why it was still the right first buy:** the entire architecture — Open WebUI,
mcpo, the MCP toolchain, hybrid retrieval, reranking, guarded actuation — is
built and proven here and redeploys onto any tier above unchanged. Only
`OPENAI_API_BASE_URL` moves. A €200 k lab does not teach you which prompt rule
breaks tool calling; a year of using this one did.

---

## Tier 1 — Developer workstation

A single-user developer machine — one person builds, tests, and demonstrates on it. Removes the tokens/second ceiling enough to iterate on prompts and tool chains without being interrupted every 40 seconds waiting for a Thinking-class model to finish.

| Component | Recommended | Alternatives (currently sold) |
|---|---|---|
| **GPU** | **NVIDIA RTX 5090 · 32 GB GDDR7 · ~1790 GB/s** (~€2 400) | RTX PRO 5000 Blackwell (48 GB GDDR7, workstation-flavor, ~€4 800); AMD Radeon PRO W7900 (48 GB, ~€3 800; ROCm-only, worse LLM ecosystem). |
| **CPU** | AMD Ryzen 9 9950X3D (16-core, 3D V-Cache) | Intel Core Ultra 9 285K (24-core). Either fine — LLM decode is GPU-side; CPU only matters for RAG orchestration + tool calls. |
| **RAM** | 128 GB DDR5-6400 (2× 64 GB, dual-channel) | 96 GB or 192 GB fine. Not the bottleneck. |
| **Storage** | 2× 4 TB NVMe Gen 5 (one system, one models/corpus) | 8 TB total roughly fits a "several models on disk + full corpus + Qdrant" without pruning. |
| **PSU / case** | 1000 W 80+ Platinum · full-tower | RTX 5090 pulls 575 W peak, wants proper airflow. |
| **Approx BOM** | **€3 500 - €4 200** (5090 build) | 5090 + 9950X3D + 128 GB + 8 TB NVMe + PSU + case + cooling. |

**What runs here well:** Qwen 3-VL-8B-Thinking at **~50-70 tokens/s decode** (~7× the PoC), the full 32k context comfortably, Qwen 3-VL-30B-A3B (a 30 B mixture-of-experts with 3 B active — fits in 32 GB Q4) at ~15-25 tokens/s, live iteration cycles feel instant.

**What still doesn't run here:** 70 B dense models at any usable quant, multi-user concurrency beyond ~3-4 parallel slots, deep-batching for high-throughput scenarios (agentic workflows spawning 20+ parallel tool calls).

**Best value point.** The 5090 is roughly the last consumer-grade rung before workstation/DC pricing kicks in. If a workload doesn't require 48 GB VRAM (i.e. you're happy with 30 B-class MoE and don't need multi-user), the 5090 is the sweet spot.

---

## Tier 2 — Small-team production server (~10-50 concurrent users)

The point at which a workstation stops being enough. One or two workstation-class GPUs in a rackmount chassis, driving a departmental or small-customer deployment. Serves both the LLM and the reranker + embedder from the same server; Qdrant colocated.

| Component | Recommended | Alternatives |
|---|---|---|
| **GPU (×1 or ×2)** | **NVIDIA RTX PRO 6000 Blackwell Max-Q · 96 GB GDDR7 · ~1800 GB/s** (~€10 500 each) | NVIDIA L40S (48 GB GDDR6, ~€8 500 — older Ada arch, less memory but well-established) · AMD MI300X (192 GB HBM3, ~€15 000 — much cheaper per GB VRAM but ROCm software friction remains a real cost). |
| **Server chassis** | Supermicro AS-2124GQ-NART or Dell PowerEdge R760xa | 2U or 4U depending on GPU count; needs PCIe 5.0 x16 per card and 800 W+ per card. |
| **CPU** | 1× AMD EPYC 9354P (32-core, PCIe 5.0 lanes) | Or Intel Xeon 6 Granite Rapids equivalent. Single-socket is enough. |
| **RAM** | 512 GB DDR5-4800 ECC RDIMM | For Qdrant + FS cache + inference server + headroom. |
| **Storage** | 4× 8 TB NVMe Gen 5 U.2/E1.S (RAID 10) | ~16 TB usable — corpus + Qdrant snapshots + N models on disk. |
| **Networking** | 2× 25 GbE (or 100 GbE if fronting a bigger cluster) | For OpenNebula integration + client-facing traffic. |
| **PSU** | 2× 2000 W redundant | RTX PRO 6000 = 600 W each; leave margin. |
| **Approx BOM** | **€18 000 - €25 000** (single-GPU) · **€28 000 - €35 000** (dual-GPU) | Chassis + CPU + RAM + storage + GPU(s) + PSUs. |

**What runs here well:** Qwen 3-72B-Instruct at Q4 (fits in 96 GB with room), Qwen 3-VL-32B-Thinking at BF16 (fits in one 96 GB card), 8-16 parallel inference slots, sub-second first-token latency, real customer-facing SLAs. Full ADR-0008 reranker at BF16 not Q4.

**What still doesn't run here:** Frontier-class 405 B+ dense models, or 671 B MoE like DeepSeek V3, or multi-tenant workloads where a hundred concurrent users each expect an interactive response.

**Best fit:** internal-tool deployments (single company, few dozen active users), customer-facing pilots, embedded-in-product AI features for SMEs.

---

## Tier 3 — DC-tier serious production (100s of concurrent users, or a foundation model to serve)

At this rung we're in HGX/DGX territory — GPU-fabric-connected servers with NVLink, deployed in ones or twos.

| Component | Recommended | Alternatives |
|---|---|---|
| **GPUs** | **NVIDIA HGX H200 · 8× H141 · 1128 GB HBM3e total · 4.8 TB/s per card · NVLink 5** (~€300 000 for a bare HGX baseboard alone) | AMD MI325X 8-way system (~€250 000, comparable HBM3e VRAM, PCIe fabric not NVLink); NVIDIA B200 HGX (Blackwell DC — ~€400 000, another generation newer). |
| **Server platform** | Supermicro SYS-821GE-TNHR or NVIDIA DGX H200 | 8U to 10U form factor; requires 400 V 3-phase power, liquid cooling, dedicated rack. |
| **CPU** | 2× Intel Xeon Platinum 8592+ (or dual AMD EPYC 9754) | 128 cores total; provides PCIe lanes + RAM channels for orchestration. |
| **RAM** | 2 TB DDR5-4800 ECC (24× 96 GB) | Feeds the CPU-side vLLM prefetch buffers + KV-cache spillover. |
| **Storage** | 60 TB NVMe Gen 5 (RAID 10) + separate parallel filesystem for the model catalogue | For continuous training-data ingestion in addition to inference. |
| **Networking** | 2× 400 GbE ConnectX-7 (or InfiniBand NDR) | For multi-node scaling and client-facing throughput. |
| **Power** | ~10 kW at load | Requires proper DC facility. |
| **Approx BOM** | **€400 000 - €600 000 for a single HGX/DGX server** | Ready-to-run; excludes rack, PDU, cooling. |

**What runs here well:** Qwen 3 Coder 480B-A35B-Instruct at BF16 (needs multi-GPU tensor-parallel), Qwen 3-VL-235B-A22B, DeepSeek V3 671B, Llama 4 Maverick 400B — the frontier open-weight tier. Hundreds of concurrent users. Deep-batching. Fine-tuning by LoRA/QLoRA. Continuous embedding backfill.

**What doesn't run here:** frontier proprietary models where the weights aren't yours to load. Otherwise there is no ceiling in the currently-shipping open-weight space.

**Best fit:** the point at which the product is the AI, not "a product with AI in it." Enterprise SaaS pivots, foundation-model resellers, sovereign-AI national initiatives.

---

## The trajectory that matters for planning

The parameter-count / VRAM / bandwidth requirements have grown roughly **2×-3× per year** across every open-weight release wave since 2022, with no plateau in sight. The Qwen 2.5 → Qwen 3 → Qwen 3-VL step alone doubled the useful-model class from 7 B to 32 B; the next step will likely reach 72 B-dense or 200 B-MoE as the "productive floor." Hardware planned today for a 3-year lifecycle should be provisioned with **2× headroom** on VRAM and bandwidth, not sized to today's model.

Consequences for this project's roadmap:

- **Tier 0 (PoC)** is a genuine on-prem demonstration platform and will remain useful for prompt-engineering, corpus ingestion tuning, and travel demos. It will not track the model frontier.
- **Tier 1 (single dGPU workstation)** is what unlocks Qwen 3-VL Instruct + Thinking at interactive latencies + the 30 B-A3B MoE class. This is the *minimum plausible dev environment* going forward. Any customer-facing demo run on Tier 0 hardware will visibly lag against expectations set by cloud AI.
- **Tier 2 (workstation-class rack GPU)** is the first customer-deployable tier — the point at which a company can host their own instance without knowing what they're doing at the infrastructure level. The whole ragfarm architecture (Open WebUI + mcpo + MCP toolchain + hybrid retrieval + rerank + guarded actuation) is designed to redeploy unchanged onto this tier; only the `OPENAI_API_BASE_URL` moves.
- **Tier 3 (DC HGX)** is the tier where a hosted-AI product becomes competitive with commercial offerings on both quality and speed. Not required for the current use case; noted for the roadmap.

The single most important decision to defer or make: whether Tier 1 (single-workstation dGPU) makes sense as an interim step before jumping to Tier 2 for customer deployment. On this exact project, Tier 1 unblocks development, and Tier 2 unblocks the customer pilot. The €4 000 vs €25 000 delta is small compared to the year of engineering time each unlocks.

---

## The concrete ask, if you have one conversation to make it in

Two asks come up in practice: *make the current models three times faster*, and
*run a planner that is not 30 B-class*. They cost very differently.

**3× on the current MoE** (68 → ~200 tok/s) needs `3 GB × 200 ÷ 0.73` ≈
**820 GB/s**. That is one workstation card. It is the cheap ask.

**A real planner** is what sets the bill, because the 235 B-A22B class reads
seven times more per token.

| ask | hardware | what it buys |
|---|---|---|
| **Development unblock** | 1× RTX PRO 6000 Blackwell · 96 GB · ~1.8 TB/s · ~€10-12 k | 3× on the current models, a 120 B-class MoE. Not the 235 B. |
| **The planner** | 2× RTX PRO 6000 · 192 GB · ~€20-25 k GPU, ~€35-40 k built | 235 B-A22B at NVFP4 (~130 GB) with room for KV, near **110 tok/s** — past the 3× ask on a model 7× the active size. |
| **Customer pilot** | 2× H200 · 282 GB · 4.8 TB/s · NVLink · €70-100 k | The same planner at full FP8, ~160 tok/s, and enough KV for real concurrency. |

Three honesty flags for whoever quotes this:

- **Only the 200 GB/s is ours.** Every other bandwidth figure is a specification.
  Apply the measured 0.73 efficiency and the estimates hold, but they are
  estimates.
- **Prices are indicative, not quotes.** The ratios matter more than the
  absolutes; get real numbers before committing.
- **Multi-GPU is not free.** A PRO 6000 pair talks over PCIe, not NVLink, so
  tensor-parallel on a 130 GB model gives some of that 110 tok/s back. H200s do
  not have that problem, and that is the argument for the more expensive tier if
  the planner is the point.
