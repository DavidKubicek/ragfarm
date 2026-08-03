# Generative LLM Model Record — Qwen3-VL-30B-A3B-Instruct (NVFP4)

> Supersedes the Qwen2.5-7B-Instruct / GGUF-Q4_K_M / llama.cpp-Vulkan record that
> lived here (ADR-0001, AMD box). Generation moved to vLLM on the Spark per
> ADR-0013; llama.cpp still serves the reranker (`../reranker/MODEL.md`), and the
> embedder is in `../embeddings/MODEL.md`.

| Field | Value |
|---|---|
| Repo | `ig1/Qwen3-VL-30B-A3B-Instruct-NVFP4` (community NVFP4 + vision) |
| Revision | `3c6162d5513d26f008628eebe9b4355559b4a305` (resolved 2026-08-03, ungated) |
| Served alias | **`qwen3-vl-30b-a3b`** — must match the `MODEL_TUNING` key in `infra/openwebui/setup_openwebui.py` or the `ragfarm-vision` preset silently fails to bind |
| Architecture | `Qwen3VLMoeForConditionalGeneration`, `model_type: qwen3_vl_moe` |
| Quantization | `compressed-tensors`, format `nvfp4-pack-quantized`, weights 4-bit float. Vision blocks are in the `ignore` list (kept at higher precision) |
| Weights | 4 safetensors shards, 17.86 GiB on disk; `LLM_MODEL_PATH` in `.env` |
| Engine | vLLM **0.26.0** in a DEDICATED venv `.venv-vllm` (torch 2.11.0+cu130) |
| Unit | `manifests/ragfarm-vllm.service` → `127.0.0.1:8080/v1` |
| Context | `--max-model-len 32768` |
| Updated | 2026-08-03 |

## MoE backend on sm_121 — MEASURED, and it contradicts ADR-0013

ADR-0013 targets `--moe-backend flashinfer` as "the b12x path". On this box:

- **`flashinfer` is not a valid flag value** in vLLM 0.26.0. The choices are
  `flashinfer_b12x`, `flashinfer_cutedsl`, `flashinfer_cutlass`, `flashinfer_trtllm`,
  `cutlass`, `marlin`, `triton`, … Passing `flashinfer` is rejected by argparse.
- **`flashinfer_b12x` does NOT work on GB10.** Forcing it fails with
  `NvFp4 MoE backend 'FLASHINFER_B12X' does not support the deployment configuration
  since kernel does not support current device cuda`. vLLM's own oracle
  (`fused_moe/oracle/nvfp4.py`) says why: *"FLASHINFER_B12X is intentionally excluded
  from auto-selection until the upstream CUTLASS SM121 MMA op guard is resolved"*.
  So the guard ADR-0013 treats as fixed by CUTLASS #3096 is, per shipping code, open.
- **What we actually run: `FLASHINFER_CUTLASS`**, selected by `--moe-backend auto`,
  with `FlashInferCutlassNvFp4LinearKernel` for the linear NVFP4 GEMM. This is
  native FP4 and ranks ABOVE `MARLIN` in the oracle's preference order, so the
  marlin fallback (which dequantizes FP4→BF16) is NOT in use.

**Backend selection depends on JIT cache state — do not read one log line as fact.**
An early run picked `VLLM_CUTLASS` only because FlashInfer could not JIT-compile
(no `ninja` on the unit's PATH); once that was fixed the higher-ranked
`FLASHINFER_CUTLASS` became available. `auto` is therefore the right setting:
it re-evaluates what is genuinely available instead of pinning a stale choice.

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
