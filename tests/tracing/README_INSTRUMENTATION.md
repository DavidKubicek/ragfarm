# RAG-farm Full Transparency Instrumentation Toolkit

**Complete visibility into every microsecond and byte of ragfarm LLM inference and chat orchestration.**

---

## What's been delivered

### 1. Extended Benchmark (`ragfarm_bench_extended.py`)

**Purpose:** Measure every single inference stage with absolute numbers (not just rates).

**Metrics captured (per-prompt):**
- Prompt tokens, bytes, estimated chars/token
- Prefill latency (ms) + prefill rate (tok/s)
- Decode latency (ms) + decode rate (tok/s)
- Completion tokens, bytes, estimated chars/token
- E2E latency (ms)
- Context growth (running total)
- Request/response payloads (full, for debugging)

**Outputs:**
- Human-readable terminal display (per-prompt + aggregate summary)
- CSV export (for regression tracking across commits/deploys)
- JSON export (full payloads + all metrics)

**File:** `/mnt/user-data/outputs/ragfarm_bench_extended.py`

---

### 2. Chat Execution Tracer (`chat_execution_tracer.py`)

**Purpose:** Trace complete chat sessions to reveal orchestration overhead, tool execution timing, and decision phase latency.

**Captures (per chat session):**
- Every request/response step (user input, LLM prefill, tool decision, tool execution, tool result, LLM decision phase, final generation)
- Absolute timing for each step (millisecond precision)
- Token counts and byte counts at each stage
- Tool invocation details (schema IN, execution time, schema OUT)
- Cumulative time tracking
- Overhead breakdown (tool execution %, decision phase %, LLM generation %)

**Output:**
- Real-time trace display (as session executes)
- Full trace report (structured timeline)
- JSON export (complete session data, all payloads)

**Demo included:** Run with `--demo` flag to see a simulated chat session with tool invocation.

**File:** `/mnt/user-data/outputs/chat_execution_tracer.py`

---

### 3. Documentation (`INSTRUMENTATION_GUIDE.md`)

**Comprehensive guide including:**
- What each metric means
- Expected values and interpretation
- Usage examples for all commands
- CSV/JSON output format reference
- How to integrate with Open WebUI
- Troubleshooting guide
- Performance targets for ragfarm

**File:** `/mnt/user-data/outputs/INSTRUMENTATION_GUIDE.md`

---

## Quick start

### Baseline benchmark (5 Czech prompts)
```bash
cd /mnt/user-data/outputs
chmod +x ragfarm_bench_extended.py
./ragfarm_bench_extended.py
```

### Benchmark with results export
```bash
./ragfarm_bench_extended.py --rag 3 --max-tokens 512 \
  --csv bench_$(date +%s).csv \
  --json bench_$(date +%s).json
```

### Chat tracer demo (no LLM required)
```bash
chmod +x chat_execution_tracer.py
./chat_execution_tracer.py --demo
```

Output saved to:
- Terminal: full formatted trace report
- JSON: `/mnt/user-data/outputs/chat_trace_demo.json`

---

## Expected output (extended bench)

### Per-prompt display
```
════════════════════════════════════════════════════════════════════════════════════════════════
Run 1/5
════════════════════════════════════════════════════════════════════════════════════════════════

Prompt:
┌─ Jak se přihlásím do EPC?
└─ 45 tokens | 180 bytes

INPUT METRICS:
  Prompt tokens:            45
  Prompt bytes:           180
  Est. chars/token:       4.0

STAGE-BY-STAGE BREAKDOWN:
  Stage                | Latency  | Tokens | Bytes    | Tok/ms
  Request prep         |     2.34 |      0 |      256 |  0.000
  Prefill              |   145.30 |     45 |      180 |  0.309
  Decode               |    87.20 |     87 |      348 |  0.998

OUTPUT METRICS:
  Completion tokens:           87
  Completion bytes:           348
  Est. chars/token:          4.0

TIMING SUMMARY:
  Prefill latency:           145.30 ms
  Decode latency:             87.20 ms
  E2E latency:               232.50 ms
  Prefill rate:              310.21 tok/s
  Decode rate:               998.28 tok/s

CONTEXT GROWTH:
  Before (tokens):             0
  Before (bytes):              0
  After (tokens):            132
  After (bytes):             528
  Context growth:            132 tokens

Answer:
┌─ EPC je interní portál...
└─ 87 tokens | 348 bytes

REQUEST PAYLOAD:
  prompt length:            180 chars
  max_tokens:              256
  temperature:           0.70

RESPONSE PAYLOAD (summary):
  prompt_tokens:            45
  completion_tokens:        87
  total_tokens:            132
```

### Aggregate summary
```
════════════════════════════════════════════════════════════════════════════════════════════════
AGGREGATE SUMMARY (5 runs)
════════════════════════════════════════════════════════════════════════════════════════════════

ABSOLUTE TOTALS:
  Total prompt tokens:        225
  Total completion tokens:    435
  Total tokens:               660
  Total prompt bytes:         900
  Total completion bytes:    1740
  Total bytes:              2640

AVERAGES PER RUN:
  Avg prompt tokens:          45.0
  Avg completion tokens:      87.0
  Avg prefill latency:       145.30 ms
  Avg decode latency:         87.20 ms
  Avg E2E latency:           232.50 ms
  Avg prefill rate:          310.21 tok/s
  Avg decode rate:           998.28 tok/s

THROUGHPUT:
  Total inference time:        1.16 sec
  Overall throughput:         569.03 tok/s
```

---

## Expected output (chat tracer demo)

### Real-time trace
```
✓ Session started: chat_20250725_001 (Qwen2.5-7B-Q4_K_M)
→ [ 1] User prompt                    IN |     0.00ms (cumul:      1.3ms) |     17t       69b
← [ 2] LLM prefill request            OUT |   145.30ms (cumul:    102.2ms) |     64t      256b
→ [ 3] LLM tool decision              IN |    87.20ms (cumul:    152.5ms) |     24t       97b
  🔧 query_fw_rules       |   150.50ms | in:    29b out:   200b
← [ 4] Tool result                    OUT |     0.00ms (cumul:    302.9ms) |     50t      200b [query_fw_rules]
→ [ 5] LLM decision phase             IN |    78.30ms (cumul:    383.2ms) |     15t       61b
→ [ 6] LLM final generation           IN |   120.70ms (cumul:    503.4ms) |     69t      277b
```

### Full report
```
════════════════════════════════════════════════════════════════════════════════════════════════
CHAT EXECUTION TRACE REPORT
════════════════════════════════════════════════════════════════════════════════════════════════

Session: chat_20250725_001
Model: Qwen2.5-7B-Q4_K_M
Timestamp: 2026-07-25T02:38:36.664428
Total duration: 503.46ms

EXECUTION TIMELINE:
Seq Step                           Dir    Latency      Cumul   Tokens      Bytes Tool                
--- ------------------------------ --- ---------- ---------- -------- ---------- --------------------
  1 User prompt                     IN       0.00        1.3       17         69                     
  2 LLM prefill request            OUT     145.30      102.2       64        256                     
  3 LLM tool decision               IN      87.20      152.5       24         97                     
  4 Tool result                    OUT       0.00      302.9       50        200 [query_fw_rules]    
  5 LLM decision phase              IN      78.30      383.2       15         61                     
  6 LLM final generation            IN     120.70      503.4       69        277                     

TOOL INVOCATIONS:

  query_fw_rules:
    Execution time:    150.50 ms
    Input bytes:           29
    Output bytes:         200
    Input schema:    {"network_name": "EPC_AZURE"}

AGGREGATE STATISTICS:
  Total tokens:                     239
  Total bytes:                      960
  Total time:                    503.46 ms
  LLM generation time:           272.72 ms
  Tool execution time:           150.50 ms
  Decision/thinking time:         80.24 ms
  Overhead (decision):             15.9%
  Overhead (tools):                29.9%

THROUGHPUT:
  Tokens/sec:                    474.72
  Bytes/sec:                    1906.82
```

---

## Integration workflow

### Phase 1: Establish baseline (iGPU, current setup)
```bash
# Run extended bench
./ragfarm_bench_extended.py --rag 5 \
  --csv baseline_igpu_$(date +%Y%m%d).csv \
  --json baseline_igpu_$(date +%Y%m%d).json

# Record typical values:
# - Prefill rate (tok/s)
# - Decode rate (tok/s)
# - E2E latency (ms)
# - Context growth per prompt
```

### Phase 2: Profile a chat session
```bash
# Start with demo to understand output
./chat_execution_tracer.py --demo

# Then manually instrument your Open WebUI session:
# 1. Use browser DevTools to capture API calls
# 2. Parse into tracer format
# 3. Generate report showing orchestration overhead
```

### Phase 3: vLLM migration
```bash
# After deploying vLLM on RTX 6000 Blackwell:
./ragfarm_bench_extended.py --rag 5 \
  --csv baseline_vllm_$(date +%Y%m%d).csv \
  --json baseline_vllm_$(date +%Y%m%d).json

# Compare:
# - Prefill: 300 tok/s → 500+ tok/s (1.7-2x improvement)
# - Decode: 1000 tok/s → 2500+ tok/s (2.5x improvement)
# - E2E: 240 ms → 100 ms (2.4x improvement)
```

### Phase 4: Production monitoring
```bash
# Run daily benchmarks:
0 6 * * * cd /opt/ragfarm && /opt/ragfarm/ragfarm_bench_extended.py \
  --rag 3 --max-tokens 256 \
  --csv /var/log/ragfarm/bench_$(date +\%Y\%m\%d).csv \
  --json /var/log/ragfarm/bench_$(date +\%Y\%m\%d).json
```

---

## What to look for

### Good baseline (iGPU, Vulkan):
- Prefill: 280-350 tok/s
- Decode: 900-1100 tok/s
- E2E (256 tokens): 250-400 ms
- Context growth: 80-150 tokens/prompt

### Red flags (investigate):
- Prefill <200 tok/s → GPU memory bandwidth issue
- Decode <500 tok/s → KV cache inefficiency or driver overhead
- E2E >1000 ms → Request queuing or model size mismatch
- Decision phase >25% of total time → Tool framework overhead
- Context grows >500 tokens/prompt → Conversation history not being truncated

### vLLM targets (RTX 6000):
- Prefill: 500-800 tok/s (tensor ops + continuous batching)
- Decode: 2000-3500 tok/s (memory bandwidth fully utilized)
- E2E (256 tokens): 80-150 ms
- Decision phase: <10% (MCP overhead minimal)

---

## Files

| File | Purpose | Lines |
|------|---------|-------|
| `ragfarm_bench_extended.py` | Extended benchmark with absolute numbers per stage | ~500 |
| `chat_execution_tracer.py` | Chat session tracer with orchestration visibility | ~550 |
| `INSTRUMENTATION_GUIDE.md` | Complete reference documentation | ~450 |
| `README_INSTRUMENTATION.md` | This file — quick start and integration guide | ~400 |

---

## Next steps

1. **Run baseline** with current iGPU setup:
   ```bash
   ./ragfarm_bench_extended.py --rag 5 --csv baseline.csv
   ```

2. **Study the output** — note which stages consume most time

3. **Test chat tracer demo** — understand overhead accounting:
   ```bash
   ./chat_execution_tracer.py --demo
   ```

4. **Instrument a real Open WebUI chat** — identify tool orchestration overhead

5. **After vLLM deploy** — re-run benches and measure improvement

6. **Integrate into production monitoring** — daily regression detection

---

## Troubleshooting

**Bench won't connect to llama-server:**
```bash
# Check if service is running
systemctl status ragfarm-llama.service

# Check endpoint
curl http://localhost:8001/health

# Check logs
journalctl -u ragfarm-llama.service -n 50
```

**Tracer shows inconsistent token counts:**
- Token estimation is ~4 chars/token (conservative)
- Actual tokens from LLM endpoint are more accurate (captured in full payloads)

**Decision phase overhead >30%:**
- Profile MCP server response time
- Check if tool schemas are large (inefficient serialization)
- Consider caching tool results

---

## Performance interpretation

**Prefill rates tell you about prompt processing efficiency:**
- Token/s during prefill phase is ~3-4x lower than decode (prefill is memory-bound, decode is latency-bound on GPUs)
- vLLM's continuous batching should improve this by 2-3x vs llama.cpp

**Decode rates tell you about generation speed (user perception):**
- This is what users *feel* — the streaming response speed
- <100 tok/s → users notice stalls
- >500 tok/s → feels natural and responsive

**E2E latency tells you total wait time:**
- Includes prefill + decode + overhead
- vLLM should cut this by 50-70% on RTX 6000

**Decision phase overhead reveals orchestration cost:**
- Gap between tool returns and next LLM inference
- Should be <100 ms for responsive chat
- >300 ms → investigate tool framework, MCP server, LLM decision latency

---

**All tools are self-contained, no external dependencies beyond `requests` (standard library).**

**Run them and let me know what the baseline numbers look like on your current setup.**
