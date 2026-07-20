# Deployment & prod re-deployment notes

Operational facts captured during the PoC build (steps 02–07). ADR-0001/0003
hold the *why*; this holds the concrete *what* needed to redeploy — especially the
things that must change when the PoC AMD MiniPC is replaced by prod NVIDIA HW.

## Runtime topology — live component registry

The maintained inventory of everything that runs. This is the **live runtime
registry**; it supersedes the point-in-time snapshot in ADR-0005 as "what is
running now." The naming, port-allocation and collocation **rules** (how a short
name derives its directory / container / unit / port / mount) stay authoritative
in **ADR-0005** — this table is the current *state*, that ADR is the *policy*. Add
a component → add its row here and take the next `81xx` port per ADR-0005. All
container services run `network_mode: host` (see below) and bind loopback except
open-webui.

| plane | component | directory | compose service / container | bind | mcpo mount | agent-facing surface | mutates |
|-------|-----------|-----------|------------------------------|------|-----------|----------------------|---------|
| host | llama | built at `~/llama.cpp` | — (systemd `ragfarm-llama`) | `127.0.0.1:8080` | — | OpenAI base URL (swappable) | no |
| host | embedder | `services/embedder` | — (systemd `ragfarm-embedder`) | `127.0.0.1:8090` `/embed` | — | internal — dense+sparse embed | no |
| host | reranker | `services/embedder` (same process) | — (systemd `ragfarm-embedder`) | `127.0.0.1:8090` `/rerank` | — | internal — bge-reranker-v2-m3, lazy-loaded (ADR-0008) | no |
| host | ingester (+ watcher) | `services/ingester` | — (systemd `ragfarm-ingester-watcher`) | — | — | batch + autonomous incremental sync (ADR-0006) | writes Qdrant |
| container | qdrant | upstream image | `qdrant` / `infra-qdrant` | `127.0.0.1:6333/6334` | — | retrieval store; volume `qdrant_data` | no |
| container | rag | `services/rag-retrieval` | `rag-retrieval` / `infra-rag-retrieval` | `127.0.0.1:8104` | `/rag` | **tool server** — `search_corpus` | no |
| container | placement | `services/mcp-placement` | `mcp-placement` / `infra-mcp-placement` | `127.0.0.1:8101` | `/placement` | **tool server** — mock until OpenNebula | no |
| container | host-control | `services/mcp-host-control` | `mcp-host-control` / `infra-mcp-host-control` | `127.0.0.1:8102` | `/host-control` | **wrapper only** — `reboot_guarded` (ADR-0004) | YES |
| container | fs | `services/mcp-fs` | `mcp-fs` / `infra-mcp-fs` | `127.0.0.1:8103` | (unbridged) | experimental — not in the agent path | no |
| container | mcpo | upstream image | `mcpo` / `infra-mcpo` | `127.0.0.1:8000` | (is the front) | the single OpenAPI tool endpoint | n/a |
| container | open-webui | upstream image | `open-webui` / `infra-open-webui` | `0.0.0.0:3000` | — | the UI (**only** LAN-exposed; auth-gated) | n/a |

`ragfarm-stack.service` launches the container plane **except `mcp-fs`** (unbridged)
and **except the ingester** (a host job / the watcher unit, not the stack): qdrant,
rag-retrieval, mcp-placement, mcp-host-control, mcpo, open-webui. The reranker is
not a separate process or port — it is a second endpoint on the embedder host,
co-located per ADR-0008 so retrieval stays one CPU model host.

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

## Autostart & lifecycle

### What starts on boot
- **Host services** (systemd, already `enabled`): `ragfarm-llama.service`,
  `ragfarm-embedder.service`, `ragfarm-ingester-watcher.service` (autonomous corpus
  sync, ADR-0006).
- **Container stack**: `ragfarm-stack.service` runs `docker compose up -d` for the
  six container services (see the topology note above), then its `ExecStartPost`
  runs `scripts/mcpo-heal.sh`. Containers also carry `restart: unless-stopped` and
  `docker.service` is enabled — so a normal reboot restarts them anyway; the unit
  guarantees ordering + one control point (install steps in the unit header).

### Start / stop / status everything (host, as `dave`)
Cold start — host model hosts first, then the container stack:
```bash
sudo systemctl start ragfarm-llama ragfarm-embedder ragfarm-ingester-watcher
sudo systemctl start ragfarm-stack
```
Stop everything — stack first, then host services:
```bash
sudo systemctl stop ragfarm-stack
sudo systemctl stop ragfarm-ingester-watcher ragfarm-embedder ragfarm-llama
```
Status at a glance:
```bash
systemctl --no-pager status ragfarm-llama ragfarm-embedder ragfarm-ingester-watcher ragfarm-stack
docker compose -f infra/compose.yaml ps
```

### Restarting individual pieces (the gotchas)
- **rag-retrieval**: any restart severs mcpo's streamable-http MCP session — always
  follow with `scripts/mcpo-heal.sh` (or just `sudo systemctl restart ragfarm-stack`),
  otherwise tools come up unmounted (ADR-0007 note #4; the boot healer is the stack
  unit's `ExecStartPost`).
- **embedder**: a restart drops the lazy-loaded reranker; it reloads (~15–20 s) on
  the next `/rerank` call. `/embed` is ready as soon as `/health` responds.
- Manual `docker compose` ops need the proxy env first: `source scripts/proxy-env.sh`
  (image pulls + container proxy inheritance).

## Operator scripts (`scripts/`) — a newcomer's map
Everything here runs from the repo root on the host as `dave`. Grouped by job.

**Deploy & lifecycle**
- `deploy.sh` — the reproducible, idempotent full deploy of the durable stack:
  ordered phases, each ending in a machine-checkable gate; safe to re-run. Uses
  `sudo` only for the specific systemd install/enable actions (never wraps the whole
  script). The AI-out-of-the-loop path for repeatable deploys.
- `mcpo-heal.sh` — waits until the MCP backends accept TCP, then restarts mcpo once
  so every tool mounts cleanly (works around the streamable-http boot race). Runs as
  the stack unit's `ExecStartPost`; run it by hand after any ad-hoc `rag-retrieval`
  restart.
- `proxy-env.sh` — **`source` it, don't execute**, before any network command (pip /
  HuggingFace / `docker compose`); loads repo-root `.env` and normalizes
  `HTTP(S)_PROXY`/`NO_PROXY` (guarantees loopback + containers bypass the proxy).

**Retrieval / RAG debugging**
- `rag_pool_inspect.py` — dump the first-stage RRF candidate pool for a query
  (`--branches` adds the dense-only and sparse-only lists). Separates a RANKING
  problem (chunk is in the pool but scored low → the reranker's job) from a RECALL
  problem (chunk never entered the pool → needs better first-stage recall / query
  expansion). This is the raw, pre-rerank view.
  ```bash
  .venv/bin/python scripts/rag_pool_inspect.py --branches "hsmbvxip001ts" "proj vedoucí EPC"
  ```
- `ingest-embed-test.sh` — quick smoke test: Qdrant points carry non-empty sparse
  vectors, and a known-hostname `search_corpus` lookup returns rows.

**Agent / tool-routing debugging**
- `dump_mcp_openapi.py` — enumerate every mcpo mount and dump its `openapi.json`
  plus the exact `operationId` the model is shown (e.g. `tool_search_corpus_post`).
  Ground truth when writing routing hints or debugging why the model does/doesn't
  call a tool. `--full` for the whole schema per mount.
- `trace_tool_calls.py` — drive llama-server with the **real** OWUI tool schemas
  (pulled live from mcpo) and the deployed grounding prompt at deterministic
  settings, feeding canned tool results, to watch which tools the 7B calls with what
  arguments across rounds. Regression-check routing after a prompt/schema change.
- `agent.py` — headless multi-turn agent over the same stack (llama-server + mcpo
  tools + the deployed grounding prompt) but with a context loop **we own and
  measure**. Unlike `trace_tool_calls.py` it EXECUTES tools for real (incl. a CLI
  reboot confirm mirroring `reboot_guarded.py`) and carries real history. Modes:
  interactive REPL, one-shot prompts, or scripted benchmarks (`--scenario
  reboot-canary`). Per turn it splits and times the **deliberate / tool / answer**
  stages (prefill vs decode each) and reports context size + how many old tool
  results were elided. This is the control for isolating an OWUI-loop bug from a
  model/stack bug — e.g. it reproduced the repeated-reboot miss with no OWUI in the
  loop, proving that failure is model discipline, not compaction.

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
Until live OpenNebula exists, `mcp-placement` and `mcp-host-control` run in
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
- **Embedder + reranker**: BGE-M3 (`/embed`) and bge-reranker-v2-m3 (`/rerank`, ADR-0008)
  both move onto the GPU (CUDA); keep the `/embed` dense+sparse and `/rerank`
  contracts on `:8090`. Re-ingest is unnecessary if the embedder model+revision are
  unchanged (the reranker touches query time only, so it never requires re-ingest).
- **Networking**: with a single CUDA stack and services able to bind a shared
  interface, the host-networking workaround can be dropped — move containers back to
  a compose bridge network and reach inference/embedder via service DNS or
  `host.docker.internal` (which requires those services to bind beyond loopback).
  Re-evaluate the `0.0.0.0` exposure + firewalling for the target network.
- **mcpo config**: `mcp-placement` (`where_is_vm`) and `mcp-host-control` are
  already mounted in `services/mcp-gateway/mcpo-config.json` and run in MOCK mode.
  When OpenNebula is reachable (steps 05/06 unblock), set `ONE_MOCK=0`/`HOST_MOCK=0`
  and fill `ONE_XMLRPC`/`ONE_AUTH` per `services/mcp-placement/.env.example` — no
  mcpo-config or registration changes needed.
- **Corpus**: `CORPUS_PATH` and the Qdrant `corpus` collection (dense 1024 + sparse)
  are portable; re-run `services/ingester/ingester.py --recreate` against prod corpus.
