# RAG-farm HTTP Tracing & Engine Telemetry Integration Guide

**Complete visibility into the ragfarm inference pipeline: HTTP calls + direct engine metrics.**

---

## Architecture Overview

Your ragfarm setup likely looks like:

```
┌─────────────────┐
│  Open WebUI     │
│  (orchestrator) │
└────────┬────────┘
         │
         ├──→ /v1/completions → llama.cpp (generation, :8001)
         ├──→ /v1/embeddings  → llama.cpp (embedder, :8003) [optional]
         ├──→ /retrieval       → Qdrant vector DB (:6333) [optional]
         └──→ /v1/rerank      → llama.cpp (reranker, :8002) [optional]
```

The tracer captures:
1. **HTTP calls** between Open WebUI and inference engines (via proxy or log parsing)
2. **Engine telemetry** queried directly from each llama.cpp instance
3. **Call sequence** showing which engines are used in what order

---

## What you need from docs/topology.md

Create/verify `docs/topology.md` contains:

```yaml
services:
  generation:
    type: "llama.cpp"
    endpoint: "http://localhost:8001"
    model: "Qwen2.5-7B-Q4_K_M"
    quantization: "Q4_K_M"
    settings: "GPU, Vulkan"
    
  reranker:
    type: "llama.cpp"
    endpoint: "http://localhost:8002"
    model: "BGE-M3-reranker"
    quantization: "FP32"
    settings: "CPU"
    
  embedder:
    type: "Embedding"
    engine: "FlagEmbedding"
    model: "BGE-M3"
    endpoint: "http://localhost:8003"
    
  vector_db:
    type: "Qdrant"
    endpoint: "http://localhost:6333"
    
orchestration:
  chat_flow:
    - step: "Embed user query"
      engine: "embedder"
      endpoint: "/embed"
    - step: "Retrieve documents"
      engine: "vector_db"
      endpoint: "/search"
    - step: "Generate answer"
      engine: "generation"
      endpoint: "/v1/completions"
    - step: "Rerank results"
      engine: "reranker"
      endpoint: "/v1/rerank"
    - step: "Return to user"
      target: "Open WebUI"
```

---

## Tool 1: Simple Tracer (`ragfarm_tracer_simple.py`)

**Best for: Quick profiling without running a proxy.**

### Query engines for live telemetry

```bash
chmod +x /mnt/user-data/outputs/ragfarm_tracer_simple.py

# Query your actual ragfarm setup
python3 ragfarm_tracer_simple.py query \
  --generation localhost:8001 \
  --reranker localhost:8002
```

**Output:**
```
Querying engines...

✓ generation  (localhost:8001)   2.34ms → Qwen2.5-7B-Q4_K_M
✓ reranker    (localhost:8002)   1.87ms → BGE-M3

═══════════════════════════════════════════════════════════════
RAGFARM ENGINE TELEMETRY
═══════════════════════════════════════════════════════════════

Engine          Endpoint                  Model                      Status           Latency
─────────────── ───────────────────────── ────────────────────────── ─────────────── ──────────
generation      localhost:8001            Qwen2.5-7B-Q4_K_M          ✓ OK              2.34ms
reranker        localhost:8002            BGE-M3                     ✓ OK              1.87ms
```

**Saved to:** `telemetry_snapshot.json`

---

## Tool 2: HTTP Request Tracer

**Best for: Seeing the exact sequence of HTTP calls during a chat.**

### Method A: Proxy approach (advanced)

```bash
python3 ragfarm_http_tracer.py \
  --listen 0.0.0.0:8000 \
  --generation localhost:8001 \
  --reranker localhost:8002 \
  --output trace.json
```

Then configure Open WebUI to use `http://localhost:8000/v1` as LLM endpoint.

Every request flows through the proxy, which captures:
- HTTP method + path
- Request/response sizes (bytes)
- Latency (wall-clock time per request)
- Status code
- Which engine handled it

### Method B: Parse Open WebUI logs (simpler)

1. **Open Browser DevTools** (F12 → Network tab)
2. **Have a chat** in Open WebUI
3. **Export requests** as HAR (right-click → Save all as HAR with content)
4. **Parse with tracer** (parser not yet included, see below for manual approach)

### Method C: Manual capture (most straightforward)

Run this during a chat:

```bash
# Terminal 1: Monitor HTTP traffic to ragfarm engines
watch -n 0.1 'netstat -i | grep -E "localhost:800[123]"'

# Terminal 2: Run curl to capture timing
time curl -X POST http://localhost:8001/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Jak se přihlásím do EPC?","max_tokens":256}' \
  -w "@curl_format.txt" -o /dev/null
```

Where `curl_format.txt`:
```
    time_namelookup:  %{time_namelookup}\n
       time_connect:  %{time_connect}\n
    time_appconnect:  %{time_appconnect}\n
   time_pretransfer:  %{time_pretransfer}\n
      time_redirect:  %{time_redirect}\n
 time_starttransfer:  %{time_starttransfer}\n
                    ─────────────────────────\n
      time_total:  %{time_total}\n
```

---

## Interpreting HTTP traces

### Call sequence example (typical RAG chat)

```
Session: user_chat_001
Timestamp: 2026-07-25T10:30:45.123456
Total requests: 5
Total time: 523.45ms

Seq Engine       Endpoint           Latency    Req      Resp  Status
--- ------------ ────────────────── ────────── -------- ──── ───────
  1 embedder     localhost:8003       45.30    256      512  200  → Embed query
  2 vector_db    localhost:6333       35.10   1024     2048  200  → Search docs
  3 generation   localhost:8001      187.30    512     1024  200  → Generate answer
  4 reranker     localhost:8002       67.80   2048      256  200  → Rerank results
  5 generation   localhost:8001      145.20   1024      512  200  → Final synthesis

PER-ENGINE STATISTICS:
Engine        Requests  Total time  Avg latency
─────────────────────── ──────────── ─────────────
embedder             1       45.30      45.30ms
vector_db            1       35.10      35.10ms
generation           2      332.50     166.25ms
reranker             1       67.80      67.80ms

Total latency: 523.45ms
  Embeddings:    45.30ms   (8.7%)
  Retrieval:     35.10ms   (6.7%)
  Generation:   332.50ms   (63.5%) ← Most time here
  Reranking:     67.80ms   (12.9%)
  Overhead:      42.75ms   (8.2%)
```

### What to look for

| Metric | Meaning | Good | Bad |
|--------|---------|------|-----|
| **Generation latency** | Time for LLM inference | <200ms | >500ms |
| **Reranker latency** | Time to score docs | <100ms | >300ms |
| **Retrieval latency** | Time to search vectors | <50ms | >200ms |
| **Request count** | HTTP calls per prompt | 3-5 | >10 (orchestration overhead) |
| **Total time** | End-to-end | <600ms | >1000ms |

---

## Combined workflow: Bench + Trace

### Step 1: Baseline (no RAG)

```bash
# Extended bench (measures LLM inference alone)
python3 ragfarm_bench_extended.py --prompt 5 \
  --csv bench_baseline.csv

# Note:
# - Prefill rate (tok/s)
# - Decode rate (tok/s)
# - E2E latency (ms)
```

### Step 2: Query engine telemetry

```bash
# Direct engine queries
python3 ragfarm_tracer_simple.py query \
  --generation localhost:8001 \
  --reranker localhost:8002
```

### Step 3: Trace a chat session

```bash
# Method 1: Proxy (capture all traffic)
python3 ragfarm_http_tracer.py \
  --listen 0.0.0.0:8000 \
  --generation localhost:8001 \
  --reranker localhost:8002

# Method 2: Manual curl
for i in 1 2 3; do
  time curl -X POST http://localhost:8001/v1/completions \
    -H "Content-Type: application/json" \
    -d '{"prompt":"test","max_tokens":256}' \
    2>&1 | grep real
done
```

### Step 4: Analyze results

Compare:
- **Bench results** → pure LLM throughput
- **Tracer results** → real chat with orchestration overhead
- **Difference** → tool orchestration cost

Example:
```
Bench (isolated LLM):
  Generation: 1000 tok/s (decode rate)
  E2E per prompt: 250ms

Chat (with tools):
  Generation request latency: 187ms (in HTTP trace)
  Tool orchestration: +100ms (decision phases, context building)
  Total E2E: ~400ms

Overhead: 250ms → 400ms = +60% from tools/orchestration
```

---

## Integration with docs/topology.md

### Step 1: Define your topology

```bash
cat > docs/topology.md << 'EOF'
# RAG-farm Service Topology

## Inference Pipeline

```
User Query
    ↓
[Embedder] ← BGE-M3 (CPU)
    ↓ (query embedding)
[Vector DB] ← Qdrant (:6333)
    ↓ (top-k documents)
[Generation] ← Qwen2.5-7B (GPU, Vulkan, :8001)
    ↓ (answer with context)
[Reranker] ← BGE-M3 reranker (CPU, :8002)
    ↓
User Response
```

## Service Endpoints

| Service | Type | Endpoint | Model | Status |
|---------|------|----------|-------|--------|
| generation | llama.cpp | :8001 | Qwen2.5-7B-Q4_K_M | ✓ |
| reranker | llama.cpp | :8002 | BGE-M3 | ✓ |
| embedder | Embedding | :8003 | BGE-M3 | ✓ |
| vector_db | Qdrant | :6333 | — | ✓ |

EOF
```

### Step 2: Map to tracer

```bash
# Update tracer config to match topology
python3 ragfarm_tracer_simple.py query \
  --generation localhost:8001 \
  --reranker localhost:8002 \
  --embedder localhost:8003
```

### Step 3: Track changes

As you optimize:
- Update `topology.md` with performance metrics
- Re-run tracer before/after changes
- Compare HTTP traces to measure improvement

---

## Metrics to track over time

### CSV for regression testing

```bash
# Baseline
python3 ragfarm_bench_extended.py --rag 5 \
  --csv bench_$(date +%Y%m%d_%H%M%S).csv

# Track these:
# - prefill_tok_s (should stay stable)
# - decode_tok_s (should stay stable)
# - e2e_ms (should stay stable)
```

### Engine telemetry for changes

```bash
# Track model/endpoint changes
python3 ragfarm_tracer_simple.py query \
  --generation localhost:8001 > telemetry_$(date +%s).txt
```

### HTTP traces for orchestration

```bash
# Track tool overhead
python3 ragfarm_http_tracer.py ... \
  --output trace_$(date +%s).json

# Analyze: grep generation trace_*.json | wc -l
#         (more calls = more overhead)
```

---

## Troubleshooting

### "Connection refused" errors

```bash
# Check services are running
systemctl status ragfarm-llama.service
systemctl status ragfarm-llama-reranker.service

# Check endpoints respond
curl -s http://localhost:8001/health | jq .
curl -s http://localhost:8002/health | jq .
```

### Tracer shows 0 requests

```bash
# Check if Open WebUI is configured correctly
# Edit Open WebUI settings → API Settings → LLM Endpoint
# Should be: http://localhost:8000/v1 (if using proxy)
# Or: http://localhost:8001/v1 (if direct)

# Test manually:
curl -X POST http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"prompt":"test","max_tokens":10}'
```

### Inconsistent latencies

```bash
# Check system load
top -b -n 1 | grep Cpu

# If high:
# - Background tasks stealing resources
# - Other services contending for GPU
# - vLLM needs optimization

# Profile:
python -m py_spy record -o flamegraph.html -- <command>
```

---

## Files you need

| File | Purpose |
|------|---------|
| `ragfarm_bench_extended.py` | Baseline LLM throughput |
| `ragfarm_tracer_simple.py` | Query engines + parse traces |
| `ragfarm_http_tracer.py` | HTTP proxy for detailed tracing |
| `ragfarm_integrated_tracer.py` | Combined HTTP + engine metrics (requires aiohttp) |
| `docs/topology.md` | Your service configuration |
| `TRACER_INTEGRATION_GUIDE.md` | This file |

---

## Quick start (TLDR)

```bash
# 1. Check engines are responding
python3 ragfarm_tracer_simple.py query --generation localhost:8001

# 2. Benchmark baseline
python3 ragfarm_bench_extended.py --rag 3 --csv baseline.csv

# 3. Have a chat in Open WebUI, then:
#    Open DevTools (F12) → Network tab
#    Filter to "localhost:8001", "localhost:8002", etc.
#    Note the latencies

# 4. For detailed tracing, run HTTP tracer as proxy
python3 ragfarm_http_tracer.py \
  --listen 0.0.0.0:8000 \
  --generation localhost:8001 \
  --reranker localhost:8002

# 5. Configure Open WebUI to use http://localhost:8000/v1
# 6. Have another chat — tracer captures everything
# 7. Check output: http_trace.json
```

---

**All tools work with your current llama.cpp setup. No vLLM changes needed yet.**

When you do migrate to vLLM:
1. Update `topology.md` (new endpoint)
2. Re-run benches (should see 2-3x improvement)
3. Re-run tracer (should see less orchestration overhead)
4. Compare before/after JSONs
