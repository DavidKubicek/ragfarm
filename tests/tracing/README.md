# Instrumentation and tracing — what these tools are, and what they taught us

**Status: a framework, not a finished toolkit.** These tools were built in July
2026 against the llama.cpp/AMD deployment to answer one question — *where does
the time and the context actually go?* They answered it, the answers shaped the
architecture, and then the ground moved under them: ADR-0013 replaced the
generation engine and the Thinking models arrived. Treat them as instruments that
need recalibrating, not as documentation of the current system.

This chapter replaces seven separate guides written in July. They documented an
engine we no longer run, ports that never existed, and a set of "questions for
David" that were answered a month ago. What was worth keeping is here; the rest
is in git history.

## What the tracing work established

These findings outlived the tools that produced them, and several are load-bearing
in the current design:

- **Retrieval is dominated by the reranker.** The cross-encoder is ~297 ms of a
  query, far more than Qdrant or the embedder. That is the cost of precision and
  it is worth paying — but it is why the reranker runs at lowered scheduling
  priority, so the interactive LLM wins contention.
- **Context growth per turn is the thing to watch, not context size.** Above
  roughly 150 tokens of growth per turn you are heading for overflow regardless
  of how much headroom you started with. This is what motivated bounding context
  by *evicting old tool-result bodies* rather than summarising, in
  `scripts/agent.py`.
- **Open WebUI's context loop is not observable from inside it.** Its compaction
  fires unpredictably and, after a summarisation pass, drops tool schemas — so a
  mutating action can narrate a success it never performed. That single finding
  is why `scripts/agent.py` exists at all: a loop we own, with the tool schemas
  permanently present so they cannot be compacted away.
- **The `:8000/rag` versus `:8104` distinction is a recurring trap.** Tools must
  talk to mcpo on `:8000`, not to the MCP server directly on `:8104`. Two
  separate tools were written against the wrong one.

## What is wrong with them today

Deliberately not chased, because a rewrite would supersede it:

- **No concept of thinking models.** They cannot separate reasoning tokens from
  answer tokens. Against Qwen3-VL-Thinking, which routinely spends two thirds of
  its budget on reasoning, every timing they report is meaningless. This is the
  single reason not to trust their numbers today.
- **Docstrings still show the old port map** — `localhost:8001/8002/8003`. Those
  ports never existed in this deployment. The code was fixed on 2026-08-03 to
  resolve endpoints through `ragfarm_env.py`; the copy-pasteable examples in the
  docstrings were not. Trust the resolver, not the comments.
- **`ragfarm_http_tracer.py`** is proxy-based and needs to sit in the request
  path. It still holds bare `host:port` defaults.
- **`chat_execution_tracer.py`'s proxy mode is dangerous.** A previous session
  wired it into Open WebUI's database and **broke the model presets**. Do not
  re-plumb it without a plan. The retired `scripts/owui_trace_proxy.py` is the
  headstone.

## Endpoint resolution

One resolver, used by every tool. Shell environment wins over `.env`, `.env`
wins over built-in defaults, and the built-in defaults are the real deployed
ports — so the tools work on a clean checkout with no configuration.

```bash
.venv/bin/python tests/tracing/ragfarm_env.py    # print resolved endpoints
```

The real port map is no longer duplicated here. `scripts/stack.sh list` prints
it from the one place it is defined, and `scripts/stack.sh status` tells you
which of them are actually answering. See `man docs/man1/stack.1`.

## The tools

| script | what it answers |
|---|---|
| `ragfarm_bench.py` | baseline tokens/s, prefill and decode, per prompt |
| `ragfarm_bench_extended.py` | the same with CSV/JSON export and prompt files |
| `ragfarm_bench_chatid.py` | context growth per turn, correlated by chat id |
| `ragfarm_rag_tracer.py` | candidate-pool evolution: Qdrant → RRF → reranker, per-stage tokens and latency |
| `ragfarm_integrated_tracer.py` | engine telemetry plus pipeline trace in one report |
| `ragfarm_tracer_simple.py` | minimal single-query timing |
| `chat_execution_tracer.py` | replay a canned session; `--demo` needs no LLM |
| `ragfarm_http_tracer.py` | proxy-based HTTP capture (see the warning above) |
| `ragfarm_env.py` | the endpoint resolver every other tool imports |

```bash
.venv/bin/python tests/tracing/chat_execution_tracer.py --demo   # no LLM required
```

## What superseded them

Most of what these tools were used for now has a purpose-built replacement, and
that is the honest reason the rewrite has not been urgent:

- **Answer quality over time** → `scripts/test_regressions.py`, which replays
  `docs/prompts.md` against the live slot and judges the answers. See
  `docs/regression-testing.md`.
- **Model comparison** → `scripts/bench_ab.py`, and the raw results kept under
  `docs/measurements/`.
- **Service health** → `scripts/stack.sh status`, which probes all thirteen
  services and includes depth checks where a 200 can lie.
- **Retrieval quality on one query** → `scripts/rag_pool_inspect.py`.

## Requirements for the rewrite

Whoever picks this up should carry forward:

1. **Thinking-model awareness.** Separate reasoning tokens from answer tokens and
   report both, or the timings mean nothing.
2. **Resolve endpoints through `ragfarm_env`**, never hardcode.
3. **Surface the ADR-0010 gate.** `search_corpus` already returns
   `_timing_ms.gate` with `floor_drop`, `kneedle_cut` and `kneedle_d` — the most
   useful retrieval diagnostic we have, and nothing reads it yet.
4. **Do not require sitting in the request path.** The proxy approach cost a
   broken Open WebUI database once already.
