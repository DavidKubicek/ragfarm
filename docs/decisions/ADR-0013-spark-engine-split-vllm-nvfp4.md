# ADR-0013 — Spark engine split: vLLM + NVFP4, and the sm_121 FP4 reality
Author: David Kubicek (david.kubicek@eywo.cz)

Status: ACCEPTED (2026-08-03) for the *architecture*. This ADR **supersedes
ADR-0001** (the Ryzen APU role-split), which was specific to the AMD Ryzen AI 9
HX 370 development box now being replaced by an NVIDIA DGX Spark (GB10) as the
production target. Numeric performance claims below are marked **MEASURE** where
they are inferred rather than observed; they must be replaced with real numbers
from the box.
Date: 2026-08-03
Supersedes: ADR-0001 (iGPU=LLM / NPU=embeddings split — that hardware no longer
exists in the target topology).
Amends: ADR-0002 (its BGE-M3 **model** choice stands unchanged and its rejection of
the NPU/Quark path was already correct; only the **host** moves, CPU → CUDA),
ADR-0008 (the cross-encoder stays, its *serving process* is re-placed),
ADR-0009 (vision support stays, its *mechanism* changes from llama.cpp `--mmproj`
to vLLM-native multimodal).
Note on ADR-0002: despite its filename, ADR-0002 is **not** a "use Quark" decision
— it is the ADR that *rejected* the NPU/Quark embedder and moved BGE-M3 to the
CPU. Nothing about Quark needs retiring here; it was already dead.
Unaffected: ADR-0003 (agent layer), ADR-0004 (actuation), ADR-0005 (conventions),
ADR-0006 (manifest), ADR-0007 (chunking), ADR-0010/0011/0012 — all of which are
deliberately serving-engine agnostic. That agnosticism is the point of ADR-0003's
"retrieval is the durable, HW-agnostic layer" and it is why this migration touches
the serving plane and almost nothing else.

## Context

The development box was an AMD Ryzen AI 9 HX 370 (Strix Point). ADR-0001 split it
by role because of two hard AMD-on-Linux constraints: the hybrid NPU+iGPU LLM flow
is Windows-only, and llama.cpp reaches the iGPU but never the NPU. Neither
constraint exists on the new target. Both ADR-0001's decision and ADR-0002's Quark
toolchain are therefore dead letters here, not decisions to be re-litigated.

The production target is an **NVIDIA DGX Spark**:

| property | value | note |
|---|---|---|
| chip | GB10 Grace Blackwell Superchip | |
| compute capability | **sm_121** (12.1) | **not** sm_120 — this distinction is load-bearing, see below |
| unified memory | 128 GB | CPU+GPU share it; no discrete VRAM budget to juggle |
| memory bandwidth | ~273 GB/s claimed, ~221 GB/s widely reported | **MEASURE** — every model-sizing decision keys off this number |

The single most important architectural fact carries over from
`docs/hardware/NVIDIA-Spark.md` unchanged: **decode is memory-bandwidth-bound**.
Choose models by *active* parameters, not total. That is what makes an MoE the
right shape here and a dense 70B the wrong one.

### The sm_121 FP4 trap — the finding that shapes this ADR

The intuitive plan was "Blackwell has FP4 tensor cores, so run NVFP4 and watch it
crush Q4_K_M." The hardware reality on *this specific part* is narrower, and
getting it wrong produces either garbage output or a silent fallback that
underdelivers while looking fine:

1. **GB10 (sm_121) has no usable native FP4 compute path.** FlashInfer-TRTLLM's
   FP4 MoE kernels gate on `SM100+` and reject 12.x consumer Blackwell; the
   CUTLASS FP4 GEMM they fall back to fails on sm_120/sm_121. The working path
   for MoE layers is **Marlin W4A16**: NVFP4 weights are dequantized FP4→BF16 and
   the matmul runs in **BF16**.
2. **Therefore `--moe-backend marlin` is mandatory**, not an optimization. Without
   it, the documented failure mode is a model that loads cleanly and then emits
   streams of `!!!!!` — a silent numerical failure, not a crash. Any agent
   debugging "the model outputs garbage" must check this **first**.
3. **NVFP4's win here is bandwidth and capacity, not tensor-core throughput.**
   4-bit weights mean ~4× less to stream per token versus BF16. Since decode is
   bandwidth-bound, that *is* the dominant win — it simply does not come from the
   FP4 ALUs, and prefill compute still happens in BF16.
4. **sm_121 NVFP4 kernels require a recent vLLM.** Stock upstream builds working
   sm_121 NVFP4 kernels from **v0.19.0** onward, with a native SM120/121 CUTLASS
   NVFP4 GEMM landing in vLLM PR #40082 (merged 2026-05-20). Community model cards
   that say "tested on v0.13.0" predate that work. **Pin ≥ 0.19.0; prefer current
   stable.**

Consequence for expectations: NVFP4 is still the right target — but the honest
pitch is **"same footprint, better numerics than Q4_K_M, and MoE-shaped decode"**,
not "FP4 tensor cores make it fly." Recording this here so nobody re-derives the
optimistic version and then benchmarks a disappointment.

### Order-of-magnitude sanity bound (**MEASURE** — replace with real numbers)

For a 30B-A3B MoE at 4-bit: ~3B *active* params × ~0.5 B/param ≈ **~1.5 GB streamed
per token**. At ~221 GB/s that is a theoretical ceiling around ~140 tok/s; real
decode will land well below it after attention, KV traffic, and Marlin's
dequantization overhead. The same 30B *dense* at 4-bit would stream ~15 GB/token —
roughly an order of magnitude worse. That gap, not FP4, is the reason to go MoE.

## Decision

### 1. vLLM replaces llama.cpp as the generative serving engine

`llama-server` served the PoC well and its OpenAI-compatible + `--jinja`
tool-calling was exactly right for ADR-0003's agent layer. It is retired on the
Spark for three reasons: NVFP4 checkpoints ship as **safetensors** for
vLLM/TensorRT-LLM (NVFP4-safetensors→GGUF conversion is reported unreliable);
vLLM's continuous batching is the concurrency story this box needs; and vLLM
handles Qwen3-VL multimodality natively, removing the `--mmproj` projector
plumbing that ADR-0009 had to build around.

- Serving: `vllm serve` on `127.0.0.1:8080`, OpenAI-compatible — **the endpoint
  contract is unchanged**, which is why the whole agent/MCP/OWUI layer above it
  needs no rework.
- Tool calling: `--enable-auto-tool-choice --tool-call-parser hermes`. This is the
  functional replacement for llama.cpp's `--jinja`; ADR-0004's typed-parameter
  actuation contract is unaffected.
- **Mandatory:** `--moe-backend marlin` (see the trap above).
- **Mandatory:** vLLM **≥ 0.19.0**.
- Context must be capped explicitly (`--max-model-len`); the 30B-A3B does not fit
  at full context alongside the reranker and embedder on shared unified memory.

### 2. Qwen3-VL-30B-A3B (NVFP4) is the primary model; tuning happens on it

The demo/tuning target is the **vision** model, not a text-only one. Qwen2.5-7B is
retired as a *default* — kept only as a fallback deck reference in
`docs/prompts.md`. Candidates, in preference order:

| checkpoint | provenance | note |
|---|---|---|
| `ig1/Qwen3-VL-30B-A3B-Instruct-NVFP4` | community | the VL + NVFP4 combination we actually want; **verify on the box** |
| `Qwen/Qwen3-VL-30B-A3B-Instruct-FP8` | official Qwen | FP8 fallback if the NVFP4 card misbehaves; larger footprint, fewer unknowns |
| `nvidia/Qwen3-30B-A3B-NVFP4` | official NVIDIA | text-only; the reference for NVFP4 *packaging*, not our vision target |

Per the standing model-format policy: fetch the **latest** revision and prefer the
fastest-loading weight format, excluding only unused/redundant files. NVFP4
checkpoints are safetensors, which satisfies that on both counts.

**A single-model preset no longer suffices.** Because presets are now per-model and
per-alias, OWUI configuration moves to an **alias-keyed** structure
(`infra/openwebui/setup_openwebui.py`), so system prompt and sampler parameters
travel with the model instead of being global constants. Idempotency of that
script is preserved.

### 3. Cross-encoder reranker stays on llama.cpp, at lowered priority

ADR-0008's decision (cross-encoder replaces MMR) is untouched. Only its placement
is decided here:

- **Keep `bge-reranker-v2-m3` at full quality.** Shrinking or further quantizing it
  to buy bandwidth would trade retrieval precision for latency — the wrong trade
  in a system whose entire value proposition is grounded, correct answers.
- **Keep it on a llama.cpp CUDA `llama-server --reranking` on `:8081`.** It is
  small, the `/reranking` response shape is what `services/rag-retrieval/server.py`
  already consumes (raw logit → sigmoid → [0,1]), and moving it to vLLM's scoring
  API would change that contract for no benefit.
- **Lower its scheduling priority** so the interactive LLM wins contention on the
  shared memory pipe. Reranking is a bounded burst inside one query; the LLM's
  decode is the user-visible latency.

### 4. Embedder moves to CUDA; contention is accepted because it is bursty

BGE-M3 moves from CPU to CUDA on `:8090/embed`. ADR-0002's model choice is
unchanged — only its host. It is prefill-shaped and gains the most from the GPU.
Contention is tolerable because the load profile is asymmetric: bulk ingestion is
a batch job that can run when nothing interactive is happening, while at query
time it embeds a single short string — negligible against the LLM.

The dense+sparse output contract (1024-dim dense + sparse token weights, the shape
`services/rag-retrieval/server.py` consumes) must be **byte-for-byte identical**
after the move. A CUDA-vs-CPU numerical drift here would silently invalidate the
existing Qdrant collection, because stored vectors were produced by the CPU build.
Gate this: re-embed a known string on both and compare, or plan a full re-ingest.

### 5. Three processes share one memory pipe — this is the standing constraint

vLLM (LLM) + llama.cpp (reranker) + BGE-M3 (embedder) all draw on the same
~221–273 GB/s. There is no separate VRAM to hide in. Every future "just add a
model" proposal must be argued against this budget. The second-Spark-over-
ConnectX-7 path in `docs/hardware/NVIDIA-Spark.md` is the escape hatch if
isolation becomes necessary.

### What is explicitly NOT changing

Recorded because the migration's blast radius is smaller than it looks, and an
agent should not "helpfully" rewrite these:

- The OpenAI-compatible endpoint contract on `:8080` — hence OWUI, mcpo, and every
  MCP service are untouched.
- `services/rag-retrieval/server.py` retrieval policy, including the ADR-0010 §1
  gate shipped 2026-07-31.
- Qdrant, the ADR-0006 content-addressed manifest, the ingester and the frozen
  `xlsx_tables.py` parser.
- ADR-0004's actuation contract.

## Consequences

Positive:
- Model capability jumps from an 8B dense to a 30B-A3B MoE at comparable decode
  cost, because only ~3B params are active per token.
- NVFP4 numerics beat Q4_K_M at equivalent footprint — a genuine quality win even
  with FP4 compute unavailable.
- vLLM's continuous batching makes concurrent users viable, which llama.cpp's
  `--parallel` slots only approximated.
- Native multimodality removes the `--mmproj` projector-pairing logic and the
  auto-detect heuristics built around it.
- 128 GB unified memory removes the constant VRAM-budget anxiety of the iGPU box.

Negative / cost:
- The whole model lifecycle tooling (`fetch-llm.sh`, `activate-llm.sh`,
  `llama-launch.sh`, `lib-models.sh`, `manifests/ragfarm-llama.service`) is
  GGUF-shaped and needs reworking for safetensors + vLLM.
- Two serving engines now coexist (vLLM for generation, llama.cpp for reranking),
  which is more surface than one.
- The Marlin fallback means we are paying NVFP4's accuracy benefit without its
  speed benefit; if a future vLLM/driver combination enables real FP4 GEMM on
  sm_121, that is free performance we should re-test for.
- `--moe-backend marlin` is a silent-failure landmine for anyone who omits it.

Neutral / open:
- All numbers in this ADR are inferred. The `MODEL.md` auto-benchmark hook (prefill
  and decode tok/s captured on every model activation) exists precisely to replace
  them with measurements.

## Open questions

1. **Real bandwidth.** 221 vs 273 GB/s — measure it. Model-sizing guidance in
   `docs/hardware/NVIDIA-Spark.md` depends on which is true.
2. **NVFP4 vs FP8 for the VL model.** Given MoE runs Marlin W4A16 anyway, does
   FP8 (official Qwen checkpoint, fewer unknowns) actually lose to community
   NVFP4 on this box? Decide by benchmark, not by bit-width.
3. **Does the community `ig1` NVFP4 VL checkpoint work at all on sm_121?** It was
   published against vLLM v0.13.0, before the sm_121 kernel work. First gate on
   the box.
4. **Reranker de-prioritization mechanism.** `nice`/cgroup weight/CUDA stream
   priority/MPS — which actually moves the needle on a unified-memory part is
   unmeasured.
5. **Does the ADR-0010 §1 floor need re-calibration under a different model?** The
   cross-encoder is unchanged, so scores should be stable — but the calibration
   was never completed on the old box either, so it is an open item regardless.

## References
- `docs/hardware/NVIDIA-Spark.md` — the hardware record this ADR builds on.
- ADR-0001 (superseded), ADR-0002 (retired), ADR-0008 (reranker), ADR-0009 (vision).
- `BUILD_STATE.md` steps 01–02 — the executable form of this decision.
- vLLM issue #43906 (SM_121 MoE backend gating), vLLM PR #40082 (SM120/121 CUTLASS
  NVFP4 GEMM), NVIDIA Developer Forums "State of native NVFP4 kernel support on
  GB10".
