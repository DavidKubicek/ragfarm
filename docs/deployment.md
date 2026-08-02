# Deployment & prod re-deployment notes
Author: David Kubicek (david.kubicek@eywo.cz)

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
| host | reranker | (built at `~/llama.cpp`) | — (systemd `ragfarm-reranker`) | `127.0.0.1:8081` `/reranking` | — | internal — bge-reranker-v2-m3 GGUF, **iGPU/Vulkan** (ADR-0008) | no |
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
rag-retrieval, mcp-placement, mcp-host-control, mcpo, open-webui. The reranker is a
**second host `llama-server`** on the iGPU (`:8081 --reranking`, ADR-0008) — a
Vulkan sibling of the LLM, not an embedder endpoint; the embedder is embeddings-only.
So there are **two `llama-server` processes**: the LLM (`:8080`) and the reranker
(`:8081`), both on the iGPU.

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

## Tested model versions (known-good baseline)
The fetch scripts default to **latest** (easy swapping/experimentation), but the model
set every gate + eval in this repo was validated against is the pinned one below. Pass
the `--revision` flags to reproduce it exactly; omit them for latest. These pins are for
**reproducibility only** — not a security constraint (the old no-pickle rule is retired,
see the model-format note in `scripts/lib-models.sh`).

| role | model | tested revision | reproduce with |
|------|-------|-----------------|----------------|
| LLM | Qwen2.5-7B-Instruct Q4_K_M GGUF (`Qwen/Qwen2.5-7B-Instruct-GGUF`) | GGUF, current | `scripts/fetch-llm.sh` |
| embedder | `BAAI/bge-m3` | `50f9396f75618b3389c1fd1068a1ff58dc7b5b26` (has `model.safetensors`; HEAD ships only `pytorch_model.bin`) | `scripts/fetch-encoder.sh --embed-revision 50f9396f75618b3389c1fd1068a1ff58dc7b5b26` |
| reranker | `BAAI/bge-reranker-v2-m3` | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` | `scripts/fetch-encoder.sh --rerank-revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |

The embedder + reranker must stay a **compatible pair** (`fetch-encoder.sh --list`); the
currently-deployed embedder is the safetensors revision above (same weights as latest,
faster load). Per-model detail lives in `models/{llm,embeddings,reranker}/MODEL.md`.

## Autostart & lifecycle

### What starts on boot
- **Host services** (systemd, already `enabled`): `ragfarm-llama.service` (LLM),
  `ragfarm-reranker.service` (iGPU cross-encoder, ADR-0008),
  `ragfarm-embedder.service` (CPU embeddings),
  `ragfarm-ingester-watcher.service` (autonomous corpus sync, ADR-0006).
- **Container stack**: `ragfarm-stack.service` runs `docker compose up -d` for the
  six container services (see the topology note above), then its `ExecStartPost`
  runs `scripts/mcpo-heal.sh`. Containers also carry `restart: unless-stopped` and
  `docker.service` is enabled — so a normal reboot restarts them anyway; the unit
  guarantees ordering + one control point (install steps in the unit header).

### Start / stop / status everything (host, as `dave`)

**One command (recommended)** — `scripts/stack.sh` runs the whole ordered sequence
and health-checks the result. Reach for this unless you're debugging one service:
```bash
scripts/stack.sh start      # host services → container stack → health check
scripts/stack.sh stop       # container stack → host services (reverse order)
scripts/stack.sh restart    # stop, then start
scripts/stack.sh status     # systemd unit + container status at a glance
scripts/stack.sh health     # probe every endpoint; non-zero exit if any is down
```

The explicit per-service sequence below does exactly the same thing — use it when you
need to bring one piece up or down on its own.

Cold start — host model hosts first, then the container stack:
```bash
sudo systemctl start ragfarm-llama ragfarm-reranker ragfarm-embedder ragfarm-ingester-watcher
sudo systemctl start ragfarm-stack
```
Stop everything — stack first, then host services:
```bash
sudo systemctl stop ragfarm-stack
sudo systemctl stop ragfarm-ingester-watcher ragfarm-embedder ragfarm-reranker ragfarm-llama
```
Status at a glance:
```bash
systemctl --no-pager status ragfarm-llama ragfarm-reranker ragfarm-embedder ragfarm-ingester-watcher ragfarm-stack
docker compose -f infra/compose.yaml ps
```

### Restarting individual pieces (the gotchas)
- **rag-retrieval**: any restart severs mcpo's streamable-http MCP session — always
  follow with `scripts/mcpo-heal.sh` (or just `sudo systemctl restart ragfarm-stack`),
  otherwise tools come up unmounted (ADR-0007 note #4; the boot healer is the stack
  unit's `ExecStartPost`).
- **reranker & embedder**: independent host services now (a GPU `llama-server` on
  `:8081` and the CPU embedder on `:8090`); restarting one doesn't touch the other.
  The reranker loads its ~1.2 GB GGUF on the iGPU in ~1–2 s; rag-retrieval reaches it
  via `RERANK_ENDPOINT` (`:8081/reranking`). One gotcha: llama.cpp reranking scores
  each `(query,doc)` pair in one physical batch, so the unit sets `-b/-ub 4096` — if
  chunks ever grow past that, raise it (see `ragfarm-reranker.service`).
- Manual `docker compose` ops need the proxy env first: `source scripts/proxy-env.sh`
  (image pulls + container proxy inheritance).

## Operator scripts (`scripts/`) — a newcomer's map
Everything here runs from the repo root on the host as `dave`. Grouped by job.

**Deploy & lifecycle**
- `stack.sh` — the **one-command operator entry point**: `stack.sh {start|stop|
  restart|status|health}`. Brings the whole stack (host `llama`/`reranker`/`embedder`/
  `ingester-watcher` units + the container stack) up or down in the right order and
  health-checks every endpoint. Use this for day-to-day start/stop; the per-service
  systemd sequence is only for debugging one piece (see Autostart & lifecycle above).
- `deploy.sh` — the reproducible, idempotent full deploy of the durable stack:
  ordered phases (preflight → venv → host services → stack → corpus → watcher →
  verify), each ending in a machine-checkable gate; safe to re-run. Uses `sudo` only
  for the specific systemd install/enable actions (never wraps the whole script).
  Picks a Python dependency profile — `--profile cpu` (default) or `cu13` — pinned in
  `services/requirements.lock` / `requirements.cu13.lock` (identical package set;
  only the torch wheel index differs, CPU vs CUDA 13.x). Builds the reranker GGUF if
  absent and installs all host units incl. `ragfarm-reranker`. The AI-out-of-the-loop
  path for repeatable deploys.
- `mcpo-heal.sh` — waits until the MCP backends accept TCP, then restarts mcpo once
  so every tool mounts cleanly (works around the streamable-http boot race). Runs as
  the stack unit's `ExecStartPost`; run it by hand after any ad-hoc `rag-retrieval`
  restart.
- `proxy-env.sh` — **`source` it, don't execute**, before any network command (pip /
  HuggingFace / `docker compose`); loads repo-root `.env` and normalizes
  `HTTP(S)_PROXY`/`NO_PROXY` (guarantees loopback + containers bypass the proxy).

**Model management (fetch / hot-swap)**
- `fetch-llm.sh` — fetch/swap the generative LLM GGUF into `models/llm/<slug>/` and
  write `LLM_GGUF_PATH` to `.env` (the unit reads it). `--list` shows known-good
  tool-calling LLMs; `--repo`/`--file` swap; `--force` re-fetch. Restart `ragfarm-llama`.
  **Vision models** (e.g. Qwen2.5-VL) are handled automatically, no separate flag: a
  vision-language GGUF needs a second, small "multimodal projector" GGUF passed to
  llama-server as `--mmproj`, or it loads but can't see images. Before downloading,
  the script checks whether the HF repo itself hosts a `*mmproj*.gguf`; if so it
  fetches that too and writes `LLM_GGUF_MMPROJ`, else (plain text model) it **clears**
  that var — so switching back to a text-only model can't launch with a leftover
  `--mmproj` from a previous vision model. Example (fetches both files):
  ```bash
  scripts/fetch-llm.sh --repo ggml-org/Qwen2.5-VL-7B-Instruct-GGUF \
    --file 'Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf'
  ```
- `activate-llm.sh` — switch the active LLM among models **already on disk**, no
  re-download. Lists every `models/llm/<slug>/` that has a complete GGUF (interactive
  numbered prompt, or `--dir <slug>` / `--path <file>` non-interactively; `--list`
  prints and exits), and writes `LLM_GGUF_PATH` + `LLM_GGUF_MMPROJ` to `.env` using the
  same mmproj auto-detect/clear rule as `fetch-llm.sh`. This is the tool for "I already
  fetched three models, which one is live" — fetch once per model, activate freely
  between them:
  ```bash
  scripts/activate-llm.sh --list                       # what's on disk
  scripts/activate-llm.sh --dir qwen2.5-32b-instruct-gguf   # switch to it
  sudo systemctl restart ragfarm-llama                  # apply
  ```
- `fetch-encoder.sh` — fetch/swap the **matched** embedder+reranker pair into
  `models/embeddings/<slug>/` and `models/reranker/<slug>/` (converts the reranker to
  GGUF), writing `EMBED_MODEL_PATH` + `RERANK_GGUF_PATH` to `.env`. `--list` shows
  compatible pairs. Restart `ragfarm-embedder ragfarm-reranker`. Both fetch latest,
  auto-pick the fastest weight format, and are the ONE place download logic lives —
  `deploy.sh`'s venv phase calls them (no duplication). `lib-models.sh` is their
  shared helper (sourced, not run).

**Activating a swapped model** — the fetch/activate scripts only write `.env`; the
units read it on (re)start. What you must do after a swap depends on which model changed:
- **LLM** (`fetch-llm.sh` / `activate-llm.sh`) → `sudo systemctl restart ragfarm-llama`.
  Nothing else — the LLM doesn't touch embeddings or the corpus. (If the model *alias*
  changed, also re-run `infra/openwebui/setup_openwebui.py` so the OWUI preset points at
  it.) The unit's `ExecStart` conditionally adds `--mmproj` only when `LLM_GGUF_MMPROJ`
  is non-empty, so a text-only `.env` (the common case) launches exactly as before —
  the mmproj knob is a no-op unless you're running a vision model.
- **Reranker only** → `sudo systemctl restart ragfarm-reranker`. Query-time only; no re-ingest.
- **Embedder** (`fetch-encoder.sh`) → the vector space changes, so you MUST re-embed:
  `sudo systemctl restart ragfarm-embedder ragfarm-reranker` **then**
  `.venv/bin/python services/ingester/ingester.py --recreate --corpus /data/corpus`
  (or `scripts/deploy.sh --recreate-corpus`). **Skipping the re-ingest silently breaks
  retrieval** — old stored vectors vs new query vectors. `scripts/stack.sh restart`
  restarts every service but does **not** re-ingest; that step is always manual.
  (`fetch-encoder.sh` prints this reminder when it actually re-fetched the embedder.)

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
`infra/openwebui/setup_openwebui.py` (idempotent) — this pushes TWO presets in
one pass (text + vision, see ADR-0009 and the "Vision engine" section below):
- **Tool servers** `TOOL_SERVER_CONNECTIONS` → `/rag` (`server:0`) + `/placement`
  (`server:1`), both `path=openapi.json`, `auth_type=none`.
- **Preset `ragfarm`** (text): base `qwen2.5-7b-instruct` + rag/placement/reboot_guarded
  attached (`meta.toolIds`) + `params.function_calling=native` + greedy sampler
  (`temp=0, top_k=1, seed=42`) + the RULE-1..5 grounding prompt. Loadbearing:
  without it the 7B answers generic prose even when the exact chunk was retrieved.
- **Preset `ragfarm-vision`** (VL): base auto-detected via `/v1/models` (first
  entry with capability `multimodal`, overridable via `VISION_BASE_MODEL_ID`),
  non-greedy sampler (`temp=0.6`, `top_k/top_p/min_p/seed` DROPPED so llama.cpp
  defaults apply — required by Qwen3-VL Thinking) + vision + file-upload
  capabilities on + a RULE-1..6 prompt that adds image-input rules and a draw.io
  HTML template pointing at the local viewer (`127.0.0.1:8091`, see
  `drawio-viewer` service in `infra/compose.yaml`).
- Capabilities matrix (both presets): file_context, file_upload, web_search,
  code_interpreter, citations, status_updates, usage, builtin_tools all ON;
  image_generation, terminal OFF; vision only on VL preset.
- Default features (per-chat pre-selected): web_search + code_interpreter.
- Builtin tools: everything ON except knowledge and calendar (OWUI opt-out
  convention — absence = enabled).
- OpenAI endpoint: `OPENAI_API_BASE_URL=http://127.0.0.1:8080/v1` (compose env).
- Open WebUI's built-in document RAG is deliberately unused for the corpus (Option B).

Run it (admin token, or email + password on the CLI):
```bash
OWUI_URL=http://127.0.0.1:3000 OWUI_TOKEN=<admin JWT> \
  .venv/bin/python infra/openwebui/setup_openwebui.py
```
Only one llama-server model is loaded at a time — the preset whose
`base_model_id` matches the wrapper's `--alias` is the one that actually works
right now; the other stays as stored config waiting for the next model swap.

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
  Repoint `OPENAI_API_BASE_URL` only. Keep the OpenAI-compatible contract. This is
  also the moment to move off the 7B: a ~30B model (e.g. Qwen2.5-32B) is expected to
  resolve most observed 7B issues — tool-calling discipline (the repeated-reboot miss,
  ADR-0008/agent.py), verbose rambling answers, and instruction-following — and brings
  a larger native context. Tensor-parallel across two GPUs buys still-larger context /
  throughput if needed. Nothing in the durable layer changes (ADR-0003).
- **Embedder**: BGE-M3 (`/embed`, currently CPU) moves onto the GPU (CUDA); keep the
  dense+sparse contract on `:8090`. Re-ingest is unnecessary if model+revision are unchanged.
- **Reranker**: already GPU-accelerated on the iGPU via llama.cpp/Vulkan
  (`:8081 --reranking`, ADR-0008). On prod it swaps the Vulkan build for a CUDA one (or
  is served by the same CUDA inference stack); the `/reranking` contract is unchanged.
  Query-time only — never requires re-ingest.
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

## Vision engine (Qwen3-VL family — ADR-0009)

Two OWUI presets coexist; only one is *live* at a time (the one whose
`base_model_id` matches the wrapper's `--alias`). To flip between text and
vision, or between Instruct and Thinking variants, use `activate-llm.sh`:

```bash
scripts/activate-llm.sh --list                                 # what's on disk
scripts/activate-llm.sh --dir qwen_qwen3-vl-8b-thinking-gguf   # switch to vision
sudo systemctl restart ragfarm-llama                            # apply (~30-60 s to reload)
```

The wrapper (`scripts/llama-launch.sh`) auto-derives `--alias` from the model's
directory name and conditionally adds `--mmproj` when the model dir contains a
`*mmproj*.gguf` (vision models). No unit edits, no other flags to touch.

### Live capabilities that Just Work

The Qwen3-VL preset has already been verified end-to-end on this stack:
- **OCR from image URL** — see the demo commands below. Auntie Anne's Indonesian
  receipt from the openlm.ai example was OCR'd correctly (all prices, all
  fields), at ~8 tok/s decode on the iGPU.
- **Image description** — attach any image in OWUI chat; the model describes
  content verbatim (RULE 4 of the vision prompt: no invention, verbatim OCR).
- **Diagram scan → regenerate as mermaid or draw.io** — screenshot a hand-drawn
  or existing diagram, ask "regenerate this as draw.io" (or "as mermaid").
- **Prompt-modify-synthesize** — attach an image, ask "same structure but add a
  reranker box between retrieval and generation". The model reads the input as
  a graph, applies your edit, and re-emits in the chosen format.

### Diagram rendering

`ragfarm-vision`'s system prompt asks the user which format they want (never
routes silently, never emits both):

- **Mermaid** — the user says "mermaid". Rendered natively by OWUI as an SVG.
- **draw.io** — the user says "draw.io" / "drawio" / "editable" / "interactive"
  / "pan-zoom". The model emits a fenced ```html block wrapping the raw
  `<mxfile>` XML plus a script tag pointing at `http://127.0.0.1:8091/viewer-static.min.js`
  (the local `drawio-viewer` nginx container). OWUI's HTML preview iframe
  loads it — pan/zoom/lightbox/layer toggle work in-chat.

The IFRAME_CSP is set to allow scripts from `127.0.0.1:8091` only; nothing else
opens up. The viewer's own external calls to `viewer.diagrams.net/styles` &
`/shapes` may 404 on an offline demo box; the core rendering still works, only
fancy stencil sets are missing.

### Verified demo commands (no prep needed, run today)

The tests below use the live stack as-is. Nothing to fetch, nothing to install.

**Sanity: vision model loaded?**
```bash
curl -s 127.0.0.1:8080/v1/models | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["models"][0]["model"], d["models"][0].get("capabilities"))'
# expect: qwen_qwen3-vl-8b-thinking ['completion', 'multimodal']
```

**OCR from a public image (via base64 to bypass llama-server's HTTPS-off build):**
```bash
.venv/bin/python - <<'PY'
import base64, requests, time
IMG = "https://ofasys-multimodal-wlcb-3-toshanghai.oss-cn-shanghai.aliyuncs.com/wpf272043/keepme/image/receipt.png"
img = requests.get(IMG, timeout=30); img.raise_for_status()
uri = f"data:{img.headers['content-type']};base64,{base64.b64encode(img.content).decode()}"
t0 = time.time()
r = requests.post("http://127.0.0.1:8080/v1/chat/completions", json={
    "model": "qwen_qwen3-vl-8b-thinking",
    "messages": [{"role":"user","content":[
        {"type":"image_url","image_url":{"url":uri}},
        {"type":"text","text":"Read all the text in the image."}]}],
    "max_tokens": 512, "temperature": 0.6,
}, timeout=120)
print(f"HTTP {r.status_code}  {time.time()-t0:.1f}s")
print(r.json()["choices"][0]["message"]["content"])
PY
```

**In-UI vision demo (OWUI, admin@ragfarm.local):**
1. Select model **`ragfarm-vision`** from the dropdown.
2. Click the paperclip → upload any image (receipt, screenshot of a diagram,
   photo of a whiteboard).
3. Ask one of: "OCR everything in this image", "describe what you see",
   "regenerate this diagram as mermaid" / "as draw.io".
4. For a diagram request, add `/no_think` to the prompt if the Thinking trace
   makes the wait too long (Qwen3 chat-template convention: strips the
   `<think>...</think>` block for that turn).

**Serve local test images to the model:** the same `drawio-viewer` nginx also
serves anything under `infra/drawio-viewer/`. Drop a `.png` / `.pdf` there and
reference `http://127.0.0.1:8091/<file>` in a prompt. Useful for a repeatable
demo without leaning on external URLs.

### /think vs /no_think (Qwen3 Thinking control)

Qwen3 parses these tokens out of user messages (chat-template convention, NOT
system-prompt rules):
- **`/think`** — force the model to emit a `<think>...</think>` block before the
  answer. Default for `*-Thinking-*` model variants.
- **`/no_think`** — suppress the reasoning block for this turn. Runs a Thinking
  model like an Instruct one; ~3-5× snappier answers, at the cost of the
  reasoning quality on hard multi-step prompts.

Practical demo advice: default (thinking on) for the first, hardest question of
the day (RAG lookup, complex OCR); append `/no_think` for follow-ups, quick
lookups, and diagram requests where the trace adds no value.

## Debug & measurement (`tests/tracing/`)

Standalone Python tools (no dependencies beyond `requests`) that answer *where
did the time go* for any inference or chat turn. All safe to run against the
live stack — read-only, no side effects.

Every tool takes `--url http://127.0.0.1:8080` (or the equivalent flag) so
they're portable across the swappable llama-server endpoint. Default endpoint
in the code is `localhost:8001` — always pass `--url` explicitly.

### The catalog

| Tool | What it measures | When to use |
|------|------------------|-------------|
| `ragfarm_bench.py` | Basic prefill/decode tok/s + TTFT + E2E per prompt | Quick "is llama fast enough right now" check |
| `ragfarm_bench_extended.py` | Full per-stage timings, absolute token+byte counts, context growth, CSV/JSON export | Regression tracking across commits or hardware swaps |
| `ragfarm_bench_chatid.py` | Same, plus context-blowup detection per chat session | Find when a conversation starts overflowing context |
| `chat_execution_tracer.py` | Chat session timeline: user→prefill→tool decision→tool exec→decision phase→generation, with orchestration-overhead % | Diagnose "chat feels slow" vs "LLM is slow" |
| `ragfarm_tracer_simple.py` | One-shot telemetry query: which model is loaded, endpoint latencies | Sanity check before any deeper trace |
| `ragfarm_integrated_tracer.py` | Combined engine telemetry + pipeline trace demo | Report generation for a whole pipeline |
| `ragfarm_rag_tracer.py` | RAG candidate-pool evolution: Qdrant → RRF → reranker, per-stage tokens/latency | Debug retrieval quality regressions ("why didn't my chunk win?") |

*(Not integrated: `ragfarm_http_tracer.py` — proxy-based, requires reconfiguring
OWUI's LLM endpoint. Its only unique benefit is E2E prompt-answer wall time,
which every other tool measures anyway.)*

### Verified one-liners (run today)

```bash
# 1. What model is loaded and how fast does one prompt run?
.venv/bin/python tests/tracing/ragfarm_tracer_simple.py query \
    --generation localhost:8080 --reranker localhost:8081

# 2. Baseline bench, one prompt, small max_tokens (fast)
.venv/bin/python tests/tracing/ragfarm_bench.py \
    --url http://127.0.0.1:8080 --prompt 1 --max-tokens 40

# 3. Extended bench, 3 prompts with CSV export (regression file)
.venv/bin/python tests/tracing/ragfarm_bench_extended.py \
    --url http://127.0.0.1:8080 --prompt 3 --max-tokens 128 \
    --csv bench_$(date +%s).csv

# 4. Chat tracer demo (canned session; no LLM required)
.venv/bin/python tests/tracing/chat_execution_tracer.py --demo
# writes chat_trace_demo.json in cwd

# 5. Real RAG pipeline trace for one query (endpoint is mcpo at :8000,
#    which mounts rag under /rag/search_corpus — NOT direct rag-retrieval :8104)
.venv/bin/python tests/tracing/ragfarm_rag_tracer.py trace \
    --chat-id demo_$(date +%s) \
    --query "FW pravidla pro host leadb229p.lea.piz" \
    --rag-endpoint http://127.0.0.1:8000
```

### What "good" looks like on this iGPU (Qwen3-VL-8B-Thinking Q4_K_M, current baseline)

| Metric | Now | Comment |
|--------|-----|---------|
| Decode | ~8-9 tok/s | LPDDR5x bandwidth-bound, both 7B and 8B land here |
| Prefill | ~170 tok/s | The 2-order-of-magnitude speedup vs decode is expected |
| Vision OCR (dense receipt) | ~40 s | 300+ output tokens at 8 tok/s |
| Reranker turn | ~1.9 s | Since ADR-0008 moved it to GPU/Vulkan (was ~36 s on CPU) |
| Tool overhead | ~15-25 % of chat turn | Higher with reboot_guarded modal, lower for pure RAG |

Baseline numbers older docs cite (300 tok/s prefill, 1000 tok/s decode) came
from a much lighter model; the current live setup is intentionally slower and
smarter. Track deltas from this table, not from those.
