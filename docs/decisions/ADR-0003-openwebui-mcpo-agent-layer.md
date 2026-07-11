# ADR-0003 — Open WebUI + mcpo as the agent/UI layer; retire custom agent.py

Status: ACCEPTED
Date: 2026-06-18
Supersedes: the step-07 "client-side agent (`services/agent/agent.py`)" design in
ADR-0001 / CLAUDE.md Chapter 1. ADR-0001 still governs the PoC inference substrate
(engine split, Vulkan, NPU detour); this ADR governs everything ABOVE the inference
endpoint.

## Context

The originating plan (ADR-0001) called for a custom client-side agent loop
(`services/agent/agent.py`): an OpenAI-compatible client driving llama-server, an
MCP client driving the HTTP MCP servers, and hand-wired tool exposure. Steps 02–04
are now DONE: llama-server serves Qwen2.5-7B on an OpenAI-compatible endpoint with
tool-calling (`--jinja`), and the corpus is ingested into Qdrant with hybrid
dense+sparse vectors reachable via the (to-be-exposed) `search_corpus` tool.

Two facts reshape the layer above inference:

1. **The PoC hardware is disposable.** The AceMagic MiniPC (Ryzen AI 9 HX 370,
   32GB system / 32GB VRAM split) is a proof-of-concept. Production hardware will
   be NVIDIA-based and substantially faster. The entire AMD-specific substrate in
   ADR-0001 — iGPU-via-Vulkan for the LLM, the NPU embedding detour, the
   "llama.cpp reaches the iGPU only" constraint — dies with the MiniPC. On NVIDIA,
   a single CUDA stack serves both generation and embeddings and none of those
   constraints survive.

2. **A custom agent loop is wasted effort.** llama-server already speaks the
   OpenAI API. Mature open-source UIs (Open WebUI, LibreChat, LobeChat) consume
   that API directly and now support MCP tool exposure. Writing and maintaining a
   bespoke `agent.py` duplicates what these provide and couples the agent loop to
   PoC-specific code.

## Decision

Adopt **Open WebUI** as the agent/UI layer, bridged to the MCP servers via
**mcpo** (the MCP-to-OpenAPI proxy). **Retire `services/agent/agent.py`
entirely** — Open WebUI is the sole agent loop.

Retrieval ownership is **Option B**: the agent/MCP layer owns RAG, the UI is a thin
front-end. Open WebUI does NOT use its own built-in document-RAG pipeline for the
corpus. All corpus retrieval flows through the `search_corpus` MCP tool, which does
hybrid dense+sparse (RRF) search over Qdrant using the step-03 embedder. The
deliberately-engineered hybrid pipeline (dense for semantics, sparse for exact
host/IP/VLAN token match) is the reason a generic UI-side RAG is rejected — generic
RAG fails the exact-match infra lookups this system exists to serve.

### Architecture layering (the load-bearing distinction)

**Durable layer — survives the hardware swap, HW-agnostic:**
- Open WebUI as the UI / agent loop.
- mcpo bridging MCP servers → OpenAPI tools Open WebUI can call.
- MCP servers: `rag-retrieval` (`search_corpus`), `mcp-placement`
  (`where_is_vm`), `mcp-host-control` (drain-then-reboot, safety-gated).
- `search_corpus`: hybrid dense+sparse over Qdrant + BGE-M3.
- The inference endpoint is consumed ONLY through its OpenAI-compatible API.

**PoC substrate — disposable, owned by ADR-0001:**
- llama.cpp + Vulkan on the AMD iGPU, Qwen2.5-7B Q4_K_M.
- BGE-M3 on CPU (ADR-0002).
- The AMD engine split and all its kernel/driver specifics.

### The swappable-endpoint rule (protects the HW migration)

The LLM inference server is a **swappable OpenAI-compatible endpoint**. Everything
in the durable layer addresses it only via its OpenAI base URL
(`http://127.0.0.1:8080/v1` on the PoC). When production NVIDIA hardware arrives,
the inference server is replaced (e.g. vLLM, TGI, or llama.cpp-CUDA) and **nothing
in the durable layer changes** — Open WebUI re-points at the new base URL, mcpo and
the MCP servers are untouched, `search_corpus` is untouched. The HW migration must
be a configuration change (endpoint URL, and likely moving BGE-M3 onto the GPU),
not a re-architecture. Any design that would force durable-layer changes on the HW
swap violates this ADR.

## Consequences

- `services/agent/agent.py` and its planned client-side loop are removed. The
  `services/mcp-gateway` README's "add a rag-retrieval MCP" guidance still applies:
  the `rag-retrieval` MCP exposing `search_corpus` is KEPT and is now consumed by
  mcpo/Open WebUI rather than by `agent.py`.
- Step 07 in BUILD_STATE.md changes from "write the client-side agent" to "stand up
  Open WebUI → llama-server, bridge the MCP servers via mcpo, expose
  `search_corpus`, prove end-to-end retrieval." (See BUILD_STATE.md step 07.)
- Open WebUI + mcpo are added to the deployment. On the PoC, their footprint
  (Open WebUI: Python/Node, few-hundred-MB idle; mcpo: light) sits on the 32GB
  system side alongside Qdrant and CPU-resident BGE-M3. This is PoC-acceptable but
  is explicitly flagged for re-evaluation on production hardware, where the
  embedder will likely move to GPU.
- OpenNebula-backed tools (`where_is_vm`, host-control) remain BLOCKED until live
  OpenNebula access exists at deployment (no creds/reachability in the PoC). Until
  then, only the RAG half of the tool surface is verifiable end-to-end. Step 07's
  gate is therefore split into a RAG-only milestone (provable now) and a full gate
  (provable at deployment). This is a real, honest partial state — NOT a reason to
  mock `where_is_vm` to force a pass. Per CLAUDE.md's hard rules, clearly-named
  test mocks are acceptable but silently routing around the OpenNebula dependency
  is not.

## Alternatives considered

- **Keep custom `agent.py`** (retire rejected earlier, then chosen): rejected.
  Duplicates Open WebUI's loop; couples the agent to PoC code; nothing in the PoC
  needs a headless scriptable path yet. Can be reintroduced later as a second
  consumer of the same MCP servers if/when cron- or CI-driven infra automation is
  required — the MCP servers are shared, so the cost of adding it back is low.
- **UI owns RAG (Option A):** rejected. Discards the hybrid dense+sparse pipeline
  that exists specifically to serve exact host/IP/VLAN lookups generic RAG fails.
- **LibreChat / LobeChat instead of Open WebUI:** viable; Open WebUI chosen for the
  strongest MCP-bridge story (mcpo) and the lightest custom-code path. The
  swappable-endpoint and Option-B decisions are UI-agnostic, so a future UI change
  would not disturb the durable architecture.
