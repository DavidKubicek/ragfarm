# BUILD_STATE — single source of truth for build progress

Updated by the agent per build protocol in `CLAUDE.md` (Chapter 2).
Step statuses: `PENDING` | `DONE` | `FAILED` | `BLOCKED` | `SKIP`
`SKIP` disables step processing: no agent will parse, run, eval any of the step's parts or gate-checks.
Set to `PENDING` and it becomes a normal planned part of the sequence again.
Raw stdout+stderr per whole step gets appended to `logs/<NN-stepname>.log`.
Timestamps are in UTC. Keep summaries <120 chars; sum-up the resulting success or reason for failure.
Reference the log file, do not parse it or reproduce it here.

## Status table

| NN | step                | status  | updated_utc       | log                            | summary |
|----|---------------------|---------|-------------------|--------------------------------|---------|
| 01 | venv-cuda13         | PENDING | 2026-08-03T00:00Z | logs/01-venv-cuda13.log        | build .venv against CUDA 13 (--profile cu13); everything downstream imports from it |
| 02 | vllm-serving        | PENDING | 2026-08-03T00:00Z | logs/02-vllm-serving.log        | vLLM v0.22.x serving NVFP4 Qwen3-VL MoE on :8080, --moe-backend flashinfer (b12x native FP4) |
| 03 | embedder-service    | PENDING | 2026-08-03T00:00Z | logs/03-embedder-service.log   | BGE-M3 on CUDA behind :8090/embed, 1024-dim dense + sparse, EN+CS |
| 04 | qdrant-ingester     | PENDING | 2026-08-03T00:00Z | logs/04-qdrant-ingester.log    | Qdrant up + corpus ingested through the FROZEN parser, dense+sparse schema |
| 05 | mcp-placement       | BLOCKED | 2026-06-18T11:30Z | logs/05-mcp-placement.log      | no OpenNebula access in PoC; ON creds+reachability deferred to deployment (ADR-0003) |
| 06 | mcp-fs-host-control | BLOCKED | 2026-06-18T11:30Z | logs/06-mcp-fs-host-control.log| host-control needs live ON (drain-reboot); deferred to deployment (ADR-0003) |
| 07 | agent-wiring        | PENDING | 2026-08-03T00:00Z | logs/07-agent-wiring.log       | Open WebUI + mcpo + rag-retrieval over the OpenAI-compatible endpoint |

> **Steps 05/06 are still `BLOCKED` on OpenNebula.** They were blocked because the
> PoC box had no cluster access. The Spark is the *production* target, so ON creds
> may now be obtainable — **ask Dave** before assuming either way. Do not clear a
> `BLOCKED` yourself and do not mock `where_is_vm` to force the gate.

> **Hardware note.** This build targets an **NVIDIA DGX Spark (GB10, sm_121)** per
> ADR-0013. Any step text below that still mentions the AMD box (NPU, iGPU, Vulkan,
> Quark, ROCm, GGUF/Q4_K_M) is historical and is marked as such. ADR-0001 is
> SUPERSEDED — do not act on it.

---

## Step definitions

Each step below carries its commands and its **Gate**. Execute a step only when
it is the first eligible step per the protocol. Run the commands, tee output to
the step's log, then evaluate the Gate to decide `DONE` vs `FAILED`.

### 01 — venv-cuda13
Build the project Python environment against **CUDA 13**. This is first because
every other step imports from it: the embedder (03), the ingester (04), the MCP
services (05–07) and every tracing tool all run out of `.venv`.

> Replaces the former `01 — npu-bringup`. The AMD RyzenAI NPU runtime is not part
> of this topology (ADR-0013 supersedes ADR-0001). `infra/npu/` and
> `docs/ryzenai/` are historical.

**Preconditions (host, Dave-supplied). All four are `BLOCKED`, not `FAILED`:**
- `cuda-libraries-13-0` installed — verify with `nvcc --version` or
  `ls /usr/local/cuda-13.0`.
- `python3.12` available (the locks are frozen against 3.12; a different minor
  will resolve different wheels and silently diverge from the lock).
- Network egress to `pypi.org` and `download.pytorch.org` (via
  `scripts/proxy-env.sh` if a proxy is configured).
- **`dave` is in the `docker` group.** `manifests/ragfarm-stack.service` runs
  `docker compose` as `User=dave`, so without it the whole container plane fails
  at step 04/07 with a permission error. Verify: `id -nG dave | grep -q docker`,
  or functionally `docker info >/dev/null`. This is a **privilege decision only
  Dave makes** — docker group membership is effectively root-equivalent, so do
  not add yourself to it and do not paper over it with `sudo docker`.

**`.env` bootstrap — do this FIRST, before anything else in this step.**
A clean checkout has **no `.env`** (it is gitignored). Nothing breaks loudly
without it — every URL has a real default in `ragfarm_env.py` — but two things go
wrong quietly, so create it up front rather than discovering this at step 03:
- The units no longer carry `Environment=` fallbacks (stripped 2026-08-03, config
  belongs in `.env`), so `ragfarm-embedder` and `ragfarm-reranker` have **no model
  path at all** until `scripts/fetch-encoder.sh` writes one.
- `CORPUS_PATH` is the one genuinely site-specific value and **no step sets it**.

```bash
cd ~dave/ragfarm
[ -f .env ] || cp .env.example .env      # every line is commented; defaults apply
grep -q '^CORPUS_PATH=' .env || echo 'CORPUS_PATH=/data/corpus' >> .env
./ragfarm_env.py                          # confirm what actually resolved
```
Confirm `corpus` points where Dave actually put it — **ask him if unsure**, do not
guess. `scripts/fetch-encoder.sh` (step 03) appends `EMBED_MODEL_PATH` and
`RERANK_GGUF_PATH` itself via `env_upsert`, which creates the file if absent, so
the model paths are self-healing from there.

> Naming note for the scripts rework: `fetch-encoder.sh` writes the **legacy**
> `RERANK_GGUF_PATH`, and `ragfarm-reranker.service` reads that same legacy name —
> internally consistent, so **do not rename one without the other**. Both should
> move to `RERANK_MODEL_PATH` together as part of the vLLM lifecycle rework.

**Why a full rebuild here.** This step deliberately **deletes** any existing
`.venv`. It is not merely hygiene: the old box was **x86_64** and the Spark is
**aarch64** (GB10's Grace half is ARM). Every compiled wheel in a carried-over
`.venv` is the wrong machine code and *cannot* run here. Same for any
`~/llama.cpp/build` rsync'd across — it must be rebuilt from source for aarch64.
The *deploy fragment* this step writes is guarded, so a later code-release run of
`deploy.sh` will **not** rebuild the venv — only `--fresh` will.

> **ARCHITECTURE WARNING — read before judging a lock failure.**
> `services/requirements.cu13.lock` was frozen on an **x86_64** box. Some pins may
> have no `linux_aarch64` wheel at that exact version, in which case pip either
> builds from source (slow but fine) or fails outright. **That is not necessarily
> your mistake, and it is not automatically a `FAILED` step.**
> - If pip resolves everything: proceed, and the re-freeze in command 5 captures
>   the real aarch64 graph.
> - If a *specific* package has no aarch64 wheel at the pinned version: record it,
>   relax **that one pin** (nearest version with an aarch64 wheel), note the change
>   in the step summary, and let the re-freeze commit the resolved set. Do **not**
>   silently drop the lock and `pip install` loose — that discards reproducibility,
>   which is the whole point of this step.
> - If torch itself has no cu130 aarch64 wheel: **STOP, that is `BLOCKED`** — it
>   changes the CUDA/torch pairing and is Dave's call, not yours.
>
> Consequence: on the *first* aarch64 build, the gate's "LOCK MATCH" check may
> legitimately report mismatches. Judge it on the list of what mismatched, and
> expect the committed lock to change more than a re-freeze would normally imply.

**Commands:**
```bash
cd ~dave/ragfarm
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); pip needs egress

# 0. record what we are building against (goes in the log, informs the lock re-freeze)
uname -m                      # EXPECT aarch64. If x86_64 you are on the wrong box.
nvidia-smi
nvcc --version || ls -d /usr/local/cuda-13.0
python3.12 --version

# 1. fresh venv — the old one is not migrated, it is replaced
rm -rf .venv
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel

# 2. install the CUDA 13 profile from the committed lock.
#    requirements.cu13.lock + the cu130 torch wheel index; deploy.sh --profile cu13
#    selects exactly this pair (scripts/deploy.sh profile_config).
.venv/bin/pip install \
    --extra-index-url https://download.pytorch.org/whl/cu130 \
    -r services/requirements.cu13.lock

# 3. VERIFY EVERY DEPENDENCY MATCHES THE LOCK (not just "it installed")
.venv/bin/pip check
.venv/bin/python - <<'PY'
import re, sys, importlib.metadata as md
bad = []
for line in open("services/requirements.cu13.lock"):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name, _, want = line.partition("==")
    if not want:
        continue
    try:
        got = md.version(name)
    except md.PackageNotFoundError:
        bad.append(f"MISSING  {name} (want {want})"); continue
    # torch carries a local version tag on the CUDA wheels (e.g. 2.13.0+cu130)
    if got.split("+")[0] != want.split("+")[0]:
        bad.append(f"MISMATCH {name}: want {want}, got {got}")
print("\n".join(bad) if bad else "LOCK MATCH: all pinned versions present and equal")
sys.exit(1 if bad else 0)
PY

# 4. verify torch actually sees the GPU, and that it is sm_121
.venv/bin/python - <<'PY'
import torch
print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
print("cuda runtime:", torch.version.cuda)
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0))
    print("capability:", torch.cuda.get_device_capability(0))
    x = torch.randn(1024, 1024, device="cuda")
    print("matmul ok:", bool(torch.isfinite(x @ x).all()))
PY

# 5. BUILD llama.cpp WITH CUDA. Still required on the Spark even though vLLM
#    replaced it for GENERATION: ADR-0013 §3 keeps the cross-encoder reranker on
#    a llama.cpp `--reranking` server (:8081), and three things hard-depend on it:
#      - manifests/ragfarm-reranker.service execs $LLAMA_DIR/build/bin/llama-server
#      - scripts/fetch-encoder.sh:105 needs convert_hf_to_gguf.py for the rerank GGUF
#      - scripts/deploy.sh phase_preflight (:148) DIES without the binary
#    Build CUDA, NOT Vulkan — infra/llama/README.md is the AMD/iGPU-era doc.
git -C ~ clone https://github.com/ggml-org/llama.cpp.git 2>/dev/null || true
cd ~/llama.cpp && git pull
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121
cmake --build build --config Release -j"$(nproc)"
ls -l ~/llama.cpp/build/bin/llama-server ~/llama.cpp/convert_hf_to_gguf.py
cd ~dave/ragfarm

# 6. re-freeze the lock WITH the resolved nvidia-* runtime deps and commit it.
#    The committed cu13 lock was frozen from a CPU venv, so its CUDA runtime deps
#    are unpinned — until this runs, the lock is not actually reproducible.
.venv/bin/pip freeze > /tmp/cu13.freeze
head -8 services/requirements.cu13.lock > /tmp/cu13.new     # keep the header comment
grep -v '^#' /tmp/cu13.freeze >> /tmp/cu13.new
mv /tmp/cu13.new services/requirements.cu13.lock
git diff --stat services/requirements.cu13.lock
```

**Gate:** all five must hold —
1. `.venv/bin/pip check` exits 0 (no broken/conflicting requirements);
2. the lock-match script prints `LOCK MATCH` and exits 0;
3. `torch.cuda.is_available()` is `True`, `get_device_capability(0)` returns
   `(12, 1)`, and the CUDA matmul returns finite values;
4. `~/llama.cpp/build/bin/llama-server` exists and is executable, AND
   `~/llama.cpp/convert_hf_to_gguf.py` exists. Sanity-check the backend actually
   compiled in: `~/llama.cpp/build/bin/llama-server --list-devices` should report a
   CUDA device, not "no devices". A Vulkan-only or CPU-only build passes the
   file-exists test and then serves the reranker at CPU speed — the exact
   regression ADR-0008 spent a day fixing;
5. `services/requirements.cu13.lock` now pins the `nvidia-*` runtime packages
   (i.e. the re-freeze actually changed the file) and is committed.

`uname -m` must be `aarch64`. If CUDA 13 or python3.12 is missing → `BLOCKED`.
If torch has no cu130 aarch64 wheel → `BLOCKED` (Dave's call). If a non-torch pin
lacks an aarch64 wheel → relax that single pin per the architecture warning above
and record it; that is a `DONE` with a noted deviation, not a `FAILED`. Any other
unresolvable conflict → `FAILED`; report the conflicting pair and WAIT.

**Deploy fragment (write on DONE, per CLAUDE.md):** marker
`deploy-step-01-venv-cuda13`. Guard on the environment being real, not merely
present:
```bash
if .venv/bin/python -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null \
   && [ "${FORCE_ALL:-0}" != 1 ]; then
```
Body = the venv creation + `pip install` from the lock (commands 1–2 above).
Verification commands 3–4 stay in the log, not the fragment.

---

### 02 — vllm-serving
Serve the generative LLM with **vLLM** on the OpenAI-compatible endpoint
`127.0.0.1:8080/v1`; the agent layer (step 07) drives it. Per ADR-0013.

> Replaces the former `02 — igpu-llm` (llama.cpp + Vulkan + GGUF Q4_K_M). That was
> the AMD box. The *endpoint contract* is deliberately identical, which is why
> nothing above the serving layer changes.

**READ THIS BEFORE ANYTHING ELSE — the sm_121 backend choice.** GB10 is compute
capability **12.1**, not 12.0, and a misconfigured NVFP4 MoE here fails
**silently**: vLLM starts cleanly, loads the model, then generates streams of
`!!!!!`. **If output is garbage, it is a backend/kernel problem first.**

Two backends, and the safe-looking one is slow:
- **`--moe-backend flashinfer`** (b12x) — **the target**. Native tensor-core FP4 on
  sm_121 (~356 TFLOPS measured) via vLLM PR #40082 (2026-05-20) + CUTLASS #3096's
  `compute_120f` fix, which requires **CUDA 13.0** (step 01 gives us that).
- **`--moe-backend marlin`** — **fallback only**. Dequantizes FP4→BF16, forfeits the
  FP4 speedup. Use if b12x fails on our checkpoint, and SAY SO in the summary.
  Do not enable MTP on this path (measured -22%).

Older sources (and an earlier revision of ADR-0013) call marlin *mandatory* because
GB10 "has no native FP4" — **stale**, pre-May-2026. See ADR-0013's correction note.

**Preconditions:** step 01 `DONE` (it supplies CUDA 13.0). Use a **current stable
vLLM (v0.22.x)** — the "≥0.19.0" floor predates #40082, and community model cards
citing v0.13.0 predate all of it.

**Model selection (ADR-0013 §2), in preference order.** Try (a); fall back to (b)
if it will not load or produces garbage *with* marlin set:
- (a) `ig1/Qwen3-VL-30B-A3B-Instruct-NVFP4` — community NVFP4 + vision, the target.
- (b) `Qwen/Qwen3-VL-30B-A3B-Instruct-FP8` — official Qwen FP8, fewer unknowns,
  larger footprint.
Record which one you actually served, and why, in the step summary and in
`models/llm/<slug>/MODEL.md`.

**Commands:**
```bash
cd ~dave/ragfarm
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); pip + HF fetch need egress

# 1. vLLM into the step-01 venv. Current stable (needs PR #40082, 2026-05-20);
#    record the resolved version — it decides whether b12x FP4 is available.
.venv/bin/pip install -U 'vllm>=0.22.0'
.venv/bin/python -c "import vllm; print('vllm', vllm.__version__)"

# 2. fetch the NVFP4 checkpoint (safetensors; latest revision, fastest format)
.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
p = snapshot_download("ig1/Qwen3-VL-30B-A3B-Instruct-NVFP4")
print("snapshot:", p)
PY

# 3. serve. Try flashinfer (native FP4) FIRST; fall back to marlin only if it
#    fails, and record which one you ended up on.
#    --max-model-len is capped deliberately: the 30B-A3B does not fit at full
#    context alongside the reranker and embedder on shared unified memory.
.venv/bin/vllm serve <resolved-snapshot-path-or-repo-id> \
  --served-model-name qwen3-vl-30b-a3b \
  --host 127.0.0.1 --port 8080 \
  --moe-backend flashinfer \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 32768
# If that produces `!!!!!` output or fails to init the grouped GEMM, retry with
#   --moe-backend marlin
# and note the downgrade (it costs the FP4 speedup) in the step summary.

# 4. probe: endpoint, then a real generation, then TOOL CALLING specifically
curl -s 127.0.0.1:8080/v1/models

curl -s 127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"qwen3-vl-30b-a3b",
  "messages":[{"role":"user","content":"Reply with exactly: pong"}]}'

curl -s 127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"qwen3-vl-30b-a3b",
  "messages":[{"role":"user","content":"What is the weather in Brno?"}],
  "tools":[{"type":"function","function":{"name":"get_weather",
    "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'
```

**Gate:** all four must hold —
1. `/v1/models` returns the served model name;
2. a chat completion returns a non-empty assistant message that is **coherent
   text** — explicitly NOT a run of `!` characters (the marlin failure mode);
3. the tool-calling probe returns a `tool_calls` entry naming `get_weather` with a
   parsed `city` argument (this is what ADR-0003's agent layer depends on, and it
   is the functional replacement for llama.cpp's `--jinja`);
4. the resolved vLLM version and the served checkpoint are recorded in the step
   summary.

Garbage output → re-check `--moe-backend marlin` before anything else, then
report `FAILED` with the vLLM version and full launch line. OOM at load → lower
`--max-model-len` and record the value that fit. A missing/ungated HF checkpoint
→ `BLOCKED`.

**Deploy fragment (write on DONE):** marker `deploy-step-02-vllm-serving`. Guard
on the endpoint answering, not on a file existing:
```bash
if curl -sf --max-time 5 http://127.0.0.1:8080/v1/models >/dev/null \
   && [ "${FORCE_ALL:-0}" != 1 ]; then
```
Body = the `pip install vllm`, the snapshot fetch, and the systemd unit
install/enable that ends up serving it (`manifests/ragfarm-vllm.service` — the
GGUF-shaped `ragfarm-llama.service` is retired with llama.cpp for generation).
Record the exact, working `vllm serve` argument list in that unit, marlin flag
included.

---

### 03 — embedder-service
> **SUPERSEDED (2026-07-21):** the manual `snapshot_download` below (pinned revision +
> `ignore_patterns=["pytorch_model.bin"]`) is superseded by `scripts/fetch-encoder.sh`,
> which fetches the **latest** revision and auto-selects the fastest weight format. The
> old **safetensors-only / no-pickle** constraint was a workaround for a pickle-RCE in the
> very old torch of the abandoned NPU venv — **retired** now that we run torch 2.13+
> (`weights_only` default). The pinned rev remains the known-good baseline: see
> `docs/deployment.md` → "Tested model versions". The commands below are kept as the
> historical record of how step 03 was originally done.

Serve a multilingual embedder behind HTTP `/embed` on `:8090`. Ingestion (step 04)
and retrieval (step 07) call it.

**Runs on CUDA** (ADR-0013 §4 — amends ADR-0002, which chose the BGE-M3 *model* and
correctly rejected the NPU/Quark path; only the host changes, CPU → CUDA). It is
prefill-shaped and gains the most from the GPU. Its contention with vLLM is
tolerable because the load is asymmetric: bulk ingest is a batch job, while a query
embeds one short string.

> **Vector-compatibility gate — do not skip.** If a populated `corpus` collection
> already exists, its vectors were produced by the **CPU** build. A CUDA-vs-CPU
> numerical drift would silently degrade retrieval without any error. Either prove
> the outputs match to tolerance, or plan a full `--recreate` re-ingest in step 04.
> On a genuinely fresh Spark with no prior collection this is moot — but check,
> do not assume.

> The AMD-era cleanup below (deleting `bge-small-en-v1.5-onnx-static/`) is
> historical; on a fresh box there is nothing to delete.

Model: **BAAI/BGE-M3** download specific snapshot ID 50f9396f75618b3389c1fd1068a1ff58dc7b5b26 
(568M, 100+ languages incl. Czech, up to 8192 tokens).
Use environment variable EMBED_MODEL_PATH to actually load the model from that path,
not by name (variable set in systemd service unit).
Emits dense AND sparse vectors from one pass — both are served, so step 04 stores
named vectors for hybrid retrieval (dense for semantics, sparse for exact
host/IP/VLAN token matches). Record model + resolved revision in
`models/embeddings/MODEL.md`.

**Service contract (`services/embedder/server.py`):**
- `POST /embed` body `{"input": ["text", ...], "kind": "passage"|"query"}` →
  `{"dense": [[...1024...]], "sparse": [{"<token_id>": weight, ...}], "dim": 1024}`
- `kind` defaults to `passage`; retrieval (step 07) passes `query`.
- CUDA inference via FlagEmbedding (`BGEM3FlagModel`, dense+sparse in one call);
  `use_fp16=True` is the normal CUDA setting. Confirm the model actually lands on
  the GPU rather than silently falling back to CPU — a silent CPU fallback here
  looks like "it works, just slowly" and will be blamed on the wrong thing later.

**Commands:**
```bash
cd ~dave/ragfarm
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); HF snapshot_download needs egress

# NOTE: the AMD-era cleanup that used to live here (a blanket
#   find models/embeddings/ -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
# ) has been REMOVED. It deleted every model directory, including BGE-M3 itself on
# any re-run, which is unacceptable in an idempotent step. On a fresh Spark there
# are no NPU-era artifacts to clean. If you are migrating a box that has one,
# delete that ONE path explicitly and log it.

# Deps come from the step-01 venv (already pinned by requirements.cu13.lock).
# Do NOT `pip install -U` here — that silently drifts from the lock.
.venv/bin/python -c "import FlagEmbedding, fastapi, uvicorn; print('embedder deps ok')"

# Fetch BGE-M3. Per the standing model policy: LATEST revision, fastest weight
# format, exclude only unused/redundant files. Record the revision the gate needs.
.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
p = snapshot_download("BAAI/bge-m3")
print("snapshot:", p)
PY
# (Known-good baseline revision, for comparison if the latest misbehaves:
#  50f9396f75618b3389c1fd1068a1ff58dc7b5b26 — see docs/deployment.md
#  "Tested model versions".)

# Launch the embedder via manifests/ragfarm-embedder.service as a new system unit.
# Probe dense+sparse with one numeric/English table row and one Czech sentence:
curl -s 127.0.0.1:8090/embed \
  -H 'Content-Type: application/json' \
  -d '{"input":["prod-kvm-03 10.20.1.43 VLAN203","jak zalohovat hostitele"],"kind":"passage"}'
```

**Gate:** the probe returns a `dense` array of 1024-dim vectors (one per input,
L2-norm ≈ 1.0) AND a non-empty `sparse` map per input, AND
`models/embeddings/MODEL.md` records `BAAI/bge-m3` with the resolved revision
hash, AND the service log shows the model on **CUDA** (not a CPU fallback). Both
probe inputs — the English/numeric table row and the Czech sentence — must return
finite vectors; this is the explicit multilingual check the prior bge-small build
would have failed.

**Deploy fragment (write on DONE):** marker `deploy-step-03-embedder-service`.
Guard: `curl -sf --max-time 5 http://127.0.0.1:8090/health`. Body = the model
snapshot fetch + the `ragfarm-embedder.service` install/enable.

---

### 04 — qdrant-ingester
Bring up Qdrant and ingest the corpus; retrieval depends on a populated
collection. Depends on step 03 (`/embed`) being up.

**The ingester is COMMITTED and FROZEN — do not regenerate, rewrite, or "improve"
it.** `services/ingester/ingester.py` (routing/embed/Qdrant) and
`services/ingester/xlsx_tables.py` (all XLSX structural parsing) are hand-tuned and
regression-locked against real corpus fixtures. The earlier plan to have a build
step generate the chunker is SUPERSEDED. Treat these two files as read-only inputs
to this step, exactly like a vendored dependency. If you believe the parser is
wrong, raise it via the PROGRESS.md blocker channel — do NOT edit it inline.

`docs/ingestion-pipeline.md` remains the design reference for WHAT the parser does
(row-per-chunk xlsx, semantic prose chunks, named dense+sparse vectors, verbatim
identifiers). The code is the source of truth where they differ.

**Precondition — regression must pass before ingest.** The parser ships with an
offline regression that needs no Qdrant/embedder:
```bash
FIXTURES=tests/fixtures python services/ingester/test_xlsx_tables.py
```
This must print `ALL PASS` (exit 0). If it does not, STOP and set this step
`FAILED` — the working tree does not match the validated parser; do not ingest a
corpus through a parser that fails its own fixtures. Diagnose from the failing
assertion, propose a fix, and WAIT for Dave (do not edit the frozen files to make
the test pass).

**Commands:**
```bash
cd ~dave/ragfarm
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); sets NO_PROXY so 127.0.0.1 probes + compose containers bypass any proxy

# 0. regression gate on the frozen parser (no services needed):
FIXTURES=tests/fixtures python services/ingester/test_xlsx_tables.py

# 1. bring up Qdrant ONLY (the ingester runs on the host, not in a container):
docker compose -f infra/compose.yaml up -d qdrant

# 2. confirm step-03 embedder is live (ingester calls :8090/embed):
curl -s 127.0.0.1:8090/embed \
  -H 'Content-Type: application/json' \
  -d '{"input":["prod-kvm-03 10.20.1.43 VLAN203"],"kind":"passage"}' >/dev/null

# 3. confirm the corpus is present and non-empty BEFORE ingest (empty => BLOCKED,
#    a 0-chunk run is a FAILURE not a pass):
ls /data/corpus | head

# 4. ingest on the HOST. Pass --corpus explicitly (do not rely on .env autoload;
#    ingester.py reads CORPUS_PATH from the environment, and bare python does not
#    load .env). --recreate rebuilds on schema/model change:
python services/ingester/ingester.py --corpus /data/corpus --recreate

# 5. verify the collection and that both vector types are present:
curl -s 127.0.0.1:6333/collections/corpus | python3 -m json.tool
```

**Gate:** the regression prints `ALL PASS`; collection `corpus` exists with point
count > 0 and a schema showing BOTH a `dense` (size 1024) and a `sparse` named
vector; a probe query for a known hostname (e.g. `hsmbvxip001ts`) returns the
correct table row in the top results (proves sparse exact-match), AND a Czech
semantic query returns a relevant doc chunk (proves multilingual dense). If
`CORPUS_PATH` is unset or unreachable → `BLOCKED`, not `FAILED`.

---

### 05 — mcp-placement  (reference implementation) — BLOCKED in PoC
`services/mcp-placement` is already written and its XML parsing is
unit-tested. It is the pattern the other MCPs are modelled on. Verifying it against
a live cluster requires OpenNebula, which the PoC does not have.

**This step is BLOCKED, not runnable, until deployment** (see ADR-0003). Do not
attempt to verify it against a mock — a mock `where_is_vm` does not satisfy this
gate and must not be committed as if it did. The code shape is already proven by
unit tests; what is deferred is the live round-trip only.

**Precondition (Dave-supplied, deferred to deployment):** real `ONE_XMLRPC`
endpoint and `ONE_AUTH` credentials in `.env` (shape in `.env.example`), plus
reachability to the OpenNebula frontend. Absent → `BLOCKED`.

**On deployment (ON available), unblock and run:**
```bash
cd ~dave/ragfarm
cp -n .env.example .env   # then fill ONE_XMLRPC + ONE_AUTH
python services/mcp-placement/server.py &
# call where_is_vm against a known VM; expect the live host it runs on
```

**Gate (deployment only):** `where_is_vm("<known VM>")` returns the correct live
host, sourced from OpenNebula (`one.vm.info` / `one.vmpool.info`), not a mock.

---

### 06 — mcp-fs-host-control — partially deferred (BLOCKED in PoC)
fs and host-control. **host-control stays SAFETY-GATED:** dry-run default,
allowlist, explicit confirm flag. Implement drain-then-reboot via OpenNebula before
enabling any real action. Model both on the step-05 reference implementation.

**host-control real actions are BLOCKED until live OpenNebula exists** (see
ADR-0003) — its drain-then-reboot path cannot be verified without a cluster, and
must never be enabled against an unverified ON connection. fs (sandboxed
read) has no ON dependency and MAY be implemented and tested now if you choose; if
you do, keep it scoped to read-only sandboxed paths.

**On deployment (ON available), unblock and run:**
```bash
cd ~dave/ragfarm
python services/mcp-host-control/server.py &
# a reboot request WITHOUT confirm must return a dry-run plan and take no action;
# with confirm against an allowlisted host, perform drain-then-reboot via ON.
```

**Gate (deployment only):** fs returns sandboxed read results for an allowed
path AND refuses a path outside the sandbox; host-control, given a reboot request
without the confirm flag, returns a dry-run plan and performs **no** real action;
with the confirm flag against an allowlisted host, performs drain-then-reboot via
OpenNebula.

---

### 07 — agent-wiring — Open WebUI + mcpo (ADR-0003), RAG-only milestone now
**Per ADR-0003: the custom `services/agent/agent.py` is RETIRED.** Open WebUI is
the agent loop; mcpo bridges the MCP servers to OpenAPI tools Open WebUI can call.
Retrieval is Option B — the UI does NOT use its own document-RAG; all corpus
retrieval flows through the `search_corpus` MCP tool (hybrid dense+sparse over
Qdrant + BGE-M3). The inference server is addressed only via its OpenAI-compatible
base URL (`http://127.0.0.1:8080/v1`) so it stays swappable — that design choice is
precisely what let the serving engine change from llama.cpp to vLLM (ADR-0013)
without touching this layer.

**Also in scope on the Spark:** the reranker (`bge-reranker-v2-m3` on a llama.cpp
CUDA `--reranking` server at `:8081`, ADR-0008) must be up before retrieval quality
can be judged, and it should run at **lowered scheduling priority** so the
interactive LLM wins memory-bandwidth contention (ADR-0013 §3). `search_corpus`
also applies the ADR-0010 §1 gate (`_gate()` + Kneedle); note that `RAG_MIN_SCORE`
is still **0.0/uncalibrated** — see the ADR-0010 calibration debt in the handoff.

If `services/agent/agent.py` (or a `services/agent/` client-loop scaffold) exists,
remove it as part of this step and note the removal in the commit. Keep the
`rag-retrieval` MCP that exposes `search_corpus`.

This step has TWO gates. The **RAG-only milestone** is provable now (steps 02–04
DONE). The **full gate** additionally requires the OpenNebula-backed tools and is
deferred to deployment (steps 05/06 BLOCKED).

**Build the `rag-retrieval` MCP `search_corpus`** following
`services/mcp-gateway/README.md`: embed the query via `/embed` with `kind=query`,
then HYBRID retrieval — dense + sparse with RRF fusion (Qdrant Query API prefetch on
both named vectors). This is what makes exact host/IP lookups and Czech/English
semantic search both work from one tool.

**Commands:**
```bash
cd ~dave/ragfarm
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); mcpo/Open WebUI bring-up fetches deps, NO_PROXY keeps 127.0.0.1 + container calls direct

# 1. ensure vLLM (step 02) and the embedder (step 03) are up, Qdrant (step 04) is
#    up with the `corpus` collection populated, and the reranker is live on :8081.

# 2. start the rag-retrieval MCP (search_corpus over Qdrant + :8090/embed):
python services/mcp-gateway/server.py &   # or the rag-retrieval entrypoint per its README

# 3. bring up mcpo bridging the MCP server(s) to OpenAPI:
#    (mcpo exposes each MCP tool as an OpenAPI operation Open WebUI can call)
#    follow services/mcp-gateway/README.md for the mcpo config (ports, tool names).

# 4. bring up Open WebUI pointed at the vLLM OpenAI endpoint, with the
#    mcpo OpenAPI tool server registered so search_corpus is callable from chat.
#    Open WebUI runs containerized or as a host service per its README; it must
#    talk to http://127.0.0.1:8080/v1 and must NOT be configured to do its own
#    corpus RAG.

# 5. end-to-end probe (RAG-only milestone): a chat query whose answer requires a
#    search_corpus call returns a grounded answer citing a retrieved corpus chunk.
```

**Gate — RAG-only milestone (provable now, sets status DONE):**
Open WebUI is reachable, drives vLLM via the OpenAI endpoint, and a chat
query that requires retrieval triggers a `search_corpus` MCP call (via mcpo) and
returns an answer demonstrably grounded in a retrieved corpus chunk. Verify BOTH
retrieval modes through the UI: a known hostname (e.g. `hsmbvxip001ts`) returns the
correct record (sparse exact-match), AND a Czech semantic query returns a relevant
chunk (multilingual dense). `services/agent/agent.py` is removed.

**Gate — full (deferred to deployment, after 05/06 unblock):**
the RAG-only gate PLUS an end-to-end query that drives an OpenNebula-backed tool
(`where_is_vm`) through mcpo and returns a grounded answer using a live infra
lookup. Do not mark this full gate met while 05/06 are BLOCKED; do not mock
`where_is_vm` to force it.

**Deploy fragment (write on DONE):** marker `deploy-step-07-agent-wiring`. Guard:
`curl -sf --max-time 5 http://127.0.0.1:3000/health` **and** the mcpo tool route
answering (`http://127.0.0.1:8000/rag/openapi.json`) — the UI being up does not
prove the tools mounted. Body = the compose bring-up plus the idempotent
`infra/openwebui/setup_openwebui.py` run that registers tool servers and upserts
the model presets.

---

## Notes for whoever executes this build

- **`deploy.sh` is the deliverable.** Every step that reaches `DONE` writes its
  guarded fragment into `scripts/deploy.sh` (CLAUDE.md → "The deploy.sh fragment
  contract"). A `DONE` step with no fragment is incomplete. You are the only party
  who knows the command sequence that actually worked, corrections included —
  write it down while you still have it.
- **Frozen files.** `services/ingester/ingester.py`, `services/ingester/xlsx_tables.py`,
  the MCP services and the manifests are read-only inputs. Disagree via the
  `PROGRESS.md` blocker channel; never edit them to make a gate pass.
- **`BLOCKED` vs `FAILED`.** Missing CUDA/python/checkpoint/credentials → `BLOCKED`
  (only Dave can supply). A command you ran whose gate did not pass → `FAILED`
  (diagnose, propose, and WAIT for confirmation before retrying).
- **Ports are fixed** across the whole system: vLLM `:8080`, reranker `:8081`,
  embedder `:8090`, Qdrant `:6333`, mcpo `:8000`, rag-retrieval `:8104`,
  Open WebUI `:3000`. `.env` at the repo root is the single source of truth for
  configuration; do not scatter values into units or compose.
