# Reranker — model record (ADR-0008)

| Field | Value |
|---|---|
| Model | `BAAI/bge-reranker-v2-m3` — cross-encoder, XLM-RoBERTa-large family, sibling of BGE-M3 |
| Revision | latest, not pinned; converted to f16 GGUF locally |
| Backend | **llama.cpp `--reranking`, CUDA** (GB10, sm_121) |
| Weights | `models/reranker/bge-reranker-v2-m3/bge-reranker-v2-m3-f16.gguf` — **1.08 GiB**, gitignored |
| Path from | `.env` → `RERANK_MODEL_PATH` |
| Endpoint | `POST http://127.0.0.1:8081/reranking` |
| Unit | `manifests/ragfarm-reranker.service` · `Nice=5` · enabled at boot |
| GPU memory | **918 MiB** measured while loaded |
| Latency | **~250 ms** warm for a 40-candidate pool; **~1.6 s** on the first call after idle (was ~36 s on CPU) |
| Output | raw logit per (query, document); `rag-retrieval` applies sigmoid → [0,1] |
| Role | stage 4 of the retrieval path — reranks the RRF-fused pool |
| Languages | 100+, including Czech |
| Updated | 2026-08-15 |

## Where it sits

Stage 4 of seven in the query-time retrieval path, and the expensive one. See the
retrieval-path diagram in the README, or `assets/src/retrieval-path.typ` for the
source.

**It is never shrunk to buy latency.** It decides whether the model sees the right
passages at all; trading retrieval precision for speed is the wrong way round for
this system.

`Nice=5` is deliberately mild — the reranker should lose contention to the
interactive LLM, but we are single-user, so the penalty is small. Revisit under
real concurrent load.

## Why a separate llama.cpp server

Different model, different purpose, and no first-party code: the unit file is the
whole deliverable. llama.cpp gives a small cross-encoder server with no Python
runtime and ~0.9 GB of GPU — replacing it with a second vLLM instance would cost
more memory than the reranker uses. It is the **only** thing still on llama.cpp;
generation moved to vLLM in ADR-0013.

## Problem → command

| symptom | do this |
|---|---|
| Is it up? | `scripts/stack.sh status` — the `reranker` row |
| Retrieval feels slow | `curl -s http://127.0.0.1:8081/health`, then check `_timing_ms.rerank` in a `search_corpus` response |
| Wrong passages winning | `.venv/bin/python scripts/rag_pool_inspect.py` — shows the pool before and after this stage |
| Weights missing after a clone | `scripts/fetch-encoder.sh` — downloads and converts to GGUF, writes `RERANK_MODEL_PATH` |
| Re-fetch / re-convert | `scripts/fetch-encoder.sh --force` |
| Other embedder+reranker pairs | `scripts/fetch-encoder.sh --list` |
| Service dead after a rebuild | `llama-server` missing → build it, `infra/llama/README.md` |
| Restart just this service | `sudo systemctl restart ragfarm-reranker` |

## Two flags that are load-bearing

**`-b 4096 -ub 4096`.** Reranking scores each (query, document) pair in *one*
physical batch, so the batch must exceed the longest pair. The default 512 is
below our ~480-word chunks (~600-700 tokens). Too small and pairs are silently
mis-scored. The old CPU FlagReranker truncated to 512 instead; llama.cpp scores
the full text, which is strictly better.

**`-ngl 999`.** All layers on the GPU. On CPU the same pool took ~36 s — the
single change that moved retrieval from unusable to faster than most hosted RAG.

## Pairing

The embedder and reranker must stay a compatible pair. `scripts/fetch-encoder.sh
--list` shows the known-good combinations. Current partner: BGE-M3, see
`models/embeddings/MODEL.md`.

## See also

`docs/decisions/ADR-0008` (why a cross-encoder at all) ·
`docs/decisions/ADR-0010` (the gate that consumes these scores) ·
`man docs/man1/stack.1`
