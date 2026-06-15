# BUILD_STATE — single source of truth for build progress

Updated by the agent per the build protocol in `CLAUDE.md` (Chapter 2).
Status values: `PENDING` | `DONE` | `FAILED` | `BLOCKED`.
Raw stdout+stderr per step lives in `logs/<NN-stepname>.log`.
All timestamps are UTC. Keep summaries ≤120 chars; they point at the log, they
do not reproduce it.

## Status table

| NN | step                | status  | updated_utc | log                            | summary |
|----|---------------------|---------|-------------|--------------------------------|---------|
| 01 | npu-bringup         | DONE    | 2026-06-15T19:10Z | logs/01-npu-bringup.log   | DKMS xrt-amdxdna 1.0.0, pgtbl_v2+iommu=on, RyzenAI-npu4, Test Finished |
| 02 | igpu-llm            | PENDING |             | logs/02-igpu-llm.log           |         |
| 03 | embedder-service    | PENDING |             | logs/03-embedder-service.log   |         |
| 04 | qdrant-ingester     | PENDING |             | logs/04-qdrant-ingester.log    |         |
| 05 | mcp-placement       | PENDING |             | logs/05-mcp-placement.log      |         |
| 06 | mcp-fs-host-control | PENDING |             | logs/06-mcp-fs-host-control.log|         |
| 07 | agent-wiring        | PENDING |             | logs/07-agent-wiring.log       |         |

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
Wrap the Quark-compiled encoder (running on the NPU from step 01) behind an HTTP
`/embed` service on `:8090`. Ingestion (step 04) and retrieval (step 07) call it.
Record the exact model+revision in `models/embeddings/MODEL.md`.

**Commands:**
```bash
# Start the embedder service (RyzenAI ONNX EP, Quark-compiled encoder) on :8090.
# Probe it:
curl -s 127.0.0.1:8090/embed \
  -H 'Content-Type: application/json' \
  -d '{"input":["hello world"]}'
```

**Gate:** the probe returns `{"embeddings": [[...]]}` with a non-empty vector of
the expected dimensionality, AND `models/embeddings/MODEL.md` records the exact
model + revision used.

---

### 04 — qdrant-ingester
Bring up Qdrant and ingest a small test corpus; retrieval depends on a populated
collection. Depends on step 03 (`/embed`) being up.

**Commands:**
```bash
docker compose -f infra/compose.yaml up -d qdrant
python services/ingester/ingester.py --corpus <small-test-corpus-path>
# verify the collection:
curl -s 127.0.0.1:6333/collections/corpus
```

**Gate:** collection `corpus` exists AND reports a point count > 0.

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
embedder (step 03). Follow `services/mcp-gateway/README.md`.

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
