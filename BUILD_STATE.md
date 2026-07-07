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
| 01 | npu-bringup         | SKIP    | 2026-06-15T19:10Z | logs/01-npu-bringup.log        | NPU UNUSED, but with CMDLINE="amd_iommu=pgtbl_v2 iommu=on" NPU/VitisAI works perfectly, Test Finished |
| 02 | igpu-llm            | DONE    | 2026-06-15T20:05Z | logs/02-igpu-llm.log           | llama.cpp+Vulkan built, Qwen2.5-7B-Instruct Q4_K_M loaded, gate passed (Pong! at ~11.8 tok/s) |
| 03 | embedder-service    | DONE    | 2026-06-16T08:10Z | logs/03-embedder-service.log   | BGEM3FlagModel CPU, safetensors=True, :8090/embed gate passed (1024-dim dense+sparse, EN+CS) |
| 04 | qdrant-ingester     | DONE    | 2026-06-18T10:05Z | logs/04-qdrant-ingester.log    | Qdrant up, 138 chunks ingested (2 xlsx+1 docx), dense+sparse schema, hostname sparse hit + Czech dense hit pass |
| 05 | mcp-placement       | BLOCKED | 2026-06-18T11:30Z | logs/05-mcp-placement.log      | no OpenNebula access in PoC; ON creds+reachability deferred to deployment (ADR-0003) |
| 06 | mcp-fs-host-control | BLOCKED | 2026-06-18T11:30Z | logs/06-mcp-fs-host-control.log| host-control needs live ON (drain-reboot); deferred to deployment (ADR-0003) |
| 07 | agent-wiring        | DONE    | 2026-07-07T09:20Z | logs/07-agent-wiring.log       | Open WebUI + mcpo + rag-retrieval (search_corpus hybrid RRF); RAG-only gate PASSED in browser via 'ragfarm (corpus RAG)' preset (grounding prompt): sparse hostname + Czech dense both grounded. Full gate (where_is_vm) deferred — 05/06 BLOCKED |

---

## Step definitions

Each step below carries its commands and its **Gate**. Execute a step only when
it is the first eligible step per the protocol. Run the commands, tee output to
the step's log, then evaluate the Gate to decide `DONE` vs `FAILED`.

### 01 — npu-bringup
Stand up the NPU runtime first; the embedder (step 03) depends on it.

**Precondition (Dave-supplied, account-gated):** these two files must be present
in `~/Downloads/ryzenai/` — they cannot be fetched by the agent:
- `RAI_1.7.1_Linux_NPU_XRT.zip`
- `ryzen_ai-1.7.1.tgz`

If either file is absent → this step is `BLOCKED`, not `FAILED`. Append a
`BLOCKED:` entry to `PROGRESS.md` naming the missing file(s) and path, set status
`BLOCKED`, and move to the next eligible step.

**Commands:**
```bash
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); installer fetches packages
ls -l ~/Downloads/ryzenai/RAI_1.7.1_Linux_NPU_XRT.zip ~/Downloads/ryzenai/ryzen_ai-1.7.1.tgz
bash infra/npu/install_npu.sh
source /opt/xilinx/xrt/setup.sh
source /opt/ryzenai/venv/bin/activate
xrt-smi examine
python /opt/ryzenai/venv/quicktest/quicktest.py
```

**Gate:** `xrt-smi examine` reports `RyzenAI-npu4` OR `NPU Strix`, AND 
`python /opt/ryzenai/venv/quicktest/quicktest.py` prints `Test Finished`.

---

### 02 — igpu-llm
Build llama.cpp with Vulkan and serve Qwen2.5-7B; the agent layer (step 07)
drives this endpoint. Follow `infra/llama/README.md` for build specifics.

**Commands:**
```bash
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); build clones/fetches deps + GGUF
# Build per infra/llama/README.md (Vulkan backend), then place the GGUF:
#   models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf
# Launch llama-server (Vulkan, OpenAI-compatible, tool-calling on):
#   via manifests/llama-server.service, or directly:
llama-server \
  -m models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  --jinja \
  -ngl 999
# probe:
curl -s 127.0.0.1:8080/v1/models
curl -s 127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-7b-instruct","messages":[{"role":"user","content":"ping"}]}'
```

**Gate:** `curl 127.0.0.1:8080/v1/models` returns the model alias, AND a chat
completion request returns a non-empty assistant message.

---

### 03 — embedder-service
Serve a multilingual embedder behind HTTP `/embed` on `:8090`. Ingestion (step 04)
and retrieval (step 07) call it. **Runs on CPU, not the NPU** — see ADR-0002 for
why the NPU path was abandoned for this corpus (mixed Czech + English; wide
structured table rows; NPU-fittable models are English-only and seq-limited). The
prior NPU build (bge-small-en-v1.5) is invalid and is deleted below.

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
- CPU inference via FlagEmbedding (`BGEM3FlagModel`, dense+sparse in one call).
  Fall back to ONNX-on-CPU dense + separate sparse only if the torch footprint
  is a problem.

**Commands:**
```bash
source scripts/proxy-env.sh   # load .env proxy vars (no-op if unset); pip + HF snapshot_download need egress
# Remove dead NPU-era embedder artifacts before rebuilding on CPU:
rm -rf models/embeddings/bge-small-en-v1.5-onnx-static/
find models/embeddings/ -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
rm -f models/embeddings/MODEL.md   # stale bge-small record; gate regenerates it
# (the GGUF LLM under models/gguf/ is step 02's — do NOT touch it)

# Install deps (CPU torch is fine; no CUDA):
pip install -U FlagEmbedding fastapi uvicorn

# Pin and record the revision actually pulled:
python - <<'PY'
from huggingface_hub import snapshot_download
print("snapshot:", snapshot_download("BAAI/bge-m3", revision="50f9396f75618b3389c1fd1068a1ff58dc7b5b26", ignore_patterns=["pytorch_model.bin"]))
PY

# Launch embedder-server via manifests/embedder-server.service as a new system unit.
# Probe dense+sparse with one numeric/English table row and one Czech sentence:
curl -s 127.0.0.1:8090/embed \
  -H 'Content-Type: application/json' \
  -d '{"input":["prod-kvm-03 10.20.1.43 VLAN203","jak zalohovat hostitele"],"kind":"passage"}'
```

**Gate:** the probe returns a `dense` array of 1024-dim vectors (one per input,
L2-norm ≈ 1.0) AND a non-empty `sparse` map per input, AND
`models/embeddings/MODEL.md` records `BAAI/bge-m3` with the resolved revision
hash. Both probe inputs — the English/numeric table row and the Czech sentence —
must return finite vectors; this is the explicit multilingual check the prior
bge-small build would have failed.

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
`services/mcp-infra-placement` is already written and its XML parsing is
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
python services/mcp-infra-placement/server.py &
# call where_is_vm against a known VM; expect the live host it runs on
```

**Gate (deployment only):** `where_is_vm("<known VM>")` returns the correct live
host, sourced from OpenNebula (`one.vm.info` / `one.vmpool.info`), not a mock.

---

### 06 — mcp-fs-host-control — partially deferred (BLOCKED in PoC)
fs-agent and host-control. **host-control stays SAFETY-GATED:** dry-run default,
allowlist, explicit confirm flag. Implement drain-then-reboot via OpenNebula before
enabling any real action. Model both on the step-05 reference implementation.

**host-control real actions are BLOCKED until live OpenNebula exists** (see
ADR-0003) — its drain-then-reboot path cannot be verified without a cluster, and
must never be enabled against an unverified ON connection. fs-agent (sandboxed
read) has no ON dependency and MAY be implemented and tested now if you choose; if
you do, keep it scoped to read-only sandboxed paths.

**On deployment (ON available), unblock and run:**
```bash
cd ~dave/ragfarm
python services/mcp-host-control/server.py &
# a reboot request WITHOUT confirm must return a dry-run plan and take no action;
# with confirm against an allowlisted host, perform drain-then-reboot via ON.
```

**Gate (deployment only):** fs-agent returns sandboxed read results for an allowed
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
base URL (`http://127.0.0.1:8080/v1`) so it stays swappable for NVIDIA HW later.

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

# 1. ensure llama-server (step 02) and the embedder (step 03) are up, Qdrant
#    (step 04) is up and the `corpus` collection is populated.

# 2. start the rag-retrieval MCP (search_corpus over Qdrant + :8090/embed):
python services/mcp-gateway/server.py &   # or the rag-retrieval entrypoint per its README

# 3. bring up mcpo bridging the MCP server(s) to OpenAPI:
#    (mcpo exposes each MCP tool as an OpenAPI operation Open WebUI can call)
#    follow services/mcp-gateway/README.md for the mcpo config (ports, tool names).

# 4. bring up Open WebUI pointed at the llama-server OpenAI endpoint, with the
#    mcpo OpenAPI tool server registered so search_corpus is callable from chat.
#    Open WebUI runs containerized or as a host service per its README; it must
#    talk to http://127.0.0.1:8080/v1 and must NOT be configured to do its own
#    corpus RAG.

# 5. end-to-end probe (RAG-only milestone): a chat query whose answer requires a
#    search_corpus call returns a grounded answer citing a retrieved corpus chunk.
```

**Gate — RAG-only milestone (provable now, sets status DONE):**
Open WebUI is reachable, drives llama-server via the OpenAI endpoint, and a chat
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
