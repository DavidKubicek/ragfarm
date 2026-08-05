# Generative LLM Model Record — the Qwen3-VL fleet

> Supersedes the Qwen2.5-7B-Instruct / GGUF-Q4_K_M / llama.cpp-Vulkan record that
> lived here (ADR-0001, AMD box). Generation moved to vLLM on the Spark per
> ADR-0013; llama.cpp still serves the reranker (`../reranker/MODEL.md`), and the
> embedder is in `../embeddings/MODEL.md`.

**There is no longer a single LLM.** `active.json` in this directory is the
git-tracked registry of every checkpoint the deployment should hold and which of
them are bound to a vLLM **slot**; `scripts/fetch_llm.py` and
`scripts/activate_llm.py` are the only supported way to change it (ADR-0013 §2a).
Do not hand-edit `LLM_MODEL_PATH` / `LLM_SERVED_NAME` — they now live in the
generated `.env.slotN` files.

| model | quant | alias / preset | why it is here |
|---|---|---|---|
| Qwen3-VL-30B-A3B-**Instruct** | NVFP4 | `…-instruct-nvfp4` / `ragfarm-vision-instruct` | first model on the Spark; fast (76 tok/s), weaker at multi-column reasoning |
| Qwen3-VL-30B-A3B-**Thinking** | NVFP4 | `…-thinking-nvfp4` / `ragfarm-vision` | flagship. Same quant as the Instruct, so Instruct-vs-Thinking is a clean single-variable comparison |
| Qwen3-VL-30B-A3B-**Thinking** | FP8 | `…-thinking-fp8` / `ragfarm-vision-fp8` | official Qwen build; quant-fidelity reference against the NVFP4 one |
| Qwen3-VL-**32B**-Thinking | NVFP4 | `…-32b-thinking-nvfp4` / `ragfarm-vision-dense` | **DENSE**, same family and quant as the MoE — isolates MoE-vs-dense |
| Qwen3-VL-**8B**-Thinking | bf16 | `…-8b-thinking` / `ragfarm-vision-8b` | the old AMD box's quality benchmark, at bf16 rather than its Q4_K_M |

Sizes, revisions and sources are in `active.json`; `scripts/fetch_llm.py --verify`
byte-checks every one of them against the Hub.

## Baseline record — Qwen3-VL-30B-A3B-Instruct (NVFP4)

Everything measured below was measured on THIS checkpoint. Treat it as the
baseline the others are compared against, not as "the model".

| Field | Value |
|---|---|
| Repo | `ig1/Qwen3-VL-30B-A3B-Instruct-NVFP4` (community NVFP4 + vision) |
| Revision | `3c6162d5513d26f008628eebe9b4355559b4a305` (resolved 2026-08-03, ungated) |
| Served alias | `qwen3-vl-30b-a3b` at the time of measurement; the registry now assigns `qwen3-vl-30b-a3b-instruct-nvfp4`. The alias is load-bearing — it is what OWUI binds its preset to |
| Architecture | `Qwen3VLMoeForConditionalGeneration`, `model_type: qwen3_vl_moe` |
| MoE shape | **128 experts/layer, top-8 routed, 48 layers** (all MoE). ~4.7M params per expert → ~29B total, ~3B active per token |
| Quantization | `compressed-tensors`, format `nvfp4-pack-quantized`, weights 4-bit float. Vision blocks are in the `ignore` list (kept at higher precision) |
| Weights | 4 safetensors shards, 17.86 GiB on disk |
| Engine | vLLM **0.26.0** in a DEDICATED venv `.venv-vllm` (torch 2.11.0+cu130) |
| Unit | `manifests/ragfarm-vllm@N.service` (templated per slot) → `127.0.0.1:8080/v1` for slot 0 |
| Context | `--max-model-len 32768` |
| Updated | 2026-08-05 |

## MoE backend on sm_121 — MEASURED, and it contradicts ADR-0013

ADR-0013 targets `--moe-backend flashinfer` as "the b12x path". On this box:

- **`flashinfer` is not a valid flag value** in vLLM 0.26.0. The choices are
  `flashinfer_b12x`, `flashinfer_cutedsl`, `flashinfer_cutlass`, `flashinfer_trtllm`,
  `cutlass`, `marlin`, `triton`, … Passing `flashinfer` is rejected by argparse.
- **`flashinfer_b12x` DOES work on GB10** — verified 2026-08-03. All three of
  `_supports_current_device()`'s conditions pass (`is_cuda`,
  `is_device_capability_family(120)`, `has_flashinfer_b12x_moe()`), it serves, and
  it generates coherent text including Czech — not the `!!!!!` mode.
  It is however **opt-in only**: vLLM's oracle excludes it from *auto*-selection
  "until the upstream CUTLASS SM121 MMA op guard is resolved".
- **What we run by default: `FLASHINFER_CUTLASS`**, via `--moe-backend auto`, with
  `FlashInferCutlassNvFp4LinearKernel` for the linear NVFP4 GEMM. Native FP4, ranked
  ABOVE `MARLIN` — the marlin dequant fallback was never needed on this box.

Measured decode (256 tok, `ignore_eos`, single stream, 3 runs):
**`flashinfer_b12x` 75.6 tok/s** vs **`FLASHINFER_CUTLASS` 71.1 tok/s**. b12x is
~6% faster, but that is decode — which is bandwidth-bound and so the least
favourable comparison for FP4. Prefill is unmeasured. Default stays `auto`: 6% does
not justify overriding an explicit upstream "not safe to auto-select yet" on a
production box.

> **Do not conclude a FlashInfer backend is unsupported without checking PATH.**
> An earlier run here reported b12x as `does not support current device cuda` and a
> different run picked `VLLM_CUTLASS` — both were artifacts of `ninja` missing from
> the unit's PATH, since `has_flashinfer_b12x_moe()` probes by import. Backend
> selection also shifts with JIT cache state, so one log line is not a fact.

## Startup memory — the two budgets are NOT the same lever

Cold start was OOM-killed three times at a near-identical **98.3–98.6 GiB** peak
with ~13.6 GiB of swap, and the peak did not move when `--gpu-memory-utilization`,
`--max-num-batched-tokens` and `--max-num-seqs` were lowered.

- **Cold-start build memory** — `MAX_JOBS` (in `.env`). FlashInfer JIT-compiles
  kernels at runtime, and `flashinfer/jit/cpp_ext.py` only passes `-j` to ninja when
  `MAX_JOBS` is set. Unset, ninja defaults to `nproc+2` = **22 parallel CUDA
  compiles**; CUTLASS translation units are multi-GB each. That, not vLLM, was the
  98 GiB. `MAX_JOBS=4` fixes it. Only bites on a cold JIT cache
  (`~/.cache/flashinfer/`); warm starts skip compilation.
- **Steady-state memory** — `--gpu-memory-utilization` (`LLM_GPU_MEM_UTIL=0.50`).
  Unified memory means there is no private VRAM: the 0.9 default would reserve
  ~108 GB of the shared 128 GB and starve the embedder, reranker, Qdrant and the OS.
  Explicitly bounding it is correct practice here even though it was not the cause
  of the OOMs above.

Measured at `LLM_GPU_MEM_UTIL=0.50`: **KV cache 36.52 GiB = 398,832 tokens**.

`ninja` and `nvcc` must be on the unit's PATH or the engine dies with
`FileNotFoundError: 'ninja'` *after* loading all 4 shards — which looks like a slow
model load, not a missing build tool.

## Verified

- `/v1/models` advertises `qwen3-vl-30b-a3b`.
- Coherent generation (`"pong"`), explicitly not the `!!!!!` NVFP4 failure mode.
- Tool calling via `--enable-auto-tool-choice --tool-call-parser hermes`:
  returns `get_weather` with parsed `{"city": "Brno"}`.
- Cold `init engine (profile, create kv cache, warmup model)` took **813.83 s**
  (torch.compile only 9.52 s of that — the rest is FlashInfer JIT at `-j4`).

## NOT yet measured

Prefill/decode tok/s and real memory bandwidth are still unmeasured; ADR-0013's
performance numbers remain inferred. The FP8 fallback
(`Qwen/Qwen3-VL-30B-A3B-Instruct-FP8`) was never needed — it exists and is ungated
if this checkpoint ever misbehaves.
