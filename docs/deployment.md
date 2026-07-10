# Deployment & prod re-deployment notes

Operational facts captured during the PoC build (steps 02–07). ADR-0001/0003
hold the *why*; this holds the concrete *what* needed to redeploy — especially the
things that must change when the PoC AMD MiniPC is replaced by prod NVIDIA HW.

## Runtime topology (PoC)

| component | where | bind | notes |
|-----------|-------|------|-------|
| llama-server (Qwen2.5-7B Q4_K_M) | host | `127.0.0.1:8080` | Vulkan/iGPU; systemd `llama-server.service` |
| embedder (BGE-M3, dense+sparse) | host | `127.0.0.1:8090` | CPU; systemd `embedder-server.service` |
| qdrant | container | `127.0.0.1:6333/6334` | `infra/compose.yaml`, volume `qdrant_data` |
| rag-retrieval (`search_corpus`) | container, host-net | `127.0.0.1:8104` | MCP streamable-http |
| mcpo (MCP→OpenAPI bridge) | container, host-net | `127.0.0.1:8000` | mounts `/rag`; config `services/mcp-gateway/mcpo-config.json` |
| open-webui (agent/UI) | container, host-net | `0.0.0.0:3000` | **only** LAN-exposed service; auth-gated |

### Why host networking (load-bearing PoC fact)
The LLM (`:8080`) and embedder (`:8090`) are host processes bound to **127.0.0.1
only** (not exposed to the LAN). A bridge-network container cannot reach a
loopback-bound host service via `host.docker.internal` (connection refused). So
rag-retrieval / mcpo / open-webui run with `network_mode: host` and reach the host
services at `127.0.0.1`. They keep their own binds on loopback (`RAG_HOST=127.0.0.1`,
mcpo `--host 127.0.0.1`) except open-webui, which binds `0.0.0.0` for remote access.

### Remote UI access
`OWUI_HOST` (compose env) controls the Open WebUI bind: `0.0.0.0` (default now =
remote access, login-gated by `WEBUI_AUTH`) or `127.0.0.1` (loopback-only; reach
via `ssh -L 3000:127.0.0.1:3000 <host>`). **Restrict `:3000` at the host firewall
to trusted networks** — it's the only externally reachable service.

## Autostart
- Host services: `manifests/llama-server.service`, `manifests/embedder-server.service`
  (both already `enabled`).
- Container stack: `manifests/ragfarm-stack.service` runs `docker compose up -d` on
  boot (install steps in the unit header). Containers also carry
  `restart: unless-stopped`, and `docker.service` is enabled — so a normal reboot
  restarts them anyway; the unit guarantees it and adds ordering + one control point.

## Open WebUI configuration (reproducible, not hand-clicked)
OWUI stores config in its `openwebui_data` volume. Recreate it with
`infra/openwebui/setup_openwebui.py` (idempotent):
- **Tool server** `TOOL_SERVER_CONNECTIONS` → OpenAPI at **`http://127.0.0.1:8000/rag`**
  (`path=openapi.json`, `auth_type=none`) → appears as tool id **`server:0`**.
- **Model preset** `ragfarm (corpus RAG)`: base `qwen2.5-7b-instruct` + `rag` tool
  pre-attached (`meta.toolIds`) + `params.function_calling=native` + a **grounding
  system prompt**. The grounding prompt is load-bearing: without it the 7B answers
  generic prose even when the exact chunk was retrieved.
- OpenAI endpoint: `OPENAI_API_BASE_URL=http://127.0.0.1:8080/v1` (compose env).
- Open WebUI's built-in document RAG is deliberately unused for the corpus (Option B).

## Verifying the toolchain
`infra/openwebui/check_toolchain.py` — exit 0 full pass, 2 needs interactive
browser confirm, 1 deterministic failure. The deterministic layer (mcpo
sparse+dense + llama tool-calling) is the reliable CI signal; OWUI's interactive
agent loop is confirmed in the browser (it is finicky to drive headlessly — the
one non-obvious requirement is `assistant_message_id` **in the chat/completions
request body**, encoded in the script).

## OpenNebula MCPs — mock mode & the confirmation gate (ADR-0004)
Until live OpenNebula exists, `mcp-infra-placement` and `mcp-host-control` run in
**mock mode** (`ONE_MOCK`/`HOST_MOCK` default to 1 in compose) against canned
VM↔host data, so the agent path and the human-in-the-loop UX are testable with no
cluster. Set `ONE_MOCK=0`/`HOST_MOCK=0` (and fill `.env`) at deployment.

- **Read-only tools** (`where_is_vm`, `list_vms_on_host`) are exposed to the model
  via mcpo (`/placement`) as tool server `server:1`.
- **The mutating op is gated.** `host-control` is bridged by mcpo but **deliberately
  NOT registered as an OWUI tool server** — the model cannot call reboot directly.
  It goes only through the OWUI native Python Tool `infra/openwebui/tools/reboot_guarded.py`,
  which: dry-runs to get the plan → shows it in a blocking `__event_call__`
  confirmation modal → executes with `confirm=True` only on human approval. The
  server-side allowlist (`HOST_ALLOWLIST`) + `confirm` gate still apply underneath.
  This is the ADR-0004 pattern: the LLM is out of the confirm loop; the human is the gate.
- `setup_openwebui.py` registers both tool servers, creates the Python Tool, and
  attaches all three (`server:0` rag, `server:1` placement, `reboot_guarded`) to the
  `ragfarm` preset. `__event_call__` modals require an interactive UI session (they
  do not fire on headless/API calls — correct for human confirmation).

## What changes on prod NVIDIA hardware
Per ADR-0003 the durable layer (Open WebUI, mcpo, MCP servers, `search_corpus`,
Qdrant) is HW-agnostic and should NOT be re-architected. Concrete changes:
- **Inference**: replace llama.cpp/Vulkan with a CUDA server (vLLM/TGI/llama.cpp-CUDA).
  Repoint `OPENAI_API_BASE_URL` only. Keep the OpenAI-compatible contract.
- **Embedder**: BGE-M3 moves onto the GPU (CUDA); keep the `/embed` dense+sparse
  contract on `:8090`. Re-ingest is unnecessary if the model+revision are unchanged.
- **Networking**: with a single CUDA stack and services able to bind a shared
  interface, the host-networking workaround can be dropped — move containers back to
  a compose bridge network and reach inference/embedder via service DNS or
  `host.docker.internal` (which requires those services to bind beyond loopback).
  Re-evaluate the `0.0.0.0` exposure + firewalling for the target network.
- **mcpo config**: when OpenNebula is reachable (steps 05/06 unblock), add
  `mcp-infra-placement` (`where_is_vm`) and `mcp-host-control` to
  `services/mcp-gateway/mcpo-config.json` so they appear in Open WebUI alongside
  `search_corpus`; fill `ONE_XMLRPC`/`ONE_AUTH` per `services/mcp-infra-placement/.env.example`.
- **Corpus**: `CORPUS_PATH` and the Qdrant `corpus` collection (dense 1024 + sparse)
  are portable; re-run `services/ingester/ingester.py --recreate` against prod corpus.
