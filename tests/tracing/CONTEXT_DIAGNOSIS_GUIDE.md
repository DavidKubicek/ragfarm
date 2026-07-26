# RAG-farm Context Blowup Diagnosis Guide

**Complete visibility into context growth through the full RAG pipeline.**

---

## The Problem

Your ragfarm RAG service starts with a **big candidate pool** from Qdrant and progressively filters:

```
Qdrant (RAG_PREFETCH)  →  RRF (fuse)  →  MMR (evict)  →  Reranker (score)  →  LLM
  N candidates             N (rescored)     M filtered     K final            Context grows
```

**The issue:** Context grows rapidly turn-by-turn because:
1. Each LLM response adds tokens
2. RAG candidates are added to context for next turn
3. Window fills up (4K, 8K limit)

**You need to see:**
- How many candidates per retrieval? → `RAG_CANDIDATES` / `RAG_PREFETCH` env vars
- How much do scores change through pipeline? → Qdrant → RRF → Reranker
- Which candidates actually get used by LLM? → Citation tracking
- When does context overflow? → Turn-by-turn accumulation

---

## Tool 1: Benchmark with Context Tracking (`ragfarm_bench_chatid.py`)

**Measures:** Pure LLM inference + context accumulation

```bash
./ragfarm_bench_chatid.py --chat-id my_chat_001 --rag 5 --csv bench.csv
```

**Output shows:**
```
CONTEXT GROWTH TRACKING (Chat ID: my_chat_001)
═══════════════════════════════════════════════

Run  Prompt  Completion  Total  Running  Growth
1      45        87       132      132     132
2      48        92       140      272     140
3      51        95       146      418     146
4      49        88       137      555     137
5      52        91       143      698     143

CONTEXT BLOWUP ANALYSIS:
  Initial context:    0 tokens
  Final context:    698 tokens
  Total growth:     698 tokens
  Avg growth/prompt: 139.6 tokens
  Growth rate:      inf%

⚠ WARNING: Context growing rapidly!
  Current: 698 tokens
  Action: Review retrieval (Qdrant candidates), RRF/MMR filters, reranker output
```

**CSV has these columns for correlation:**
```
chat_id, timestamp, run_idx, prompt,
prompt_tokens, completion_tokens, total_tokens,
context_before, context_after, context_growth
```

**Use this to:** Detect when context starts overflowing

---

## Tool 2: RAG Pipeline Tracer (`ragfarm_rag_tracer.py`)

**Measures:** Candidate pool evolution through Qdrant → RRF → MMR → Reranker

### Single query trace

```bash
python ragfarm_rag_tracer.py trace \
  --chat-id my_chat_001 \
  --query "leadb229p.lea.piz?" \
  --rag-endpoint http://127.0.0.1:8000 \
  --output rag_trace_my_chat_001.json
```

**Output shows:**
```
RAG PIPELINE TRACE
═════════════════════════════════════════════════

Chat ID: my_chat_001
Query: leadb229p.lea.piz?
Total time: 2685.0ms

TIMING BREAKDOWN:
Stage            Time (ms)  % Total
─────────────────────────────────
embed_ms            258.3    9.6%
fuse_ms              23.6    0.9%
expand_ms             0.0    0.0%
rerank_ms          2369.1   88.2%

CANDIDATE POOL EVOLUTION:

Qdrant Prefetch (258.3ms)
  Candidates: 10 | Initial retrieval from Qdrant
  [ 1] score=0.9659 | EPC25-FW-rules-EPC-20260713-DB_ENDUR.xlsx | table_row
       sheet: Firewall Rules DB ENDUR, Date Request: 13/7/2026...
  [ 2] score=0.9540 | EPC25-FW-rules-EPC-20260713-DB_ENDUR.xlsx | table_row
       sheet: Firewall Rules DB ENDUR, Date Request: 13/7/2026...
  ... (8 more)

RRF Fusion (23.6ms)
  Candidates: 10 | Sparse+dense fusion, scores may change
  [ 1] score=0.9659 | ... (same, slightly reranked)
  ... (9 more)

Reranker Score (2369.1ms)
  Candidates: 10 | LLM semantic relevance scoring
  [ 1] score=0.9800 | ... (score adjusted by reranker)
  ... (9 more)

FINAL CONTEXT:
  Candidates: 10
  Total tokens (est): 4,320
  Avg tokens/doc: 432

⚠ WARNING: Large context window
  4,320 tokens is 1x a 4K window
  This will cause context overflow after a few turns
```

### Pool evolution (multiple k values)

```bash
python ragfarm_rag_tracer.py evolve \
  --query "jak zálohovat hostitele" \
  --rag-endpoint http://127.0.0.1:8000 \
  --k-values 50,100,200,500
```

**Shows:**
```
CANDIDATE POOL EVOLUTION
════════════════════════════════════════════════════════════════

Query: jak zálohovat hostitele

k Value  Candidates  Avg Score  Min Score  Total Tokens
─────────────────────────────────────────────────────────
     50           50     0.7234     0.4521       21,600
    100          100     0.6845     0.3210       43,200
    200          200     0.6234     0.2156       86,400
    500          500     0.5123     0.1034      216,000
```

**Use this to:**
1. See how many candidates RAG returns for each query
2. Identify score cliff (where scores drop sharply)
3. Calculate context blowup: `Total Tokens × avg_turns_per_session`

---

## Workflow: Diagnose Context Overflow

### Step 1: Run bench to see context growth

```bash
# 5 RAG prompts, track context
./ragfarm_bench_chatid.py --chat-id diag_001 --rag 5 --csv bench_diag.csv

# Check the CSV
cat bench_diag.csv | awk -F, '{print $1, $10, $11, $12}' \
  # Shows: chat_id context_before context_after context_growth
```

**If context_growth is >150 tokens/prompt:**
→ Go to Step 2

### Step 2: Trace actual RAG queries

```bash
# Use same chat_id to correlate
for query in "leadb229p.lea.piz?" "jak zálohovat" "Vypiš FW pravidla"; do
  python ragfarm_rag_tracer.py trace \
    --chat-id diag_001 \
    --query "$query" \
    --output rag_trace_diag_001_${i}.json
done

# Check final_context_tokens in each JSON
jq '.final_context_tokens' rag_trace_diag_001_*.json
```

**If final_context_tokens > 2000 for each query:**
→ Go to Step 3

### Step 3: Identify the bottleneck

Check which RAG service env vars are set:

```bash
# Show RAG service config (from ragfarm/.env or container logs)
echo "RAG_PREFETCH (initial pool size):"
echo "RAG_CANDIDATES (final candidates to LLM):"
echo "RAG_MMR_LAMBDA (diversity threshold):"
echo "RAG_USE_RERANKER (true/false):"
```

Then run pool evolution to see shrinking:

```bash
python ragfarm_rag_tracer.py evolve \
  --query "jak zálohovat hostitele" \
  --k-values 100,200,500,1000

# Look for:
# - k=1000, Candidates=1000, Total Tokens=500,000  ← TOO MANY
# - Score drops sharply after k=200 ← candidates are low quality below this
# - Reranker time dominates ← LLM bottleneck
```

### Step 4: Adjust RAG configuration

Based on findings:

```bash
# CASE 1: Too many initial candidates
# Solution: Reduce RAG_PREFETCH in .env
export RAG_PREFETCH=200  # was 500

# CASE 2: Too many final candidates to LLM
# Solution: Reduce RAG_CANDIDATES in .env
export RAG_CANDIDATES=5  # was 10

# CASE 3: Reranker is slow
# Solution: Increase RAG_MMR_LAMBDA (filter more before reranker)
export RAG_MMR_LAMBDA=0.8  # was 0.6

# CASE 4: Reranker is disabled
# Solution: Enable it to filter low-quality candidates
export RAG_USE_RERANKER=true

# Restart RAG service
systemctl restart ragfarm-rag-retrieval

# Re-run bench to verify improvement
./ragfarm_bench_chatid.py --chat-id diag_002 --rag 5 --csv bench_diag_after.csv
```

---

## Interpreting Results

### Good baseline (before optimization):
```
Bench:
  context_growth per prompt: 120-150 tokens
  final context after 5 turns: 600-750 tokens

RAG trace:
  Qdrant prefetch: 500 candidates
  After RRF: 500 (rescored)
  After reranker: 10 candidates to LLM
  Final context: 2048 tokens
```

### Warning signs (context blowup):
```
Bench:
  context_growth per prompt: >200 tokens
  final context after 5 turns: >1000 tokens (overflow at 4-5 turns)

RAG trace:
  Qdrant prefetch: 1000+ candidates (too many)
  After reranker: 20+ candidates to LLM (should be <10)
  Final context: >3000 tokens (approaching window limit)
```

### Optimized (after tuning):
```
Bench:
  context_growth per prompt: 80-100 tokens
  final context after 10 turns: 800-1000 tokens

RAG trace:
  Qdrant prefetch: 100-200 candidates (controlled)
  After reranker: 5 candidates to LLM
  Final context: 1200-1500 tokens (comfortable margin)
```

---

## CSV Correlation

Both tools use `chat_id` for correlation:

```bash
# Bench CSV
chat_id,run_idx,prompt,context_before,context_after,context_growth
diag_001,0,lead...,0,132,132
diag_001,1,jak...,132,272,140
diag_001,2,Vypiš...,272,418,146

# RAG JSON (from tracer)
{"chat_id": "diag_001", "query": "leadb229p.lea.piz?", 
 "final_candidate_count": 10, "final_context_tokens": 4320}
{"chat_id": "diag_001", "query": "jak zálohovat", 
 "final_candidate_count": 10, "final_context_tokens": 4850}

# Cross-reference:
# Row 1 of bench → RAG query 1 (leadb229p.lea.piz?)
# Row 2 of bench → RAG query 2 (jak zálohovat)
# Check: context_growth (132) vs final_context_tokens (4320)
#        The RAG candidates are NOT all in context (they're scored, top-k selected)
```

---

## Files

```
ragfarm_bench_chatid.py         LLM + context tracking (chat_id support)
ragfarm_rag_tracer.py           RAG pipeline visibility (pool evolution)
CONTEXT_DIAGNOSIS_GUIDE.md      This file
```

---

## Summary

**To diagnose context blowup:**

1. Run `ragfarm_bench_chatid.py` → see context accumulation
2. Run `ragfarm_rag_tracer.py trace` → see candidates per query
3. Run `ragfarm_rag_tracer.py evolve` → see pool shrinking
4. Compare timings + token counts → identify bottleneck
5. Adjust RAG env vars → retest

**Chat ID correlation:** All results tagged with same `chat_id` for cross-referencing across tools.

---

**Next: Run this workflow and share the results. I'll help interpret.**
