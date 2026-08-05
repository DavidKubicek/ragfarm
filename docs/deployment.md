# Deployment & prod re-deployment notes
Author: David Kubicek (david.kubicek@eywo.cz)

Operational facts needed to run and redeploy the system. **ADR-0013/0003 hold the
*why*; this holds the concrete *what*.**

> **Hardware note.** This system now runs on an **NVIDIA DGX Spark (GB10, sm_121,
> 128 GB unified memory)**. It was prototyped on an AMD Ryzen MiniPC (iGPU +
> Vulkan + GGUF), and that migration is complete — ADR-0013 supersedes ADR-0001.
> AMD-specific mechanics have been removed from this document; what is deliberately
> kept is (a) **model-behaviour knowledge**, which is hardware-independent and was
> expensive to learn, and (b) the **AMD baseline performance table**, retained as
> the before/after comparison. Both are marked where they appear.

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
| host | vllm | `.venv-vllm` | — (systemd `ragfarm-vllm`) | `127.0.0.1:8080` | — | OpenAI base URL (swappable) | no |
| host | embedder | `services/embedder` | — (systemd `ragfarm-embedder`) | `127.0.0.1:8090` `/embed` | — | internal — dense+sparse embed, **CUDA** | no |
| host | reranker | (built at `~/llama.cpp`) | — (systemd `ragfarm-reranker`) | `127.0.0.1:8081` `/reranking` | — | internal — bge-reranker-v2-m3 GGUF, **CUDA** (ADR-0008) | no |
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
rag-retrieval, mcp-placement, mcp-host-control, mcpo, open-webui.

**Three processes share the one GPU and the one memory pipe** — there is no separate
VRAM on GB10 (ADR-0013 §5). Measured while idle-but-loaded:

| process | GPU memory |
|---|---|
| vLLM (`:8080`, `LLM_GPU_MEM_UTIL=0.50`) | ~59.5 GB |
| embedder BGE-M3 (`:8090`) | ~1.6 GB |
| reranker llama.cpp (`:8081`) | ~0.9 GB |
| **total** | **~62 GB of 121 GB** |

`llama-server` now runs **once**, for the reranker only (`:8081 --reranking`,
ADR-0008/0013 §3) — generation moved to vLLM. The embedder is embeddings-only.
Argue any "let's also run X" against the table above, not against a spare-capacity
assumption.

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

| role | model | deployed revision | reproduce with |
|------|-------|-----------------|----------------|
| LLM | `ig1/Qwen3-VL-30B-A3B-Instruct-NVFP4` (NVFP4 safetensors, 17.9 GiB, vision) | `3c6162d5513d26f008628eebe9b4355559b4a305` | step-02 fragment in `scripts/deploy.sh` (`snapshot_download`) |
| embedder | `BAAI/bge-m3` | `5617a9f61b028005a4858fdac845db406aefb181` (latest; this repo ships **only** `pytorch_model.bin` — it has no `model.safetensors`) | `scripts/fetch-encoder.sh` |
| reranker | `BAAI/bge-reranker-v2-m3` | latest, converted to f16 GGUF locally | `scripts/fetch-encoder.sh` |

Served LLM alias is **`qwen3-vl-30b-a3b`** and it is **load-bearing**:
`infra/openwebui/setup_openwebui.py` keys `MODEL_TUNING` by the alias advertised on
`/v1/models`, so serving under another name makes the `ragfarm-vision` preset
silently fail to bind.

The embedder + reranker must stay a **compatible pair** (`fetch-encoder.sh --list`).
Per-model detail lives in `models/{llm,embeddings,reranker}/MODEL.md` — including the
measured NVFP4 MoE backend findings, which contradict parts of ADR-0013 and are worth
reading before touching the serving flags.

> **Older baseline, for reference only:** the PoC ran Qwen2.5-7B-Instruct Q4_K_M GGUF
> with bge-m3 at `50f9396f…`. Neither is deployed now; GGUF is the retired llama.cpp
> generation path.

## Autostart & lifecycle

### What starts on boot
- **Host services** (systemd, already `enabled`): `ragfarm-vllm.service` (LLM, CUDA),
  `ragfarm-reranker.service` (CUDA cross-encoder, ADR-0008; `Nice=5`),
  `ragfarm-embedder.service` (CUDA embeddings),
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
sudo systemctl start ragfarm-vllm ragfarm-reranker ragfarm-embedder ragfarm-ingester-watcher
sudo systemctl start ragfarm-stack
```
Stop everything — stack first, then host services:
```bash
sudo systemctl stop ragfarm-stack
sudo systemctl stop ragfarm-ingester-watcher ragfarm-embedder ragfarm-reranker ragfarm-vllm
```
Status at a glance:
```bash
systemctl --no-pager status ragfarm-vllm ragfarm-reranker ragfarm-embedder ragfarm-ingester-watcher ragfarm-stack
docker compose -f infra/compose.yaml ps
```

### Restarting individual pieces (the gotchas)
- **rag-retrieval**: any restart severs mcpo's streamable-http MCP session — always
  follow with `scripts/mcpo-heal.sh` (or just `sudo systemctl restart ragfarm-stack`),
  otherwise tools come up unmounted (ADR-0007 note #4; the boot healer is the stack
  unit's `ExecStartPost`).
- **reranker & embedder**: independent host services (a CUDA `llama-server` on `:8081`
  and the CUDA embedder on `:8090`); restarting one doesn't touch the other. The
  reranker loads its ~1.1 GB GGUF in ~1–2 s; rag-retrieval reaches it via
  `RERANK_ENDPOINT` (`:8081/reranking`). One gotcha: llama.cpp reranking scores each
  `(query,doc)` pair in one physical batch, so the unit sets `-b/-ub 4096` — if chunks
  ever grow past that, raise it (see `ragfarm-reranker.service`).
- **vLLM cold start is slow, and that is not a hang.** First start after a cache miss
  JIT-compiles FlashInfer kernels: measured **813 s cold vs ~3 min warm**. Two things
  make this survivable and both live in `.env`:
  `MAX_JOBS` (caps parallel CUDA compiles — unset it defaults to `nproc+2` and the
  OOM killer takes the service at ~98 GB) and `LLM_GPU_MEM_UTIL` (steady-state
  weights+KV budget). See `.env.example` and ADR-0013 §5a — they are *different*
  budgets, and lowering the vLLM memory flags does nothing for the cold-start peak.
  The unit has `StartLimitBurst=3`, so a genuinely broken config goes `failed`
  instead of crash-looping while `systemctl is-active` says `activating`.

### GPU contention & the reranker's priority (tunable — revisit with real load)

`ragfarm-reranker.service` runs at **`Nice=5`**, a deliberately mild de-prioritisation
so the interactive LLM wins contention (ADR-0013 §3). It is set low on purpose: we are
**single-user today**, and the point is to observe how much these three actually fight
over the GPU before tuning harder.

**Know what `Nice` does and does not buy.** It is CPU scheduling only — it does *not*
yield GPU time. Kernel ordering on the device is the driver's business, and nothing in
the unit deprioritises the reranker's CUDA work. What it does help with is the
host-side half: HTTP handling, tokenisation, and the batch marshalling around each
rerank call.

To tune (any of these, no code changes):
- raise/lower `Nice=` in `manifests/ragfarm-reranker.service`, reinstall, restart;
- if *GPU* contention is the real problem, the levers are CUDA MPS priorities, or
  simply not running a bulk re-rank concurrently with interactive generation;
- if the reranker is starving the LLM of memory rather than compute, lower
  `LLM_GPU_MEM_UTIL` and re-check the topology table above.

Measure before tuning: a rerank turn was ~1.9 s on the old iGPU and the model is
unchanged, so if it regresses noticeably under concurrent load, that is the signal —
not a number anyone should guess at up front.
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

> **REPLACED 2026-08-05 by `scripts/fetch_llm.py` + `scripts/activate_llm.py`,
> driven by the git-tracked registry `models/llm/active.json` (ADR-0013 §2a).**
>
> ```bash
> scripts/fetch_llm.py -m Firworks/Qwen3-VL-32B-Thinking-nvfp4   # register + download
> scripts/fetch_llm.py --sync        # fetch everything the registry lists as missing
> scripts/fetch_llm.py --verify      # byte-check what is on disk against the Hub
> scripts/activate_llm.py -s 0 -a qwen3-vl-30b-a3b-thinking-nvfp4   # bind slot 0
> scripts/activate_llm.py --status   # slots, ports, GPU budget
> ```
>
> **SLOTS.** vLLM serves ONE base model per process, so two resident models means
> two processes: `ragfarm-vllm@N.service` on port `8080 + 2N` (8081 is the
> reranker), each with its own `.env.slotN` written by `activate_llm.py`. This is
> what allows switching model **mid-chat** in Open WebUI with the conversation
> intact. `--gpu-memory-utilization` is per process and instances do NOT
> coordinate — two slots at the single-model 0.50 will OOM; `activate_llm.py`
> derives each slot's share and refuses to exceed a 0.72 total.
>
> `activate_llm.py` errors out on a registry with duplicate `model`/`alias`/`preset`
> rather than auto-fixing it: those names show up in the UI.
>
> The GGUF-era text below is kept because the mechanics are still accurate for
> llama.cpp, which still serves the reranker. `fetch-encoder.sh` is NOT retired.

> **`fetch-llm.sh` and `activate-llm.sh` are RETIRED for generation (ADR-0013).**
> They are GGUF/`--mmproj`/llama.cpp-shaped, the Spark serves NVFP4 safetensors on
> vLLM, and `deploy.sh` no longer calls them. Kept below because the *mechanics* are
> still accurate for llama.cpp and no vLLM-shaped replacement exists yet. To change
> the served LLM today: edit `LLM_MODEL_PATH`/`LLM_SERVED_NAME` in `.env` and restart
> `ragfarm-vllm`. `fetch-encoder.sh` is NOT retired — it still fetches the
> embedder+reranker pair and is called by `deploy.sh`.
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
  sudo systemctl restart ragfarm-llama                  # RETIRED: unit no longer exists
  ```
- `fetch-encoder.sh` — fetch/swap the **matched** embedder+reranker pair into
  `models/embeddings/<slug>/` and `models/reranker/<slug>/` (converts the reranker to
  GGUF), writing `EMBED_MODEL_PATH` + `RERANK_MODEL_PATH` to `.env`. `--list` shows
  compatible pairs. Restart `ragfarm-embedder ragfarm-reranker`. Both fetch latest,
  auto-pick the fastest weight format, and are the ONE place download logic lives —
  `deploy.sh`'s venv phase calls them (no duplication). `lib-models.sh` is their
  shared helper (sourced, not run).

**Activating a swapped model** — the fetch/activate scripts only write `.env`; the
units read it on (re)start. What you must do after a swap depends on which model changed:
- **LLM** → edit `.env` (`LLM_MODEL_PATH`), then `sudo systemctl restart ragfarm-vllm`.
  Nothing else — the LLM doesn't touch embeddings or the corpus. (If the model *alias*
  changed, also re-run `infra/openwebui/setup_openwebui.py` so the OWUI preset points at
  There is no `--mmproj` step any more: the old llama.cpp unit attached a separate
  multimodal projector GGUF, whereas the vLLM checkpoint carries vision natively.
  `LLM_MMPROJ_PATH` / `LLM_GGUF_MMPROJ` are inert on the Spark.
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

## The AMD → Spark migration (DONE) and what is still open

ADR-0003's central claim held up: the durable layer (Open WebUI, mcpo, MCP servers,
`search_corpus`, Qdrant) is hardware-agnostic and **was not re-architected**. The
whole engine swap touched the serving plane and almost nothing else. Keep it that way.

**Done, 2026-08-03/04 (build steps 01–04):**
- **Inference** — llama.cpp/Vulkan/GGUF → **vLLM 0.26.0 serving NVFP4 safetensors**,
  same OpenAI-compatible contract on `:8080`. The model moved 7B → **30B-A3B MoE**.
- **Embedder** — BGE-M3 CPU → **CUDA**, same dense+sparse contract on `:8090`. The
  service now *refuses to start* without CUDA rather than silently falling back.
- **Reranker** — Vulkan → **CUDA** llama.cpp, `/reranking` contract unchanged. This is
  the only remaining llama.cpp user. Query-time only — never requires re-ingest.
- **Corpus** — re-ingested from scratch on the Spark: 5 files → 183 points, dense 1024
  + sparse, alias `corpus` (ADR-0006).

**Still open:**
- **Networking**: the `network_mode: host` workaround is still in place and still
  load-bearing for the same reason as before (host services bind loopback only). It
  *could* now be revisited, but nothing forces it — treat it as optional cleanup, and
  re-evaluate the `0.0.0.0` exposure + firewalling for the target network first.
- **mcpo config**: `mcp-placement` (`where_is_vm`) and `mcp-host-control` are already
  mounted in `services/mcp-gateway/mcpo-config.json` and run in MOCK mode. When
  OpenNebula is reachable (steps 05/06 unblock), set `ONE_MOCK=0`/`HOST_MOCK=0` and
  fill `ONE_XMLRPC`/`ONE_AUTH` per `services/mcp-placement/.env.example` — no
  mcpo-config or registration changes needed.
- **Did the bigger model fix the 7B problems?** The move to ~30B was expected to
  resolve tool-calling discipline (the repeated-reboot miss), rambling answers, and
  instruction-following. **Unverified end-to-end** — tool-calling works at the API
  level (`get_weather` with parsed args), but the agent-layer behaviour is step 07.
  Re-test rather than assuming; see the prompt-design notes below, several of which
  were scaffolding for an 8B and may now be unnecessary.
- **GGUF-era scripts**: `fetch-llm.sh`, `activate-llm.sh`, `llama-launch.sh` still
  assume GGUF + `--mmproj` and are **unused for generation**. `deploy.sh` no longer
  calls them. A vLLM-shaped model-swap equivalent does not exist yet.
- **`LLAMA_DIR` is half-honoured**: `deploy.sh` and `fetch-encoder.sh` respect it,
  `ragfarm-reranker.service` and `llama-launch.sh` hardcode `/home/dave/llama.cpp`.
  Either make the units read it or drop the variable — don't leave it half-and-half.

## Vision engine (Qwen3-VL family — ADR-0009)

**There is one model now, and it is the vision model.** `Qwen3-VL-30B-A3B-Instruct`
is both the general and the vision engine on the Spark — there is no separate text
model to flip to, and no `--mmproj` to attach: vision is native to the checkpoint and
vLLM serves it directly (ADR-0009 as amended by ADR-0013).

To change the served model, edit `LLM_MODEL_PATH` / `LLM_SERVED_NAME` in `.env` and
`sudo systemctl restart ragfarm-vllm`. Keep the alias in step with the `MODEL_TUNING`
key in `infra/openwebui/setup_openwebui.py` or the preset silently stops binding.

> The old `activate-llm.sh --dir …` + `--mmproj` flow documented here was the GGUF /
> llama.cpp mechanism. Those scripts still exist but are **not** used for generation,
> and there is no vLLM-shaped replacement yet (see "Still open" above).

### Live capabilities that Just Work

The Qwen3-VL preset has already been verified end-to-end on this stack:
- **OCR from image URL** — see the demo commands below. Auntie Anne's Indonesian
  receipt from the openlm.ai example was OCR'd correctly (all prices, all
  fields). Measured at ~8 tok/s decode on the OLD AMD iGPU; the Spark decodes far
  faster (see the performance tables below), so this is a capability record, not a
  current timing.
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

**Sanity: which model is served?**
```bash
curl -s 127.0.0.1:8080/v1/models | python3 -c 'import sys,json; print([m["id"] for m in json.load(sys.stdin)["data"]])'
# expect: ['qwen3-vl-30b-a3b']
```
(vLLM returns the OpenAI `{"data":[...]}` shape. llama.cpp's old `{"models":[...]}`
with a `capabilities` list is gone — a command reading `d["models"][0]` is pre-Spark.)

**OCR from a public image.** Sent as base64 rather than a URL — originally to work
around llama-server's HTTPS-off build, and still the safer habit on an offline box:
```bash
.venv/bin/python - <<'PY'
import base64, requests, time
IMG = "https://ofasys-multimodal-wlcb-3-toshanghai.oss-cn-shanghai.aliyuncs.com/wpf272043/keepme/image/receipt.png"
img = requests.get(IMG, timeout=30); img.raise_for_status()
uri = f"data:{img.headers['content-type']};base64,{base64.b64encode(img.content).decode()}"
t0 = time.time()
r = requests.post("http://127.0.0.1:8080/v1/chat/completions", json={
    "model": "qwen3-vl-30b-a3b",
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

> **Applicability on the Spark:** the deployed checkpoint is
> `Qwen3-VL-30B-A3B-**Instruct**`, not a `*-Thinking-*` variant, so there is no
> reasoning block to suppress and `/no_think` is a no-op for it. Kept because the
> convention is real, survives a model swap to a Thinking variant, and the
> demo advice below still applies if one is ever served.

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

> **Status: incomplete, rewrite expected.** This is a framework, not a finished
> tool, and it has no concept of thinking/reasoning models. Some docstrings still
> quote pre-Spark ports. Treat output as indicative; requirements for the rewrite
> are in `tests/tracing/README.md`. The `owui_trace_proxy.py` experiment
> (port 8095) is **retired** — it corrupted the OWUI DB and presets during demo
> prep. It is still on disk; do not re-plumb it without a plan.

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

### What "good" looks like — Spark (current) vs AMD iGPU (historical)

**Current: DGX Spark GB10, Qwen3-VL-30B-A3B NVFP4 on vLLM 0.26.0.**

| Metric | Now | Comment |
|--------|-----|---------|
| Decode | **75.6 tok/s** (`flashinfer_b12x`) / **71.1 tok/s** (`FLASHINFER_CUTLASS`, default) | 256 tok, `ignore_eos`, single stream, 3 runs. ~54% of the ~140 tok/s bandwidth ceiling |
| Prefill | **UNMEASURED** | Where FP4 should show its biggest win — the gap most worth closing |
| Vision OCR (dense receipt) | UNMEASURED on Spark | Capability verified on AMD; re-time it here |
| Reranker turn | UNMEASURED on Spark | Was ~1.9 s on the iGPU; same model, now CUDA |
| Tool overhead | UNMEASURED on Spark | Was ~15–25% of a chat turn |
| Cold start (vLLM) | **813 s** | FlashInfer JIT; torch.compile is only 9.5 s of it |
| Warm start (vLLM) | **~3 min** | Reuses `~/.cache/flashinfer` + `~/.cache/vllm` |

Most rows are still `UNMEASURED` because step 07 (agent wiring) is where chat-turn
and tool timings become meaningful. Fill them in from real runs — do not carry the
AMD numbers across.

**Historical baseline: AMD Ryzen MiniPC iGPU, Qwen3-VL-8B-Thinking Q4_K_M / Vulkan.**
Kept deliberately as the before/after reference — this is what the hardware
investment was measured against.

| Metric | AMD iGPU | Comment |
|--------|----------|---------|
| Decode | ~8–9 tok/s | LPDDR5x bandwidth-bound; both 7B and 8B landed here |
| Prefill | ~170 tok/s | The order-of-magnitude gap vs decode is expected |
| Vision OCR (dense receipt) | ~40 s | 300+ output tokens at 8 tok/s |
| Reranker turn | ~1.9 s | After ADR-0008 moved it to GPU/Vulkan (was ~36 s on CPU) |
| Tool overhead | ~15–25 % of chat turn | Higher with the `reboot_guarded` modal, lower for pure RAG |

**Headline so far: decode went ~8.5 → ~71 tok/s, roughly 8×, while the model grew
from 8B dense to 30B-A3B MoE** — i.e. more capable *and* an order of magnitude
faster. Note the comparison is not like-for-like (different model, quant and engine);
it is the honest end-to-end "what the box does for us" delta, which is the number
that justified the hardware.

Older docs citing 300 tok/s prefill / 1000 tok/s decode refer to a much lighter
model than either row above — ignore them.
