# ADR-0008 — Cross-encoder re-ranking replaces MMR in search_corpus
Author: David Kubicek (david.kubicek@eywo.cz)

Status: PENDING (reopened 2026-07-21; latency resolved the same day). The
ranking-**quality** decision is validated and staying (the cross-encoder fixed the
MMR failure). The **latency** blocker that reopened this — ~36 s CPU rerank — is
**resolved**: the reranker moved to its own GPU `llama-server --reranking` on the
Vulkan iGPU (**~36 s → ~1.9 s**, see "Measured latency" → Resolution, and the
Architecture section). Still open, hence PENDING: `RAG_MIN_SCORE` calibration on
accumulated dumps. Promote to ACCEPTED once that lands.
Date: 2026-07-20 (reopened + latency resolved 2026-07-21)
Builds on: ADR-0002 (BGE-M3 dense+sparse embedder, CPU model host on :8090),
ADR-0003 (rag-retrieval owns corpus RAG in the MCP layer), ADR-0007 (section-aware
chunking + broad-in/narrow-out retrieval; this ADR supersedes its §2 **MMR** step).
Scope: the **ranking policy** in `services/rag-retrieval/server.py` and one new
`/rerank` endpoint on `services/embedder/server.py`. Chunking (`ingester.py`),
the embedder's `/embed`, the manifest/sync layer, and Qdrant are untouched.

## Context

ADR-0007 §1 shrank chunks from page-sized slabs to sentence-scoped prose and
one-row-per-record tables. ADR-0007 §2 kept an **MMR** re-ranker whose diversity
term was designed for the *old* large chunks. On the new small chunks that term
inverted from helpful to harmful — it reads N distinct records that share a
template (e.g. every `... Firma: EPC ...` contact row) as "redundant" and evicts
the real answers in favour of topically-different noise. The full trace, the
measured scores, and the "grep would have done better" symptom are recorded in
ADR-0007's appended **"Learned failure mode"** section.

ADR-0007's own open question #1 already named the remedy: *"a cross-encoder
re-ranker over the fused pool."* This ADR adopts it.

Why a cross-encoder is the right instrument here:
- It scores each `(query, chunk)` pair **directly** (query and document attend to
  each other in one forward pass), rather than comparing pre-computed vectors. So
  it judges *actual* relevance instead of aggregate topicality, and it has **no
  diversity term** to fight — N distinct same-template records each keep their true
  score.
- `BAAI/bge-reranker-v2-m3` is the sibling of our embedder: same BGE-M3 /
  XLM-RoBERTa-large family, multilingual (Czech ✓), and the pairing BAAI ships for
  "retrieve with bge-m3, rerank with bge-reranker-v2-m3."

## Decision

1. **Broad-in, wider.** Fuse a larger hybrid RRF candidate pool
   (`RAG_PREFETCH`/`RAG_CANDIDATES` default **20 → 40**) so every same-template row
   (e.g. all EPC contacts) is present before ranking.
2. **Cross-encoder re-rank the pool** with `bge-reranker-v2-m3`. Each candidate's
   `text_clean` (embedding text; falls back to `text`) is scored against the query,
   `normalize=True` (sigmoid → [0,1]).
3. **Floor, then top-k.** Drop hits below `RAG_MIN_SCORE` (default **0.0** = keep
   all, until calibrated on real dumps — observed separation is real ≈ 0.13–0.95 vs
   junk ≈ 0.002, so a future floor of 0.01–0.1 is safe), then return the top `k`
   (default **5 → 8**; raise for "list all …" queries). Same-section window
   expansion (ADR-0007 §2) is unchanged and runs after selection.
4. **MMR retired, not deleted.** The reranker is the default (`RAG_USE_RERANKER=1`).
   `RAG_USE_RERANKER=0` restores the exact ADR-0007 MMR path, kept **only** so the
   "MMR helps / hurts" claim can be settled with data rather than asserted.
5. **Score matches order.** The returned `score` is now the reranker's normalized
   relevance on the default path (the RRF score on the legacy MMR path), so results
   are always sorted by the number shown — fixing the ADR-0007 defect where the
   printed RRF score disagreed with the post-MMR order.

### Architecture — a dedicated GPU reranker; policy stays in rag-retrieval

Per ADR-0007's scope, all ranking **policy** (pool size, sort, floor, `k`, the
rerank-vs-MMR switch) lives in `rag-retrieval/server.py`. Only **pair-scoring** is
delegated to a reranker service, called over HTTP.

**The reranker is a dedicated `llama-server --reranking` on the iGPU** (`:8081`,
Vulkan) — the same engine and device as the LLM (`manifests/ragfarm-reranker.service`,
`models/reranker/MODEL.md`). rag-retrieval POSTs candidate texts to `RERANK_ENDPOINT`
(`:8081/reranking`); llama.cpp returns a **raw logit** per pair, which rag-retrieval
`sigmoid`s to the [0,1] score contract (byte-identical to the earlier FlagReranker
`normalize=True`). The GGUF (`models/reranker/bge-reranker-v2-m3/…-f16.gguf`, gitignored)
is downloaded + converted from the HF weights by `scripts/fetch-encoder.sh` (which
`deploy.sh` calls), via llama.cpp's `convert_hf_to_gguf.py`.

**Superseded design (2026-07-20 → 2026-07-21).** The reranker was first co-hosted as a
`POST :8090/rerank` sub-endpoint on the CPU embedder service (FlagReranker, torch), to
reuse the existing CPU model host and keep rag-retrieval thin. Correct on the "avoid
container bloat / avoid ROCm" axis, but **wrong on latency** — the CPU cross-encoder
took ~36 s/query. Moving it to llama.cpp's native `--reranking` on the **Vulkan iGPU**
(no ROCm — gfx1150 is unofficial per ADR-0001; no torch) cut that to ~1.9 s on VRAM
the 7B leaves idle. The embedder and reranker now share nothing (different model,
device, purpose), so the embedder is embeddings-only again, and the reranker record
moved to `models/reranker/MODEL.md`. A reranker is a single-pass encoder, so there is
no autoregressive KV cache to share between the two `llama-server` processes (and
caches are model-specific regardless) — a second process is the right shape, not a
compromise.

## Validation

Same query that failed under MMR, now through the full mcpo → rag-retrieval →
`/rerank` path, `k=5`:

```
search_corpus("proj vedoucí EPC", k=5)
  0.9491  Marek Česal    — Řízení projektu / PM   (the actual project leader)
  0.2225  Viktor Bobro   — Projektový tým
  0.2119  Michal Šterba  — Projektový tým
  0.1902  Miroslav Laboj — Projektový tým
  0.1794  Viktor Vážný   — Projektový tým
```

Five EPC contact rows, correctly ranked (leader on top), **zero** unrelated chunks —
versus one contact + four noise chunks before. The reranker also cleanly separates
signal from noise (junk rows score ~0.002), which is what makes a future
`RAG_MIN_SCORE` floor practical.

## Consequences

Positive:
- "List all X" queries work: distinct same-template records are no longer suppressed.
- Ranking reflects true query–document relevance, in Czech and English.
- Returned `score` is meaningful and consistent with order.
- No container bloat; rag-retrieval stays a thin HTTP client; the reranker is a
  dedicated GPU `llama-server` sharing the iGPU/VRAM the LLM leaves idle.
- **~36 s → ~1.9 s** per query once the cross-encoder moved to the Vulkan iGPU.

Negative / cost:
- A second `llama-server` process (~1.2 GB VRAM for the reranker GGUF) and one extra
  HTTP round-trip per query. The GGUF is a build artifact (gitignored; regenerate via
  `convert_hf_to_gguf.py`, see `models/reranker/MODEL.md`).
- llama.cpp reranking scores each pair in one physical batch, so the unit must set
  `-b/-ub` above the longest chunk (currently 4096; the default 512 500s on long rows).

Neutral / open:
- `RAG_MIN_SCORE` is 0.0 (off) pending calibration on accumulated result dumps.
- `RAG_CANDIDATES=40` is a starting point; larger pools cost linearly at rerank time.
- The legacy MMR path remains available (`RAG_USE_RERANKER=0`) purely for A/B.

## Measured latency (2026-07-21) — why this ADR is PENDING

`search_corpus` now returns a per-stage `_timing_ms` split (embed / fuse / rerank /
expand; `services/rag-retrieval/server.py`). Measured on a real query
(`"hesla pro EPC"`, `k=5`, `RAG_CANDIDATES=40`, reranker **warm**):

| stage | time |
|-------|------|
| embed | 0.2 s |
| fuse (Qdrant hybrid RRF) | 0.02 s |
| **rerank (cross-encoder)** | **~36 s** |
| expand | 0.006 s |

The cross-encoder is **~99 %** of retrieval latency. An isolated `/rerank` of 40
short synthetic docs was ~10 s; ~36 s is with real corpus candidates (wide table
rows / multi-line chunks → many more tokens per pair) and is repeatable. This is
inference, **not** model load.

### Not the cause: lazy loading
Loading `bge-reranker-v2-m3` on the first `/rerank` costs ~15–20 s **once** after an
embedder (re)start; it does not recur, and the 36 s was measured warm. So
**eager-loading at unit start would not reduce per-query latency** — it would only
move the one-time first-call penalty into embedder startup. Pre-loading (an
`@lifespan` warm-up) is defensible for first-call UX since the reranker is used on
essentially every RAG query, but it is orthogonal to the real problem — low priority.

### Candidate mitigations (evaluate, then record the winner + numbers here)
1. **`RAG_CANDIDATES` 40 → 20** — rerank cost is ~linear in pool size; ~halves it.
   Cheapest lever; observed score cliffs are sharp, so recall should hold.
2. **Cap the per-candidate text** sent to the reranker (not the returned text) — the
   wide table rows are the token hogs; truncating the rerank input cuts per-pair cost.
3. **CPU threads / niceness** — ensure the reranker isn't single-threaded and isn't
   starving llama-server (idle vs under-load varied ~10 s → 36–50 s).
4. **ONNX int8 quantization** of the cross-encoder on CPU — typically 2–4× faster;
   the biggest CPU-only win, at the cost of a ranking-quality re-check.
5. **GPU rerank via llama.cpp/Vulkan — CHOSEN (2026-07-21).** Not ROCm/torch (that
   *would* be a bother on gfx1150), but llama.cpp's native `--reranking` running the
   GGUF on the **Vulkan iGPU** — same engine as the LLM, no ROCm — on VRAM the 7B
   leaves idle. ~36 s → ~1.9 s. See Resolution below. (Mitigations 1–4 remain
   available for further trimming but are no longer needed for acceptable latency.)

### Resolution (2026-07-21) — GPU reranker on the iGPU
Converted `bge-reranker-v2-m3` to an f16 GGUF (`convert_hf_to_gguf.py`, from the
cached HF weights) and served it from a dedicated `llama-server --reranking` on the
iGPU (Vulkan, `:8081`; `manifests/ragfarm-reranker.service`). Measured end-to-end
through mcpo → rag-retrieval → `:8081`:

| stage | CPU (embedder sub-endpoint) | GPU (iGPU/Vulkan) |
|-------|-----------------------------|-------------------|
| rerank (40 candidates) | ~36 s | **~1.9 s** |

Scores are byte-identical (llama.cpp raw logit → `sigmoid` == FlagReranker
`normalize=True`; e.g. Marek Česal 0.9491 both ways), so ranking, the returned
`score`, and `RAG_MIN_SCORE` semantics are unchanged. Operational note: llama.cpp
reranking needs `-b/-ub` ≥ the longest `(query,doc)` pair (unit sets 4096; default
512 errors on long rows). This retires the CPU `/rerank` sub-endpoint on the embedder
(embeddings-only again).

### Reranker batch-size tuning (numbers + options)
llama.cpp reranking scores each `(query, document)` pair as a **single sequence in one
physical batch** (encoder, non-causal — not split token-by-token), so the physical
batch `-ub` must be ≥ the longest pair, and the logical batch `-b` ≥ `-ub`. Defaults
500'd us:

- **Default `-ub 512`** → `input (543 tokens) is too large to process. increase the
  physical batch size` on the first long candidate (a 189-word chunk + query ≈ 543 t).
- **Unit sets `-b 4096 -ub 4096`** (`manifests/ragfarm-reranker.service`). Sizing:
  prose chunks cap at the ingester's `CHUNK_MAX_WORDS=480` words (~600–700 tokens) and
  table rows are shorter, so 4096 is ~6× the worst-case pair — safe against chunk drift.

Options / trade-offs:
- **Raise `-b/-ub`** if `CHUNK_MAX_WORDS` is ever increased (keep `-ub` above the
  longest possible chunk+query). Cost is a bigger compute buffer — trivial on the
  48 GB UMA for a 560 M encoder. `-ub` need not exceed `-c` (8192/slot default); 4096
  sits comfortably under it.
- **Truncate the rerank input** in `rag-retrieval._rerank` (send only the first N
  tokens per candidate) to keep `-ub` small. *Rejected as default:* the old CPU
  FlagReranker silently truncated to 512, whereas llama.cpp scoring the **full** text
  is strictly better relevance — we prefer correctness to a smaller batch.
- **Throughput vs latency:** a larger `-ub` also lets more pairs pack into one batch;
  at 40 candidates the ~1.9 s figure already includes this. Trimming `RAG_CANDIDATES`
  (40→20) remains the lever if latency ever needs to drop further.

### Hardware trajectory (out of scope for the decision; noted for the prod-HW plan)
Much of the current pain — 7B tool-discipline flakiness, over-long answers, and this
CPU rerank cost — eases on the planned prod NVIDIA box: a ~30 B model (better
instruction-following, larger native context, tensor-parallel across two cards for
still-larger context / throughput) plus CUDA rerank. Per ADR-0003 the durable layer
is HW-agnostic; model and rerank device swap without re-architecture. Tracked in
`docs/deployment.md` → "What changes on prod NVIDIA hardware".
