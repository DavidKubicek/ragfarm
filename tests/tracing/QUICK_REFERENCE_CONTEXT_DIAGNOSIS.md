# Context Blowup Diagnosis - Quick Reference

## One-minute setup

```bash
cd /mnt/user-data/outputs
chmod +x ragfarm_bench_chatid.py ragfarm_rag_tracer.py
```

---

## Diagnose in 3 commands

### 1. Benchmark (pure LLM + context growth)
```bash
./ragfarm_bench_chatid.py --chat-id diag_001 --rag 5 --csv bench.csv

# Output: CSV shows context_before, context_after, context_growth per turn
# If context_growth > 150 tokens/turn → context blowup happening
```

### 2. RAG trace (show candidate pool)
```bash
python ragfarm_rag_tracer.py trace \
  --chat-id diag_001 \
  --query "leadb229p.lea.piz?" \
  --rag-endpoint http://127.0.0.1:8000 \
  --k 50

# Output: Shows Qdrant → RRF → Reranker pipeline
# Check: final_context_tokens (if >3000, you're near overflow)
```

### 3. Pool evolution (see shrinkage across different k)
```bash
python ragfarm_rag_tracer.py evolve \
  --query "jak zálohovat hostitele" \
  --rag-endpoint http://127.0.0.1:8000 \
  --k-values 50,100,200,500

# Output: Shows how many tokens at each pool size
# Look for: Score cliff (where scores drop sharply)
```

---

## Workflow

### Detect problem
```bash
./ragfarm_bench_chatid.py --chat-id test_001 --rag 10 --csv bench.csv
grep context_growth bench.csv  # >150? Problem detected.
```

### Identify cause
```bash
# Check what RAG is returning
python ragfarm_rag_tracer.py evolve \
  --query "test query" \
  --k-values 100,200,500

# Read output:
# - k=500, Candidates=500, Total Tokens=216,000  ← HUGE
# - Suggests RAG_PREFETCH=500 is too large
```

### Fix it
```bash
# Edit ragfarm/.env
export RAG_PREFETCH=100      # Reduce initial pool
export RAG_CANDIDATES=5      # Reduce final candidates
export RAG_MMR_LAMBDA=0.8    # More aggressive filtering

# Restart RAG service
systemctl restart ragfarm-rag  # (or however you start it)

# Verify improvement
./ragfarm_bench_chatid.py --chat-id test_002 --rag 10 --csv bench_after.csv
diff bench.csv bench_after.csv  # Should see smaller context_growth
```

---

## Interpreting output

### Bench CSV columns
```
chat_id         → Correlation ID (same across tools)
run_idx         → Which prompt (0, 1, 2, ...)
prompt_tokens   → Tokens in user query
completion_tokens → Tokens in LLM response
total_tokens    → Sum
context_before  → Cumulative tokens BEFORE this request
context_after   → Cumulative tokens AFTER this request
context_growth  → How much context grew (after - before)
```

### RAG trace output
```
embed_ms        → Qdrant prefetch time
fuse_ms         → RRF fusion time
expand_ms       → MMR diversity filtering time
rerank_ms       → Reranker scoring time (usually dominates)
final_context_tokens → Total tokens if all candidates kept
```

---

## Key metrics

| Metric | Good | Bad | Action |
|--------|------|-----|--------|
| context_growth/turn | <100 | >200 | Reduce RAG_CANDIDATES |
| final_context_tokens (single query) | <1500 | >3000 | Reduce RAG_PREFETCH |
| rerank_ms | <2000 | >5000 | Increase RAG_MMR_LAMBDA (filter before reranker) |
| Score cliff | Yes (sharp drop after k=100) | No (gradual) | RAG_PREFETCH too large |

---

## Troubleshooting

### "Connection refused" to RAG endpoint
```bash
curl -s http://127.0.0.1:8000/rag/search_corpus \
  -H 'Content-Type: application/json' \
  -d '{"query":"test","k":3}'

# If fails: RAG service not running
systemctl start ragfarm-rag-retrieval
```

### Tracer shows 0 context tokens
```bash
# Check endpoint is correct
python ragfarm_rag_tracer.py trace \
  --query "test" \
  --rag-endpoint http://127.0.0.1:8000 \
  --k 10

# If response is empty: RAG corpus might be empty
```

### Bench shows huge context_growth
```bash
# Expected: 80-150 tokens/turn
# If >300: Either RAG candidates huge or LLM generating too much
# Check:
python ragfarm_rag_tracer.py evolve --k-values 50,100,200
# Look at token counts → if they grow fast, RAG candidates too large
```

---

## Files

```
ragfarm_bench_chatid.py          Benchmark with context tracking
ragfarm_rag_tracer.py            RAG pipeline visibility
CONTEXT_DIAGNOSIS_GUIDE.md       Full guide (this file's parent)
QUICK_REFERENCE.md               This file
```

---

## Example session

```bash
# 1. Detect
./ragfarm_bench_chatid.py --chat-id diag_001 --rag 5 --csv bench.csv
# Output: context_growth = 180 tokens/turn (HIGH)

# 2. Investigate
python ragfarm_rag_tracer.py evolve \
  --query "leadb229p" \
  --k-values 50,100,200,500
# Output: k=500, Total Tokens=216,000 (HUGE)

# 3. Hypothesis
# RAG_PREFETCH=500 is too large. Reduce to 100.

# 4. Fix
export RAG_PREFETCH=100
# Restart service...

# 5. Verify
./ragfarm_bench_chatid.py --chat-id diag_002 --rag 5 --csv bench_after.csv
# Output: context_growth = 95 tokens/turn (GOOD)
```

---

**Start with the 3 commands above. Share the output.**
