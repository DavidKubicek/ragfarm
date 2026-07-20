# ADR-0008 — Cross-encoder re-ranking replaces MMR in search_corpus

Status: ACCEPTED (2026-07-20). Implemented and live; validated end-to-end on the
corpus (see Validation below).
Date: 2026-07-20
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

### Architecture — the model runs on the embedder host, policy stays in rag-retrieval

Per ADR-0007's scope, all ranking **policy** (pool size, sort, floor, `k`, the
rerank-vs-MMR switch) lives in `rag-retrieval/server.py`. Only the **model
inference** is delegated: the cross-encoder executes behind a new
`POST :8090/rerank` on the existing embedder service, which rag-retrieval calls
over HTTP exactly as it already calls `/embed`. This is symmetric with how
embedding already works and was chosen over loading the model in the rag-retrieval
process because:

- `rag-retrieval` is a slim `python:3.12-slim` container (mcp/qdrant-client/
  requests, no torch). In-process reranking would add ~2 GB of torch+FlagEmbedding
  to that image and load a 2.3 GB model **inside the container**.
- ADR-0007 note #4: every `rag-retrieval` restart forces an mcpo heal cycle. Loading
  a 2.3 GB model on each such restart would add ~15–20 s to every heal. Keeping
  rag-retrieval thin keeps restarts fast.
- The embedder service **already is** the host-side CPU BGE model host (FlagEmbedding
  + torch, model in the HF cache, rarely restarted) — the natural home for a sibling
  BGE model. The reranker is **lazy-loaded on first `/rerank` call**, so it costs no
  RAM or startup time until retrieval actually uses it.

`bge-reranker-v2-m3` is pinned safetensors-only (downloaded with `ignore_patterns=
["*.bin"]`, per the standing no-pickle rule) and recorded in
`models/embeddings/MODEL.md`.

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
- No container bloat; rag-retrieval restarts stay fast; one CPU model host.

Negative / cost:
- The embedder host holds a second ~2.3 GB model once `/rerank` is first hit.
- One extra HTTP round-trip and a cross-encoder forward pass over ~40 candidates per
  query (~1–2 s CPU). Acceptable for the interactive PoC; trim `RAG_CANDIDATES` if it
  drags.

Neutral / open:
- `RAG_MIN_SCORE` is 0.0 (off) pending calibration on accumulated result dumps.
- `RAG_CANDIDATES=40` is a starting point; larger pools cost linearly at rerank time.
- The legacy MMR path remains available (`RAG_USE_RERANKER=0`) purely for A/B.
