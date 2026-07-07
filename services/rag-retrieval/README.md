# rag-retrieval — search_corpus (ADR-0003 durable layer)

`search_corpus` is the corpus RAG tool. Per ADR-0003 (Option B) all corpus
retrieval flows through this MCP tool, **not** Open WebUI's built-in document RAG.

## What it does
Hybrid retrieval over the step-04 Qdrant `corpus` collection:
1. embeds the query via the step-03 embedder (`/embed`, `kind=query`) → dense + sparse
2. Qdrant Query API with two prefetch branches — **dense** (semantic, Czech/English)
   and **sparse** (exact host/IP/VLAN token match) — fused by **RRF**

This is why one tool serves both exact identifier lookups (e.g. `hsmbvxip001ts`)
and multilingual semantic questions.

## Topology (PoC)
The generative LLM (`llama-server` :8080) and the BGE-M3 embedder (:8090) run on
the **host**, bound to `127.0.0.1` (loopback only — not exposed to the LAN). The
agent/UI layer runs in containers on **host networking** so it reaches those
loopback services at `127.0.0.1` without widening their bind:

```
Open WebUI (127.0.0.1:3000)  ──OpenAI──▶ llama-server (127.0.0.1:8080/v1)
      │  tools via mcpo OpenAPI
      ▼
mcpo (127.0.0.1:8000/rag) ──MCP──▶ rag-retrieval (127.0.0.1:8104/mcp)
                                        │  /embed          │ Qdrant Query API
                                        ▼                  ▼
                              embedder (127.0.0.1:8090)  qdrant (127.0.0.1:6333)
```
All three container services are defined in `infra/compose.yaml`. Bring them up
(after `source scripts/proxy-env.sh`) with:
`docker compose -f infra/compose.yaml up -d rag-retrieval mcpo open-webui`

## Tool contract
`search_corpus(query: str, k: int = 5) -> {query, count, results[]}` where each
result is `{score, text, source_file, location, kind, lang}`.

## Verifying the chain
Direct (deterministic, no LLM) — proves embedder + Qdrant + MCP + mcpo:
```bash
# through mcpo's OpenAPI (the exact surface Open WebUI calls):
curl -s 127.0.0.1:8000/rag/search_corpus -H 'Content-Type: application/json' \
  -d '{"query":"hsmbvxip001ts","k":3}'                 # exact record (sparse)
curl -s 127.0.0.1:8000/rag/search_corpus -H 'Content-Type: application/json' \
  -d '{"query":"jak zálohovat hostitele","k":3}'       # Czech chunk (dense)
```

Through Open WebUI (the agent loop): open `http://127.0.0.1:3000` (SSH-forward
from a laptop: `ssh -L 3000:127.0.0.1:3000 <host>`), select `qwen2.5-7b-instruct`,
enable the **rag** tool, and ask e.g. *"What are the group, vCPU and RAM of host
hsmbvxip001ts?"* and a Czech infra question. The model calls `search_corpus` and
grounds its answer in the retrieved chunk.

## Open WebUI wiring (one-time, persisted in its volume)
- OpenAI endpoint: `OPENAI_API_BASE_URL=http://127.0.0.1:8080/v1` (compose env).
- Tool server: registered as `TOOL_SERVER_CONNECTIONS` → `http://127.0.0.1:8000/rag`
  (OpenAPI), appears as tool id `server:0`. Configure under
  Admin → Settings → Tools, or via `POST /api/v1/configs/tool_servers`.
- Function calling: **native** (the 7B emits tool calls via llama-server `--jinja`).
- Do **not** enable Open WebUI's own document RAG for the corpus (Option B).

## Extending (at deployment, when 05/06 unblock)
Add the OpenNebula-backed MCP servers to `services/mcp-gateway/mcpo-config.json`
so `where_is_vm` / host-control appear alongside `search_corpus` in Open WebUI.
