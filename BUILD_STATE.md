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
| 04 | qdrant-ingester     | PENDING |                   | logs/04-qdrant-ingester.log    |         |
| 05 | mcp-placement       | PENDING |                   | logs/05-mcp-placement.log      |         |
| 06 | mcp-fs-host-control | PENDING |                   | logs/06-mcp-fs-host-control.log|         |
| 07 | agent-wiring        | PENDING |                   | logs/07-agent-wiring.log       |         |

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

Model: **BAAI/BGE-M3** (568M, 100+ languages incl. Czech, up to 8192 tokens).
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
print("snapshot:", snapshot_download("BAAI/bge-m3"))
PY

# Start the service (loads model once, serves on :8090):
python services/embedder/server.py &

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
collection. Depends on step 03 (`/embed`) being up. The ingestion design — how
docx vs xlsx are parsed and chunked — is specified in
`docs/ingestion-pipeline.md`; follow it, do not invent a different chunker.

Key points (full detail in that doc):
- **xlsx** → one row per chunk, serialized as a flat `key: value, ...` string so
  each host/IP/VLAN record is independently retrievable. Header row defines keys.
  Identifiers kept verbatim (no lowercasing) so sparse exact-match works.
- **docx** → semantic chunks of ~256–384 tokens with ~15% overlap, split on
  headings/paragraphs, never mid-sentence. Tables inside docx route to the xlsx path.
- Each chunk stored with BOTH a dense named vector and a sparse named vector from
  step 03, enabling hybrid (RRF) search. Collection `corpus` uses named vectors
  `dense` (1024, cosine) and `sparse` (sparse index).
- Payload per point: `source_file`, `kind` (`table_row`|`doc_text`), `lang`
  (best-effort), and the raw chunk text. Deterministic point IDs for idempotent
  re-ingest; `--recreate` to rebuild on model/schema change.

**Commands:**
```bash
docker compose -f infra/compose.yaml up -d qdrant
# Ingest the real corpus dir (CORPUS_PATH); start with a 2-3 file subset to verify:
python services/ingester/ingester.py --corpus "$CORPUS_PATH" --recreate
# verify the collection and that both vector types are present:
curl -s 127.0.0.1:6333/collections/corpus | python3 -m json.tool
```

**Gate:** collection `corpus` exists, reports point count > 0, AND its schema
shows both a `dense` (size 1024) and a `sparse` named vector. A probe query for a
known hostname returns the correct table row in the top results (proves sparse
exact-match works), AND a Czech semantic query returns a relevant doc chunk
(proves multilingual dense works).

---

### 05 — mcp-placement  (reference implementation)
`services/mcp-infra-placement` is already written and its XML parsing is
unit-tested. Wire real OpenNebula credentials and verify against the live
cluster. This is the pattern the other MCPs are modelled on.

**Precondition (Dave-supplied):** real `ONE_XMLRPC` endpoint and `ONE_AUTH`
credentials in `.env` (copy shape from `.env.example`), and network reachability
to the OpenNebula frontend. If creds or reachability are absent → `BLOCKED`,
not `FAILED`.

**Commands:**
```bash
cp -n .env.example .env   # then fill ONE_XMLRPC + ONE_AUTH
# start the placement MCP, then verify a live lookup:
python services/mcp-infra-placement/server.py &
# call where_is_vm against a known VM (replace VM1 with a real name/id):
#   expect the live host the VM currently runs on
```

**Gate:** `where_is_vm("VM1")` returns the correct live host for a known VM,
sourced from OpenNebula (`one.vm.info` / `one.vmpool.info`), not a mock.

---

### 06 — mcp-fs-host-control
fs-agent and host-control are working stubs. **host-control stays SAFETY-GATED:**
dry-run default, allowlist, explicit confirm flag. Implement drain-then-reboot
via OpenNebula before enabling any real action. Model both on the step-05
reference implementation.

**Commands:**
```bash
# Implement fs-agent (sandboxed read) and host-control (drain-then-reboot via
# OpenNebula) following the mcp-infra-placement pattern.
# Verify host-control refuses to act without the confirm flag (dry-run default):
python services/mcp-host-control/server.py &
# a reboot request WITHOUT confirm must return a dry-run plan and take no action.
```

**Gate:** fs-agent returns sandboxed read results for an allowed path AND refuses
a path outside the sandbox; host-control, given a reboot request without the
confirm flag, returns a dry-run plan and performs **no** real action; with the
confirm flag against an allowlisted host, it performs drain-then-reboot via
OpenNebula.

---

### 07 — agent-wiring
Wire the client-side agent: OpenAI-compatible client → llama-server (step 02),
MCP client → the HTTP MCP servers (steps 05–06), expose their tools to the model.
Add a `rag-retrieval` MCP that queries Qdrant (`search_corpus`) using the
embedder (step 03), following `services/mcp-gateway/README.md`. `search_corpus`
must embed the query via `/embed` with `kind=query`, then do HYBRID retrieval:
dense + sparse with RRF fusion (Qdrant Query API prefetch on both named vectors).
This is what makes exact host/IP lookups and Czech/English semantic search both
work from one tool.

**Commands:**
```bash
# Start the agent (client-side: llama-server + MCP clients), then run an
# end-to-end probe that requires a tool call and a corpus retrieval:
python services/agent/agent.py
# e.g. ask a question whose answer requires search_corpus + where_is_vm.
```

**Gate:** an end-to-end query drives at least one MCP tool call (e.g.
`where_is_vm` or `search_corpus`) and returns a grounded answer that
demonstrably used a retrieved corpus chunk and/or a live infra lookup.
