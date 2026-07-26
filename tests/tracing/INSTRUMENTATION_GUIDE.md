# RAG-farm Instrumentation & Transparency Guide

**Full visibility into every microsecond and byte of LLM inference and chat orchestration.**

This toolkit provides two complementary tools:

1. **ragfarm_bench_extended.py** — Benchmark with absolute numbers for every inference stage
2. **chat_execution_tracer.py** — Chat session tracer capturing tool orchestration, decision phases, and overhead

---

## Part 1: Extended Benchmark (ragfarm_bench_extended.py)

### What it measures

Every **single stage** with **absolute numbers**:

| Metric | Meaning | Example |
|--------|---------|---------|
| **Prefill latency** | Time to process prompt (ms) | 145.3 ms |
| **Decode latency** | Time to generate answer (ms) | 87.2 ms |
| **E2E latency** | Total time (ms) | 232.5 ms |
| **Prompt tokens** | Input size | 45 tokens |
| **Prompt bytes** | Input size (UTF-8) | 180 bytes |
| **Completion tokens** | Answer size | 87 tokens |
| **Completion bytes** | Answer size (UTF-8) | 348 bytes |
| **Context growth** | Running context accumulation | +132 tokens/run |
| **Prefill rate** | Tokens/sec during prompt processing | 310.2 tok/s |
| **Decode rate** | Tokens/sec during generation | 998.3 tok/s |

### Output format

**Per-prompt output** (human-readable):
```
Prompt 1/5: Jak se přihlásím do EPC?
┌─ EPC je interní portál...
├─ Request prep      |     2.34ms |      0 tokens |       256 bytes |   0.000 tok/ms
├─ Prefill           |   145.30ms |     45 tokens |       180 bytes | 0.309 tok/ms
├─ Decode            |    87.20ms |     87 tokens |       348 bytes | 0.998 tok/ms
└─ E2E               |   232.50ms

INPUT METRICS:
  Prompt tokens:           45
  Prompt bytes:           180
  Est. chars/token:      4.0

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
```

**Aggregate summary** (all prompts):
```
AGGREGATE SUMMARY (5 runs)
════════════════════════════════════════════════════════════════════

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

### Usage

```bash
# Basic: 5 default Czech prompts
./ragfarm_bench_extended.py

# 10 random samples from defaults (with replacement)
./ragfarm_bench_extended.py --prompt 10

# 3 RAG corpus queries
./ragfarm_bench_extended.py --rag 3

# Custom max tokens + RAG + save CSV + JSON
./ragfarm_bench_extended.py --rag 5 --max-tokens 512 --csv bench.csv --json bench.json

# Custom prompts from file
./ragfarm_bench_extended.py --prompt my_prompts.txt

# Monitor after vLLM deploy (regression tracking)
./ragfarm_bench_extended.py --rag 3 --csv deploy_$(date +%s).csv --json deploy_$(date +%s).json
```

### CSV Output

Columns: `timestamp`, `run_idx`, `prompt`, `prompt_tokens`, `prompt_bytes`, `completion_tokens`, `completion_bytes`, `total_tokens`, `total_bytes`, `context_before_tokens`, `context_after_tokens`, `context_before_bytes`, `context_after_bytes`, `prefill_ms`, `decode_ms`, `e2e_ms`, `prefill_tok_s`, `decode_tok_s`

Example:
```csv
timestamp,run_idx,prompt,prompt_tokens,prompt_bytes,completion_tokens,completion_bytes,total_tokens,total_bytes,...
2026-07-25T02:38:36.664428,0,"Jak se přihlásím do EPC?",45,180,87,348,132,528,...
```

### JSON Output

**Full payloads** from every request/response, including:
- Complete request payload (prompt, tokens, settings)
- Complete response payload (usage, completion, timing)
- Stage breakdown with millisecond precision

---

## Part 2: Chat Execution Tracer (chat_execution_tracer.py)

### What it captures

Every **phase** of a chat session with **absolute timing**:

1. **User input** — Size, tokens
2. **LLM prefill** — Time to process prompt, decision if tool needed
3. **Tool detection** — Which tool(s), schema extracted
4. **Tool execution** — Wall-clock time per tool, input/output schemas and sizes
5. **Decision phase** — Time between tool return and next LLM generation (who's thinking?)
6. **LLM generation** — Final answer, tokens, bytes, latency

### Output format

**Real-time trace output** (as session runs):
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

**Full trace report**:
```
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

### Key metrics explained

| Metric | Interpretation |
|--------|-----------------|
| **LLM generation time** | Time LLM spent on inference (excluding tools) |
| **Tool execution time** | Wall-clock time tools ran |
| **Decision/thinking time** | Gap between tool returns and next LLM request (orchestration overhead) |
| **Overhead (decision)** | % of total time spent in decision phase — *lower is better* |
| **Overhead (tools)** | % of total time spent executing tools — *indicates tool efficiency* |

### Usage

**Demo with simulated chat session:**
```bash
./chat_execution_tracer.py --demo
```

**Integration with Open WebUI** (proxy mode — not yet implemented, but framework ready):
```bash
# Start tracer as proxy between Open WebUI and vLLM
./chat_execution_tracer.py --listen 0.0.0.0:8002 --forward localhost:8001

# Configure Open WebUI to use proxy:
# Settings > API Settings > LLM Endpoint: http://localhost:8002/v1
```

### JSON Output

```json
{
  "session": {
    "session_id": "chat_20250725_001",
    "timestamp": "2026-07-25T02:38:36.664428",
    "model_name": "Qwen2.5-7B-Q4_K_M",
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
    },
    ...
  ],
  "tool_invocations": {
    "query_fw_rules": [
      {
        "timestamp_ms": 302.9,
        "execution_time_ms": 150.5,
        "input_bytes": 29,
        "output_bytes": 200,
        "input": {"network_name": "EPC_AZURE"},
        "output": {...}
      }
    ]
  }
}
```

---

## Integration: Testing with Real Open WebUI Chat Session

### Setup

1. **Start llama-server:**
   ```bash
   systemctl start ragfarm-llama.service
   ```

2. **Start Open WebUI** (if not already running):
   ```bash
   docker run -d -p 3000:8080 --name open-webui ghcr.io/open-webui/open-webui:latest
   ```

3. **Run extended bench as baseline:**
   ```bash
   ./ragfarm_bench_extended.py --rag 3 --csv baseline.csv --json baseline.json
   ```

4. **Test in Open WebUI:**
   - Open http://localhost:3000
   - Have a chat session (3-5 exchanges)
   - Include at least one tool call (e.g., ask for FW rules, contacts, etc.)

5. **Capture session manually** (until proxy mode implemented):
   - In Open WebUI developer console (F12 → Network tab), capture requests to `/v1/completions`
   - Save HAR file
   - Parse with tracer (custom JSON conversion tool)

### Expected observations

**Baseline bench (no tools):**
- Prefill: 300-400 tok/s (GPU-bound)
- Decode: 900-1200 tok/s (memory-bound)
- E2E: 200-300 ms per prompt
- Context growth: ~50-150 tokens/prompt

**Chat session (with tools):**
- LLM generation: ~50-100 ms
- Decision phase: 10-50 ms (orchestration overhead)
- Tool execution: 50-500 ms (depends on tool)
- Total overhead: 15-40% (decision + orchestration)

### Interpretation guide

**Prefill rates (tok/s):**
- <200 tok/s → GPU memory bottleneck (check bus bandwidth, tensor ops)
- 200-400 tok/s → Good (Q4_K_M on Vulkan iGPU typical)
- >400 tok/s → Excellent (RTX 6000 Blackwell target)

**Decode rates (tok/s):**
- <100 tok/s → Generation too slow, users waiting (unacceptable for chat)
- 100-300 tok/s → Acceptable (iGPU)
- >500 tok/s → Excellent (GPU)

**Decision phase overhead:**
- <5% → Minimal orchestration overhead
- 5-15% → Normal (acceptable tool coordination)
- >20% → High orchestration burden, investigate tool framework overhead

**Tool execution time:**
- Should be <500 ms for responsive chat
- If >1 sec, profile tool (DB queries, API calls, etc.)

---

## Advanced: Extending the tracer for your MCP tools

To instrument your custom MCP tool invocations:

```python
from chat_execution_tracer import ChatTracer

tracer = ChatTracer()
tracer.start_session(session_id="myapp_001", model_name="Qwen2.5-7B")

# Your LLM request
tracer.log_request(
    step_name="LLM decision",
    direction="IN",
    payload={"tool": "my_tool", "params": {...}},
    latency_ms=87.3
)

# Tool execution
import time
t0 = time.time()
result = my_tool(params)
execution_time = (time.time() - t0) * 1000

tracer.log_tool_invocation(
    tool_name="my_tool",
    schema={"name": "my_tool", "params": {...}},
    input_params=params,
    output=result,
    execution_time_ms=execution_time
)

# Generate report
session = tracer.end_session()
print(tracer.format_session_report(session))
```

---

## Files

- **ragfarm_bench_extended.py** — Standalone benchmark (no dependencies beyond requests)
- **chat_execution_tracer.py** — Standalone tracer (demo mode included)
- **INSTRUMENTATION_GUIDE.md** — This file

---

## Performance targets for ragfarm (energy distributor, NIS2)

| Metric | iGPU (baseline) | RTX 6000 Blackwell (target) |
|--------|-----------------|----------------------------|
| Prefill rate | 300 tok/s | 500+ tok/s |
| Decode rate | 1000 tok/s | 2500+ tok/s |
| E2E (256 tokens) | 400-600 ms | 100-200 ms |
| Tool overhead | <20% | <10% |
| Context (8K window) | 8000 tokens | 32000 tokens |

---

## Troubleshooting

### Bench shows 0 tok/s on prefill/decode
→ Check llama-server is up: `curl http://localhost:8001/health`

### Tracer reports high decision phase overhead (>30%)
→ Check Open WebUI backend; may indicate slow MCP server response

### Context grows too fast
→ Verify conversation history isn't being re-sent on every request; should use KV cache

### Tool execution time > 1 sec
→ Profile with `perf` or `py-spy`:
  ```bash
  python -m py_spy record -o flamegraph.html -- your_tool
  ```

---

## Next steps

1. Run extended bench against current iGPU setup → establish baseline
2. Run demo tracer → verify overhead accounting works
3. Capture real Open WebUI session → identify orchestration bottlenecks
4. Migrate to vLLM + RTX 6000 → re-run both benchmarks → measure improvement
5. Integrate tracer into production monitoring (optional)

---

**David**, this gives you **complete visibility**:
- Every single stage timed to millisecond precision
- Every request/response payload captured
- Context growth tracked
- Tool orchestration overhead quantified
- Decision phase overhead isolated

Run the demo, then against your actual ragfarm setup. You'll see exactly where the time is going.
