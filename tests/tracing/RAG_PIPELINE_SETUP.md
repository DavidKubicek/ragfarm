# RAG Pipeline Tracing Setup - Questions for David

To build the RAG pipeline tracer that shows context blowup at each stage, I need to know your exact flow:

## 1. Qdrant Retrieval
- **Endpoint:** (default localhost:6333?)
- **Top-K retrieved initially:** How many candidates do you get from Qdrant? (e.g., top-100? top-500?)
- **What's the score type:** similarity_score? cosine? bge_score?

## 2. RRF (Reciprocal Rank Fusion)
- **Who implements it:** Open WebUI? mcpo? Custom script in ragfarm?
- **Where does it run:** Before/after MMR?
- **Input:** All Qdrant results?
- **Output:** Reranked list, how many survive? (e.g., top-100 → top-50?)

## 3. MMR (Max Marginal Relevance)
- **Who implements it:** Same as RRF?
- **Diversity threshold:** What value? (higher = more filtering)
- **Input:** RRF output?
- **Output:** Further filtered, how many? (e.g., top-50 → top-20?)

## 4. Reranker (llama.cpp instance on :8002)
- **Model:** BGE-M3? Something else?
- **Input:** MMR output (top-20? top-10?)
- **Output:** Final scored list, how many to LLM? (e.g., top-5?)
- **Score range:** 0-1? or raw logits?

## 5. LLM Context
- **Generation llama.cpp:** localhost:8001?
- **Max context window:** 4K? 8K? 32K?
- **How context built:** (Qdrant docs + user query + system prompt)?
- **Current problem:** Does context overflow (grow >4K)? When? After how many turns?

## Example output I'll build:

```
RETRIEVAL CANDIDATE POOL EVOLUTION
══════════════════════════════════════════════════════════════

[1] Qdrant Initial Retrieval: 100 candidates
────────────────────────────────────────
  rank score text[:200]
  1    0.95  "EPC is internal portal for management..."
  2    0.93  "Login requires domain credentials..."
  3    0.88  "FAQ: common authentication errors..."
  ...
  100  0.45  "Old archived docs, not relevant..."

[2] After RRF: 50 candidates
────────────────────────────────────────
  (scores adjusted by reciprocal rank fusion)
  1    0.97  "Login requires domain credentials..."
  2    0.95  "EPC is internal portal..."
  ...
  50   0.52  "..."

[3] After MMR: 20 candidates  
────────────────────────────────────────
  (diversity filter applied, some removed)
  1    0.97  "Login requires domain credentials..."
  3    0.95  "EPC is internal portal..."
  (rank 2 removed - too similar to rank 1)
  ...
  20   0.67  "..."

[4] After Reranker: 5 candidates
────────────────────────────────────────
  (BGE-M3 final scoring)
  1    0.98  "Login requires domain credentials..."
  2    0.96  "EPC is internal portal..."
  ...
  5    0.78  "..."

[5] Context for LLM: 4 docs selected
────────────────────────────────────────
  Documents: 2048 tokens
  System prompt: 512 tokens
  User query: 45 tokens
  ───────────────────
  Total context: 2605 tokens (65% of 4K window)

[6] LLM Generation
────────────────────────────────────────
  Generated: 256 tokens
  Documents cited: [1, 2, 4]
  Lost citation: [3, 5]
```

## Once you answer these, I'll build:

1. **Qdrant query** → get initial results with scores
2. **RRF tracer** → show before/after scores
3. **MMR tracker** → show which docs filtered
4. **Reranker input/output** → query your :8002 instance, show final scores
5. **LLM context builder** → show what actually goes to LLM
6. **Citation extraction** → which docs the LLM actually cited/used

**This will show you exactly where context blowup happens.**
