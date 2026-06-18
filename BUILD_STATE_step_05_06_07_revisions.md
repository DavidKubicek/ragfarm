# BUILD_STATE.md — step 05/06/07 replacement blocks (apply manually, sync first)

Per ADR-0003. Replace the three existing step-definition blocks (`### 05`, `### 06`,
`### 07`) with the versions below. Update the status-table rows as noted. Do this
yourself from the IDE while no agent holds the lock, then push — the build agent
must NOT edit step definitions itself.

---

## Status-table rows

Set these three rows. 05 and 06 go straight to BLOCKED (no OpenNebula in the PoC);
07 stays PENDING but its scope has changed (RAG-only milestone).

```
| 05 | mcp-placement       | BLOCKED |  <UTC>            | logs/05-mcp-placement.log      | no OpenNebula access in PoC; ON creds+reachability deferred to deployment |
| 06 | mcp-fs-host-control | BLOCKED |  <UTC>            | logs/06-mcp-fs-host-control.log| host-control needs live ON (drain-reboot); deferred to deployment |
| 07 | agent-wiring        | PENDING |                   | logs/07-agent-wiring.log       |         |
```

Also append the two BLOCKED entries to `PROGRESS.md` (newest at the end):

```
BLOCKED: 05-mcp-placement — <UTC>
  need:   live OpenNebula access — ONE_XMLRPC endpoint + ONE_AUTH credentials,
          and network reachability to the ON frontend
  where:  .env keys ONE_XMLRPC, ONE_AUTH (shape in .env.example)
  detail: PoC MiniPC is not yet placed on the live network; ON access lands at
          deployment. The placement MCP code + XML parsing are unit-tested; only
          the live one.vm.info/one.vmpool.info round-trip is unverified.

BLOCKED: 06-mcp-fs-host-control — <UTC>
  need:   live OpenNebula access (same as 05) before host-control real actions
  where:  .env keys ONE_XMLRPC, ONE_AUTH
  detail: fs-agent (sandboxed read) can be implemented and tested now, but
          host-control's drain-then-reboot is via ON and cannot be verified
          without a live cluster. Keep host-control dry-run/confirm-gated; do not
          enable real actions until ON is reachable at deployment.
```

---

## ### 05 — mcp-placement  (reference implementation) — BLOCKED in PoC

`services/mcp-infra-placement` is already written and its XML parsing is
unit-tested. It is the pattern the other MCPs are modelled on. Verifying it against
a live cluster requires OpenNebula, which the PoC does not have.

**This step is BLOCKED, not runnable, until deployment.** Do not attempt to verify
it against a mock — a mock `where_is_vm` does not satisfy this gate and must not be
committed as if it did. The code shape is already proven by unit tests; what is
deferred is the live round-trip only.

**Precondition (Dave-supplied, deferred to deployment):** real `ONE_XMLRPC`
endpoint and `ONE_AUTH` credentials in `.env` (shape in `.env.example`), plus
reachability to the OpenNebula frontend. Absent → BLOCKED.

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

## ### 06 — mcp-fs-host-control — partially deferred (BLOCKED in PoC)

fs-agent and host-control. **host-control stays SAFETY-GATED:** dry-run default,
allowlist, explicit confirm flag. Implement drain-then-reboot via OpenNebula before
enabling any real action. Model both on the step-05 reference implementation.

**host-control real actions are BLOCKED until live OpenNebula exists** — its
drain-then-reboot path cannot be verified without a cluster, and must never be
enabled against an unverified ON connection. fs-agent (sandboxed read) has no ON
dependency and MAY be implemented and tested now if you choose; if you do, keep it
scoped to read-only sandboxed paths.

**On deployment (ON available), unblock and run:**
```bash
cd ~dave/ragfarm
python services/mcp-host-control/server.py &
# a reboot request WITHOUT confirm must return a dry-run plan and take no action;
# with confirm against an allowlisted host, perform drain-then-reboot via ON.
```

**Gate (deployment only):** fs-agent returns sandboxed read results for an allowed
path AND refuses a path outside the sandbox; host-control, given a reboot request
without the confirm flag, returns a dry-run plan and performs NO real action; with
the confirm flag against an allowlisted host, performs drain-then-reboot via
OpenNebula.

---

## ### 07 — agent-wiring — Open WebUI + mcpo (ADR-0003), RAG-only milestone now

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
