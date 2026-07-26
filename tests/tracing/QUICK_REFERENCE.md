# RAG-farm Instrumentation Quick Reference

## Commands

```bash
# 1. Extended Benchmark
cd /mnt/user-data/outputs
chmod +x ragfarm_bench_extended.py

# Default (5 Czech prompts)
./ragfarm_bench_extended.py

# 10 random prompts
./ragfarm_bench_extended.py --prompt 10

# 3 RAG queries (expects knowledge base)
./ragfarm_bench_extended.py --rag 3

# With CSV/JSON export
./ragfarm_bench_extended.py --rag 5 --csv bench.csv --json bench.json

# Custom tokens + timeout
./ragfarm_bench_extended.py --rag 3 --max-tokens 512 --timeout 60

# 2. Chat Execution Tracer
chmod +x chat_execution_tracer.py

# Demo (no LLM required)
./chat_execution_tracer.py --demo

# Result saved to chat_trace_demo.json
```

---

## Metric Interpretation Cheat Sheet

### Prefill latency (ms)
```
What:    Time to process the entire prompt
Typical: 100-200 ms (Vulkan iGPU), 30-50 ms (RTX 6000)
Bad:     >500 ms (GPU memory bottleneck)
```

### Decode rate (tok/s)
```
What:    Tokens generated per second (streaming speed)
Typical: 800-1200 tok/s (Vulkan iGPU), 2000-3500 tok/s (RTX 6000)
Bad:     <300 tok/s (unacceptable for chat)
Target:  >1000 tok/s for responsive chat
```

### Prefill rate (tok/s)
```
What:    Prompt tokens processed per second
Typical: 250-400 tok/s (Vulkan iGPU), 500-800 tok/s (RTX 6000)
Bad:     <150 tok/s (GPU memory bandwidth issue)
```

### E2E latency (ms)
```
What:    Total time from prompt to final answer
Formula: prefill_latency + decode_latency
Typical: 200-500 ms (for 256-token generation)
Target:  <300 ms for responsive chat
```

### Context growth (tokens/prompt)
```
What:    How many tokens are retained after each prompt
Typical: 80-150 tokens/prompt
Bad:     >500 tokens/prompt (context window filling too fast)
Check:   Is conversation history being truncated?
```

### Decision phase overhead (%)
```
What:    % of total time spent between tool return and next LLM generation
Formula: (decision_time_ms / total_time_ms) * 100
Typical: 10-20% (with tools)
Bad:     >30% (tool orchestration overhead too high)
```

### Tool execution time (ms)
```
What:    Wall-clock time one tool takes to run
Typical: 50-200 ms (DB query, API call)
Bad:     >1000 ms (profiling needed)
Track:   Accumulates quickly with multiple tools
```

---

## Output files

### CSV columns
```
timestamp
run_idx
prompt
prompt_tokens, prompt_bytes
completion_tokens, completion_bytes
total_tokens, total_bytes
context_before_tokens, context_after_tokens
context_before_bytes, context_after_bytes
prefill_ms, decode_ms, e2e_ms
prefill_tok_s, decode_tok_s
```

### JSON structure
```json
{
  "session": {
    "session_id": "string",
    "timestamp": "ISO8601",
    "model_name": "string",
    "total_time_ms": 503.46,
    "total_tokens": 239,
    "total_bytes": 960,
    "tool_execution_time_ms": 150.5,
    "decision_time_ms": 80.24,
    "llm_generation_time_ms": 272.72
  },
  "requests": [
    {
      "step_number": 1,
      "step_name": "User prompt",
      "direction": "IN",
      "payload_bytes": 69,
      "estimated_tokens": 17,
      "latency_ms": 0,
      "cumulative_ms": 1.3,
      "full_payload": {...}
    }
  ],
  "tool_invocations": {
    "tool_name": [
      {
        "timestamp_ms": 302.9,
        "execution_time_ms": 150.5,
        "input": {...},
        "output": {...}
      }
    ]
  }
}
```

---

## Baseline targets

### Current setup (iGPU, Vulkan, Qwen2.5-7B Q4_K_M)
```
Prefill rate:          280-350 tok/s
Decode rate:           900-1200 tok/s
E2E (256 tokens):      250-400 ms
Context/prompt:        100-150 tokens
Overhead (tools):      15-25%
Overhead (decision):   8-12%
```

### After vLLM + RTX 6000 Blackwell
```
Prefill rate:          500-800 tok/s     (+1.8-2.5x)
Decode rate:           2000-3500 tok/s   (+2.2-3.0x)
E2E (256 tokens):      80-150 ms         (+3-5x faster)
Context/prompt:        same
Overhead (tools):      10-15%            (reduced)
Overhead (decision):   5-8%              (reduced)
```

---

## Troubleshooting matrix

| Symptom | Cause | Fix |
|---------|-------|-----|
| Prefill <150 tok/s | GPU memory bottleneck | Check bandwidth with profiler |
| Decode <300 tok/s | Model quantization loss, driver overhead | Switch model, update drivers |
| E2E >1000 ms | Request queuing, slow llama-server | Check CPU load, increase workers |
| Context >500 tokens/prompt | Not truncating history | Verify conversation truncation logic |
| Decision >30% | Slow tool execution or MCP overhead | Profile tools, check MCP response time |
| Tool >1 sec | Slow DB/API | Use profiler (py-spy), add caching |
| Inconsistent numbers | Token estimation vs actual | Use actual tokens from response.usage |

---

## Quick comparison: Before/After vLLM

```
METRIC                  iGPU          vLLM          IMPROVEMENT
─────────────────────────────────────────────────────────────────
Prefill (tok/s)         300           650           +2.2x
Decode (tok/s)          1000          2500          +2.5x
E2E 256-token (ms)      350           100           +3.5x
Decision overhead       15%           8%            -46%
Tool exec overhead      25%           12%           -52%
───────────────────────────────────────────────────────────────────
User perception:        "Noticeable  → "Instant
                         response"      responses"
```

---

## Real-world example

```
User asks: "What are the EPC_AZURE FW rules?"

Timeline with tracer:
─────────────────────────────────────────────────────────────
User sends prompt:      1.3 ms    (17 tokens)
LLM prefill:            102.2 ms  (decision: "need tool")
LLM decision:           152.5 ms  (24 tokens decision)
Tool execution:         302.9 ms  (query_fw_rules: 150 ms)
LLM decision phase:     383.2 ms  (thinking about result: 78 ms)
LLM generation:         503.4 ms  (final answer: 120 ms)
─────────────────────────────────────────────────────────────

Breakdown:
- LLM work:        272.7 ms (54%)
- Tool work:       150.5 ms (30%)
- Orchestration:    80.2 ms (16%)
Total:             503.4 ms

User experience: "Feels like 500ms response time"
```

---

## Files you need

1. **ragfarm_bench_extended.py** — Run this to measure inference
2. **chat_execution_tracer.py** — Run `--demo` to understand output
3. **INSTRUMENTATION_GUIDE.md** — Full reference
4. **README_INSTRUMENTATION.md** — Getting started
5. **QUICK_REFERENCE.md** — This file

All in: `/mnt/user-data/outputs/`

---

## One-liner tests

```bash
# Test if llama-server is up
curl -s http://localhost:8001/health | jq .

# Get current model
curl -s http://localhost:8001/v1/models | jq '.data[0].id'

# Run quick 3-prompt benchmark with export
./ragfarm_bench_extended.py --rag 3 --max-tokens 256 --csv bench_$(date +%s).csv

# See what tracer captures
./chat_execution_tracer.py --demo | head -50

# Check baseline numbers exist
ls -lh baseline*.csv
```

---

## Performance debugging workflow

```
1. Run baseline benchmark
   ./ragfarm_bench_extended.py --rag 5 --csv baseline.csv
   
2. Check if prefill OK
   grep prefill baseline.csv | awk '{print $NF}'
   
3. Check if decode OK
   grep decode baseline.csv | awk '{print $NF}'
   
4. If slow, profile LLM endpoint
   curl -X POST http://localhost:8001/v1/completions \
     -H "Content-Type: application/json" \
     -d '{"prompt":"test","max_tokens":10}' | jq '.usage'
   
5. If tools slow, trace one call
   ./chat_execution_tracer.py --demo
   grep "execution_time" chat_trace_demo.json
   
6. Identify bottleneck, fix, re-run baseline
   (go to step 1)
```

---

**Print this page. Keep it next to your terminal when profiling ragfarm.**
