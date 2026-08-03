# tests/tracing — state of these tools

**Status: partial. Treat as a framework, not a finished toolkit.** Dave's own
assessment stands: incomplete, and it has **no concept of thinking/reasoning
models** — it cannot separate `reasoning_content` from `content`, which makes it
close to useless against Qwen3-VL-Thinking. A rewrite is expected; do not invest
in these as-is beyond keeping them runnable.

## What was fixed (2026-08-03)

These tools were written against an early, wrong port map — `localhost:8001` for
generation, `:8002` reranker, `:8003` embedder. **None of those ports exist.**

`ragfarm_env.py` is now the single resolver: it loads the repo-root `.env` via
`python-dotenv` and exposes the real endpoints. Shell-exported values win over
`.env`; `.env` wins over the built-in defaults; the built-in defaults are the real
deployed ports, so the tools work with no `.env` at all.

```bash
.venv/bin/python tests/tracing/ragfarm_env.py    # print the resolved endpoints
```

Fixed in every tool: the **functional** defaults — `argparse default=` and the
main constructor defaults (`base_url`, `rag_endpoint`). All eight tools import
cleanly and resolve endpoints from `.env`.

## What is still wrong

Deliberately not chased, because the rewrite will replace it:

- **Docstrings and usage examples** still show `localhost:8001/8002/8003`. They are
  copy-pasteable and will mislead you. Trust `ragfarm_env.py`, not the docstrings.
- **`ragfarm_http_tracer.py`** still holds bare `host:port` constructor defaults
  (`generation_endpoint="localhost:8001"` etc.). It is proxy-based, needs an OWUI
  reconfiguration to sit in the request path, and was already being skipped.
- **`chat_execution_tracer.py`**'s proxy mode (`--listen 0.0.0.0:8002 --forward
  localhost:8001`) is unrelated to the reranker despite the port collision. A
  previous session wired this into OWUI's DB and **broke the model presets** —
  see the retired `scripts/owui_trace_proxy.py`. Do not re-plumb without a plan.

## The real port map

| port | service |
|---|---|
| 8080 | LLM, OpenAI-compatible (llama.cpp `--alias` / vLLM `--served-model-name`) |
| 8081 | reranker `/reranking` (bge-reranker-v2-m3) |
| 8090 | embedder `/embed` (BGE-M3, dense+sparse) |
| 6333 | Qdrant |
| 8000 | mcpo OpenAPI bridge — tools mount under `/<name>`, e.g. `/rag` |
| 8104 | rag-retrieval MCP (behind mcpo; curl it via `:8000/rag/...`) |
| 3000 | Open WebUI |

**`:8000/rag` vs `:8104` is a recurring trap.** `ragfarm_rag_tracer.py` must talk
to mcpo on `:8000`, not the MCP directly on `:8104`.

## Requirements for the rewrite

Whoever rewrites this should carry forward:

1. **Thinking-model awareness** — separate reasoning tokens from answer tokens;
   report both. Without this the timings are meaningless on a Thinking model.
2. **Read endpoints from `ragfarm_env`**, never hardcode.
3. **Report the ADR-0010 gate** — `search_corpus` returns `_timing_ms.gate` with
   `floor_drop`, `kneedle_cut`, `kneedle_d`. That is exactly the retrieval
   diagnostic worth surfacing, and nothing here reads it yet.
4. **Do not require sitting in the request path.** The proxy approach cost a
   broken OWUI DB once already.
