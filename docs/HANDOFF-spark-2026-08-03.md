# Handoff — ragfarm, to a cold agent on the DGX Spark
**Written:** 2026-08-03 · **By:** Opus 4.7/5, on the outgoing AMD box · **For:** whoever picks this up on the Spark
**Owner:** David Kubicek (david.kubicek@eywo.cz) — "Dave" throughout.

Read this once, end to end, before touching anything. Then read `CLAUDE.md`
(the contract), `docs/decisions/ADR-0013` (the live engine split), and
`BUILD_STATE.md` (what to execute). This document is the map; those three are the
territory. Where they disagree with this document, **they win** — they are
maintained, this is a snapshot.

---

## 0. Who you are working with

Dave is a 22-year systems-integration consultant (DB2/AIX/clusters heritage),
architect and lead of this project. He is security-fluent and deeply technical —
do not over-explain fundamentals. He wants **reasons, trade-offs, and concrete next
steps**, and he corrects divergence from ground truth quickly. He reads diffs.

Things he has asked for explicitly, which you should treat as standing policy:
- **`.env` at the repo root is the single source of truth** for configuration.
  Not systemd units, not compose, not hardcoded constants. Units should carry
  start/stop/restart *policy* only.
- **Don't scatter config across files.** Editable data belongs in one clearly
  marked structure at the top of a script.
- **Reproducibility is non-negotiable.** `scripts/deploy.sh` must rebuild the
  system from bare metal — which is why he is willing to delete `.venv` and the
  models to prove it.
- He will tell you when he wants options rather than action. When he does, give
  options **with a recommendation**, not a survey.

---

## 1. What ragfarm is

An on-prem RAG + infrastructure-control agent for the ŠA / EPC hosting
environment. A local LLM answers questions grounded in an internal corpus
(spreadsheets of VM inventory and firewall rules, Czech/English prose docs) and
can drive infrastructure operations through safety-gated tools.

The business goal: prove genuine on-prem AI capability convincingly enough to
justify production hardware. **That worked** — the board approved, and the DGX
Spark you are running on is the result. The system is moving from proof-of-concept
to production.

**Architecture in one line:** Open WebUI → mcpo (MCP↔OpenAPI bridge) → MCP
services, with an OpenAI-compatible LLM endpoint underneath and Qdrant + a
hybrid retrieval pipeline behind the `search_corpus` tool.

---

## 2. The single most important thing to know

**GB10 is sm_121, not sm_120, and it has no usable native FP4 compute path.**

`--moe-backend marlin` is **mandatory** when serving an NVFP4 MoE on vLLM here.
Omit it and vLLM starts fine, loads the model fine, and then emits streams of
`!!!!!`. It is a silent numerical failure, not a crash, and it will cost you hours
if you do not know it. **If generation output is garbage, check this first.**

Consequence for expectations: NVFP4's win on this box is **bandwidth, capacity and
numerics-at-equal-footprint** — not tensor-core throughput. The large decode win
comes from the model being **MoE** (≈3B active of 30B), not from FP4. Full
reasoning in ADR-0013.

---

## 3. Hardware and the constraint everything derives from

| property | value |
|---|---|
| chip | GB10 Grace Blackwell Superchip |
| compute capability | **sm_121** (12.1) |
| unified memory | 128 GB (CPU+GPU shared — no separate VRAM budget) |
| memory bandwidth | ~273 GB/s claimed, ~221 GB/s widely reported — **MEASURE IT** |

**Decode is memory-bandwidth-bound.** Choose models by *active* parameters, not
total. This is why the target is a 30B-A3B MoE and never a dense 70B.

**Three GPU consumers share one pipe:** vLLM (LLM), llama.cpp (reranker), BGE-M3
(embedder). Every "let's also run X" proposal must be argued against that budget.
The escape hatch, if isolation ever becomes necessary, is a second Spark over
ConnectX-7 — see `docs/hardware/NVIDIA-Spark.md`.

---

## 4. Service topology — ports are fixed, learn them

| service | endpoint | where it runs | notes |
|---|---|---|---|
| LLM (vLLM) | `127.0.0.1:8080/v1` | host, systemd | OpenAI-compatible. **This contract is the seam** that made the llama.cpp→vLLM swap cheap. |
| Reranker | `127.0.0.1:8081/reranking` | host, systemd | llama.cpp CUDA `--reranking`, `bge-reranker-v2-m3`. Returns a raw logit; the caller sigmoids it. |
| Embedder | `127.0.0.1:8090/embed` | host, systemd | BGE-M3, dense (1024-dim) + sparse in one pass. |
| Qdrant | `127.0.0.1:6333` | container | collection/alias `corpus`. |
| mcpo | `127.0.0.1:8000` | container, host-net | MCP→OpenAPI bridge. Each MCP mounts under `/<name>` (rag → `/rag`). |
| rag-retrieval | `127.0.0.1:8104` | container, host-net | the `search_corpus` MCP. |
| mcp-placement | `127.0.0.1:8101` | container | OpenNebula placement. Currently `ONE_MOCK=1`. |
| mcp-host-control | `127.0.0.1:8102` | container | **safety-gated**. `HOST_MOCK=1`. |
| mcp-fs | `127.0.0.1:8103` | container | sandboxed read-only. |
| Open WebUI | `0.0.0.0:3000` | container, host-net | the **only** LAN-exposed service besides nginx. Auth-gated. |
| drawio-viewer | `0.0.0.0:80` | container | nginx serving a local draw.io mirror + `tests/fixtures/`. |

**systemd units** (`manifests/`): `ragfarm-llama.service` (→ needs replacing with
`ragfarm-vllm.service`), `ragfarm-reranker.service`, `ragfarm-embedder.service`,
`ragfarm-ingester-watcher.service`, `ragfarm-stack.service` (the compose stack).

**Operational entry points:** `scripts/stack.sh {start|stop|restart|status|health}`
brings the whole system up in dependency order and probes every endpoint.
`scripts/deploy.sh` is the reproducible full deploy.

---

## 5. The RAG pipeline — read this before touching retrieval

This is the durable, hardware-agnostic core (ADR-0003). It survived the entire
engine migration untouched, and that is by design. **Do not couple it to the
serving engine.**

### Ingestion (`services/ingester/`)

- **`ingester.py` and `xlsx_tables.py` are FROZEN.** Hand-tuned, regression-locked
  against real corpus fixtures. Treat them as a vendored dependency. If you believe
  the parser is wrong, raise it via `PROGRESS.md` — do not edit inline.
- Regression gate, runs offline with no services:
  `FIXTURES=tests/fixtures python services/ingester/test_xlsx_tables.py` → must
  print `ALL PASS`.
- XLSX becomes **row-per-chunk** (one record per table row), prose becomes
  section-aware semantic chunks (ADR-0007) with ~15% sentence overlap.
- Each chunk stores `text_raw` (verbatim, returned to the model) and `text_clean`
  (embedding-only). **Never return `text_clean` to the model.**
- Corpus is **content-addressed through a manifest** (ADR-0006). Retrieval targets
  the collection **alias**, so a rebuild can swap atomically. Anything new that
  persists must ride the manifest.

### Retrieval (`services/rag-retrieval/server.py`)

```
query
  → embed dense+sparse                          :8090/embed  (kind=query)
  → Qdrant hybrid Query API, two prefetch branches            :6333
        dense (semantic) + sparse (exact token) → RRF fusion
  → cross-encoder rerank of the fused pool                    :8081/reranking
        raw logit → sigmoid → [0,1]
  → ADR-0010 §1 GATE: absolute floor, then Kneedle hatch
  → top-k
  → same-section window expansion
  → verbatim text_raw + provenance → model
```

**Knobs** (all env, all documented in `.env.example`):

| var | default | meaning |
|---|---|---|
| `RAG_PREFETCH` | 40 | candidates per branch before fusion |
| `RAG_CANDIDATES` | 40 | fused pool size handed to the reranker |
| `RAG_USE_RERANKER` | 1 | 0 = legacy MMR path (A/B only) |
| `RAG_MIN_SCORE` | **0.0 — UNCALIBRATED** | absolute reranker-score floor |
| `RAG_GATE_KNEEDLE` | 1 | Kneedle adaptive hatch |
| `RAG_GATE_MIN_SET` | 12 | arm Kneedle only above this many survivors |
| `RAG_GATE_WEAK_KNEE` | 0.05 | below this normalized chord distance, no knee |
| `RAG_EXPAND_NEIGHBORS` | 1 | same-section neighbours folded in per side |
| `RAG_EXPAND_MAX_WORDS` | 600 | word cap on an expanded window |

**MMR is retired** (ADR-0008). Do not propose re-adding it. The learned failure:
MMR reads N near-identical-but-distinct records (contact rows, forward/reverse
firewall rules) as "redundant" and evicts the actual answers. The cross-encoder
scores each on its own merit. This lesson is expensive; do not re-learn it.

---

## 6. ADR status — what is real, what is aspiration

Read the status line, not the filename. Several are misleading.

| ADR | Status | What you need to know |
|---|---|---|
| 0001 engine split | **SUPERSEDED by 0013** | AMD iGPU/NPU. Historical only. Do not act on it. |
| 0002 "quark-quantization" | Accepted, amended by 0013 | **The filename lies.** This ADR *rejects* Quark/NPU and puts BGE-M3 on CPU. 0013 moves it CPU→CUDA. |
| 0003 agent layer | ACCEPTED | OWUI+mcpo. Retrieval is Option B — the UI's own doc-RAG is **not** used. |
| 0004 actuation | ACCEPTED | typed params, server builds argv, **no shell**, least privilege, audit. |
| 0005 conventions | ACCEPTED | naming/layout. |
| 0006 manifest | ACCEPTED | content-addressed corpus + alias switch. |
| 0007 chunking | ACCEPTED | section-aware, broad-in/narrow-out. Its §2 MMR is superseded. |
| 0008 cross-encoder | **PENDING** | The *quality* decision is validated and live. Still PENDING purely because `RAG_MIN_SCORE` is uncalibrated. |
| **0009 vision** | **ACCEPTED — last fully implemented ADR** | Amended by 0013: mechanism moves from llama.cpp `--mmproj` to vLLM-native. |
| 0010 dual-branch | ACCEPTED (architecture) | **§1 gate code shipped 2026-07-31, unverified by Dave. Floor uncalibrated. §2 LightRAG NOT BUILT.** |
| 0011 workbooks | PROPOSED | nothing built. |
| 0012 multimodal | PROPOSED | nothing built. |
| **0013 Spark** | **ACCEPTED** | The live engine split. Start here. |

---

## 7. Outstanding debts, in priority order

### 7.1 ADR-0010 §1 floor calibration — **the highest-leverage open item**
The gate mechanism is in (`_gate()` + `_kneedle_cut()` in
`services/rag-retrieval/server.py`), unit-tested, and live. The **number** is not.
`RAG_MIN_SCORE=0.0` means the floor is off and only Kneedle is cutting.

Method (already tooled):
```bash
.venv/bin/python scripts/rag_pool_inspect.py --dump-scored calib.csv --pool 40 \
  "hsmbvxip001ts" "Jak se přihlásím do EPC?" "<FW-rule FQDN that triggers reverse rules>"
```
Label each row required/junk in the `label` column. Set `RAG_MIN_SCORE` to the
highest value that keeps **every** required row — including the low-scoring
reverse-direction firewall rules, which is the whole subtlety — while cutting junk.
ADR-0010 predicts **~0.35–0.45**, lower than intuition because reverse rules are
textually near-identical to their forward twins and the cross-encoder penalises
them. Closing this promotes **both** ADR-0008 and ADR-0010 §1.

This directly attacks the recurring **context-blowup** problem and it is unblocked.

### 7.2 Work from Dave's 2026-08-03 list

**DONE before the handover** (on the outgoing box, already on `main`):
- **`setup_openwebui.py` → alias-keyed `MODEL_TUNING`.** All per-model knobs in one
  nested structure at the top of the file, keyed by served alias. Verified
  behaviour-preserving and idempotent. Adding the Spark model = add one entry
  keyed `qwen3-vl-30b-a3b` — **already seeded**, it activates when vLLM serves that
  alias. Watch for the printed NOTE when two aliases share a `preset_id`.
- **Tracing endpoints via `.env`.** `tests/tracing/ragfarm_env.py` resolves every
  endpoint (shell > `.env` > real-port defaults). All 8 tools import cleanly.
  `python-dotenv==1.2.2` is in all three locks.
- **`cu12` → `cu13` profile** + `sympy` pin corrected (torch 2.13.0 needs
  `>=1.13.3`; two of three locks had 1.13.1 and would fail to resolve).
- **ADR-0013, CLAUDE.md Ch1/Ch2, BUILD_STATE steps 01–03/07** — all retargeted.

**STILL OPEN:**
- **`.env` inside containers.** Answered but not implemented: `load_dotenv()` is
  **verified working inside our containers**; what is missing is the *file* —
  compose's `.env` handling only does `${VAR}` substitution in the compose file and
  does not place it in the container. Recipe: bind-mount `../.env:/app/.env:ro` and
  `load_dotenv("/app/.env")` in the service. Not done here because it could not be
  validated before the move. **Once it works, strip the redundant `Environment=` /
  `EnvironmentFile=` from the units** — Dave wants units to carry policy, not config.
- **Tracing rewrite.** Ports are fixed but the framework still has no concept of
  thinking models. Requirements for the rewrite are in `tests/tracing/README.md`;
  docstrings there still quote the old wrong ports and say so explicitly.
- **MODEL.md auto-benchmark.** On model activation, run a canned benchmark and
  upsert prefill/decode tok/s into the model's parent-dir `MODEL.md`. This is the
  instrument that settles NVFP4-vs-alternatives empirically — see §7.3. **Not
  started.**
- **`docs/rag-pipeline.md`** — a single authoritative writeup. §5 here is the seed.
  **Not started.**
- **Docs and diagrams still describe the AMD box:** `README.md`,
  `docs/deployment.md`, `docs/prompts.md`, `docs/pdf/*.md`, and the four matplotlib
  architecture diagrams in `assets/ragfarm_*.png`. `docs/ryzenai/` should be
  retired or clearly marked historical. **Not started.**
- **`scripts/` model lifecycle is still GGUF-shaped.** `fetch-llm.sh`,
  `activate-llm.sh`, `llama-launch.sh`, `lib-models.sh` and
  `manifests/ragfarm-llama.service` all assume GGUF + `--mmproj`. They need a
  safetensors/vLLM equivalent (`ragfarm-vllm.service`). **This is real work and it
  is on the critical path for step 02.**

### 7.3 Numbers that are inferred, not measured
Everything performance-related in ADR-0013 is tagged **MEASURE**. Specifically:
real memory bandwidth (221 vs 273 GB/s), decode tok/s, prefill tok/s, whether the
community NVFP4 VL checkpoint works on sm_121 at all, and whether FP8 actually
loses to NVFP4 here given MoE runs Marlin W4A16 either way.

### 7.4 Tracing framework
`tests/tracing/` is a partial framework, not a working tool. Dave's assessment:
*"framework nekompletní, is just a framework. Taky vůbec nebere v potaz thinking
modely"* — it has no concept of thinking/reasoning models. A rewrite is expected.
A trace proxy (`scripts/owui_trace_proxy.py`, port 8095) was built and then
**retired** — it corrupted the OWUI DB and model presets during demo prep. It is
still on disk. **Do not re-plumb it without a plan.**

### 7.5 OpenNebula
Steps 05/06 are `BLOCKED` because the PoC box had no cluster access. The Spark is
production, so creds may now be obtainable — **ask Dave**. Never mock `where_is_vm`
to force a gate.

---

## 8. Hard rules — violate these and you will break something Dave cares about

- **Never edit `CLAUDE.md`, the ADRs, or `docs/decisions/*` during build execution.**
  Raise disagreement through `PROGRESS.md`.
- **Frozen files:** `services/ingester/ingester.py`, `services/ingester/xlsx_tables.py`,
  the MCP services, the manifests. Read-only inputs.
- **Never `git push --force`. Never commit `logs/` or `.env`.** All work lands on
  `main`; `git pull --rebase` before every push.
- **Never resolve a git conflict yourself** — abort, `BLOCKED:` it, wait for Dave.
- **`/tmp/ragfarm.lock`** — take the heartbeat on session start, refresh per step,
  `echo IDLE` on clean exit. If it holds a timestamp under 300s old, **another
  agent is live: stop immediately.**
- **Run as `dave`, never root.** `sudo` only for the specific privileged action a
  step needs (package install, systemctl). Never `sudo` a whole script.
- **Do not spawn subagents for build steps.** The build is strictly sequential and
  dependency-gated; parallel workers become concurrent committers the lock cannot see.
- **Ask before:** anything running as root, anything touching prod VMs, any
  `--recreate` on a live corpus.
- **Every `DONE` step writes its guarded deploy fragment** into `scripts/deploy.sh`.
  See CLAUDE.md → "The deploy.sh fragment contract". A `DONE` step without one is
  incomplete.

---

## 9. Prompt/preset design — hard-won, don't casually revert

The Open WebUI presets carry a system prompt that went through many painful
iterations against an 8B thinking model. What survived:

- **`RULE 0` decides tool-vs-no-tool first**, and takes precedence over the
  tools-first rule. Without it the model hallucinated image-analysis tools.
- **Enumerate the tools that exist and say anything else is a hallucination.**
  The model invented `describe_image`, `analyze_image`, `ocr_tool`.
- **"Call each tool AT MOST ONCE per turn."** Otherwise it re-queried endlessly.
- **`file_context: False` on the vision preset.** OWUI's file_context middleware
  prepends an `<attached_files>` XML blob *as text* alongside the real vision
  tokens, and the model then reasons about "a text description of an image"
  instead of looking at it. This one cost a lot of debugging.
- **Long prompts breed loops on small models.** An earlier attempt to handle every
  OCR edge case in `RULE 4` *caused* a "Wait, but…" reasoning loop. Cutting it back
  fixed it. Resist the urge to over-specify.
- **Small models ignore negative-frame lists** and will emit the exact banned
  string. Prefer positive instruction.
- `max_tokens` must be set explicitly or OWUI's frontend default truncates long
  tabular answers mid-value.

A 30B MoE may need less of this scaffolding than an 8B did — **re-test rather than
assuming either way**.

---

## 10. Where things live

```
CLAUDE.md                     the contract — read every session
BUILD_STATE.md                linear build progress + step definitions + gates
PROGRESS.md                   the BLOCKED:/UNBLOCKED: channel to Dave
scripts/deploy.sh             the accreting reproducible deploy (fragment per step)
scripts/stack.sh              start/stop/restart/health the whole system
scripts/rag_pool_inspect.py   candidate-pool inspection + --dump-scored calibration
services/rag-retrieval/       search_corpus: hybrid RRF + rerank + ADR-0010 gate
services/ingester/            FROZEN parser + manifest-driven ingest
services/mcp-*/               placement, host-control (safety-gated), fs
infra/compose.yaml            the container stack
infra/openwebui/              setup_openwebui.py (presets, idempotent), check_toolchain.py
manifests/                    systemd units
docs/decisions/               ADRs — 0013 is the live engine split
docs/hardware/NVIDIA-Spark.md the hardware record
tests/fixtures/               committed demo/regression fixtures, served at /fixtures/
models/{llm,embeddings,reranker}/  each with a MODEL.md recording model+revision
logs/<NN-stepname>.log        raw build output — gitignored, never inlined anywhere
```

---

## 11. Suggested order of work

1. **Execute `BUILD_STATE.md` steps 01→02** — venv on CUDA 13, then vLLM serving.
   Nothing else can be judged until the LLM answers. Write the deploy fragments.
2. **Steps 03→04** — embedder on CUDA (mind the vector-compatibility gate), then
   Qdrant + ingest through the frozen parser.
3. **Step 07** — OWUI + mcpo + rag-retrieval; prove the RAG-only gate.
4. **Calibrate `RAG_MIN_SCORE`** (§7.1). Highest leverage, fully unblocked.
5. **Then the §7.2 backlog** — alias-keyed presets, `.env` SSoT, tracing rewrite,
   MODEL.md benchmarks, docs and diagrams.
6. **ADR-0012 Phase 1** (Docling + scanned-PDF OCR) closes a real gap: scanned PDFs
   are currently **skipped** entirely by the ingester.
7. **ADR-0011 Phase 1** — author real workbooks, then the execution loop. Note it
   begins with a *deliberately unsafe* lab bootstrap; confirm the lab boundary with
   Dave explicitly, and never let it touch prod.

Do not run ahead of the build order. Steps are dependency-gated for real reasons.
