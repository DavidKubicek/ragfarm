# CLAUDE.md — project contract (auto-loaded every session)

This file is the stable contract for the `ragfarm/` build. It is loaded into
context at the start of every Claude Code session in this directory. It is
**read-mostly**: do not edit it, the ADRs, or any `docs/decisions/*` file as part
of executing a build step. If the plan itself is wrong, raise it through the
blocker channel (Chapter 2) and wait.

Progress state does NOT live in this file or in conversation history (sessions
are siloed across CLI / Remote Control / IDE). It lives in `BUILD_STATE.md`
(linear build progress) and `PROGRESS.md` (human-gated blockers). Chapter 2
defines exactly how to use both.

---

## Chapter 1 — Architecture, constraints, build-order definition

You are continuing a project scaffolded in a planning session. Read `README.md`
and `docs/decisions/ADR-0001` and `ADR-0002` first — they explain *why* the
architecture is shaped this way. Do not redesign the engine split without
updating ADR-0001.

### The single most important constraint
Target is **Linux** on an AMD **Ryzen AI 9 HX 370**. On Linux, AMD RyzenAI 1.7.1
gives an **NPU-only LLM flow**; the hybrid single-model NPU+iGPU flow is
**Windows-only**, and **llama.cpp reaches the iGPU only, never the NPU**.
Therefore the engine split is:

- **iGPU** runs the generative LLM (llama.cpp + Vulkan).
- **NPU** runs the embedding model (RyzenAI EP, quantized with AMD Quark).
- **CPU** runs Qdrant + the MCP services.

If you are ever tempted to "just use the NPU for the LLM," re-read ADR-0001:
decode on the NPU is ~17 tok/s and unsuitable for an interactive agent.

### Models and how to engage them
- **Generative LLM:** `Qwen2.5-7B-Instruct`, GGUF **Q4_K_M**, served by
  `llama-server` (Vulkan) on `127.0.0.1:8080`, OpenAI-compatible, `--jinja` on
  for tool-calling. This is the model the agent/gateway drives.
- **Embedding model (NPU):** a BF16-friendly sentence encoder (BGE/E5/MiniLM
  class). Quantize/compile with **AMD Quark** → RyzenAI ONNX EP. Expose it as a
  tiny HTTP `/embed` service on the host at `:8090` (ingester + retrieval call
  it). Record exact model+revision in `models/embeddings/MODEL.md`.
- **ROCm vs Vulkan:** use **Vulkan**. ROCm on gfx1150 is unofficial — explore
  only after Vulkan works, and keep it behind its own ADR.

### Build-order definition (authoritative sequence)
The order below is fixed and matches ADR-0001. Per-step commands and gate-checks
live in `BUILD_STATE.md`; this is the canonical list of *what* the steps are and
*why they are ordered this way*.

1. **npu-bringup** — stand up the NPU runtime first; the embedder depends on it.
   Requires account-gated downloads only Dave can fetch.
2. **igpu-llm** — build llama.cpp+Vulkan and serve Qwen2.5-7B; the agent layer
   depends on this endpoint.
3. **embedder-service** — wrap the Quark-compiled encoder behind `:8090/embed`;
   ingestion and retrieval depend on it.
4. **qdrant-ingester** — bring up Qdrant and ingest a test corpus; retrieval
   depends on a populated collection.
5. **mcp-placement** — already written and unit-tested; it is the **reference
   implementation**. Wire real OpenNebula creds and verify against the live
   cluster. Model the other MCPs on it.
6. **mcp-fs-host-control** — fs-agent and host-control stubs. host-control is
   SAFETY-GATED (dry-run default, allowlist, confirm flag) — keep it that way;
   implement drain-then-reboot via OpenNebula before enabling real actions.
7. **agent-wiring** — client-side agent: OpenAI-compatible client → llama-server,
   MCP client → the HTTP MCP servers, expose tools to the model. Add a
   `rag-retrieval` MCP that queries Qdrant (`search_corpus`).

### Salvaged context from the originating planning session
- The engine-split decision and the measured NPU prefill/decode numbers live in
  ADR-0001 and `docs/ryzenai/AMD_FACTS.md`.
- OpenNebula is the placement owner (XML-RPC `one.vm.info` / `one.vmpool.info`);
  the placement MCP is built around that, **not** libvirt.
- "Quark" = AMD's quantizer for the NPU embedding path (ADR-0002).
- Discarded from the half-asleep first brief: "Optane tool" (Optane is
  discontinued; the real item was Quark) and the assumption that llama.cpp or
  ROCm gets you onto the NPU (it does not).

### Open questions for Dave (not blockers for steps 1–4)
- Exact embedding model preference, or accept the agent's BF16-friendly pick.
- BIOS version string + confirmation the NPU is enabled (transcribe into
  `docs/hardware/bios-f5x.md` from the screenshot).
- Corpus location on the host (compose assumes `/srv/corpus`, read-only).

---

## Chapter 2 — Build protocol (READ THIS FIRST, EVERY SESSION)

Three files carry all cross-session state. Conversation history does not persist;
these files do.

- `BUILD_STATE.md` — single source of truth for **linear build progress**. One
  row per step, plus each step's commands and gate-check. You read it on start
  and update it after every step.
- `PROGRESS.md` — the **blocker channel** between you and Dave. You append
  `BLOCKED:` entries here when you need something only Dave can provide. Dave
  flips them to `UNBLOCKED:` when he has supplied it. This is the only file Dave
  writes into to steer the build.
- `logs/<NN-stepname>.log` — raw stdout+stderr per step. Bulk output goes here,
  never into BUILD_STATE.md, PROGRESS.md, or your chat reply.

### On session start
1. Read `BUILD_STATE.md`. Identify the first step whose status is not `DONE`.
2. Read `PROGRESS.md`. If any entry for a step is still `BLOCKED:`, that step is
   not eligible to run — skip it and take the next non-`DONE`, non-`BLOCKED` step
   in order. If an entry is now `UNBLOCKED:`, that step is eligible again: re-run
   its gate-check and proceed with it.
3. Resume from the first eligible step. Do NOT re-run `DONE` steps unless Dave
   asks, or unless that step's gate-check now fails.
4. Do NOT skip the planned order for any reason other than an active `BLOCKED:`.

### For each step you execute
1. Run the step's commands exactly as defined in `BUILD_STATE.md`.
2. Append the full stdout+stderr to `logs/<NN-stepname>.log` (create if absent;
   append, never truncate). Do NOT paste raw output into BUILD_STATE.md or your
   reply.
3. Run the step's **Gate** (defined in that step's row in BUILD_STATE.md).
   - Gate passes → set status `DONE`.
   - Gate fails → set status `FAILED`.
4. Update that step's status line in `BUILD_STATE.md`: status, UTC timestamp,
   log path, and a one-line summary (≤120 chars). Keep the file small — the
   summary points at the log; it does not reproduce it.

### On FAILED (agent can act; needs Dave's confirm to retry)
A `FAILED` step is one you ran but whose gate did not pass, and which you can
diagnose yourself.
- Stop. Read the relevant tail of `logs/<NN-stepname>.log` and summarize the
  probable cause in ≤5 lines.
- Propose the fix and **WAIT for Dave's explicit confirmation before retrying.**
  Never loop unattended on a failing step — several steps touch live OpenNebula
  infra that can reboot hosts.
- After Dave confirms, retry the **same** step. Re-running replaces that step's
  status line and appends (never truncates) its log.

### On BLOCKED (only Dave can clear; hand off and move on)
A step is `BLOCKED`, not `FAILED`, when you **cannot proceed without Dave**:
an account-gated file is missing, OpenNebula creds/reachability are absent, a
BIOS/EC toggle is needed, or anything else only Dave can supply. When you hit
such an obstacle:
1. Append a `BLOCKED:` entry to `PROGRESS.md` (format below) stating exactly what
   is needed and the exact command/file path/credential involved.
2. Set that step's status in `BUILD_STATE.md` to `BLOCKED` with the same UTC
   timestamp, so the table and the ledger agree.
3. Continue with the next eligible (non-`DONE`, non-`BLOCKED`) step in order.
4. Do NOT fake, mock, or work around a hard blocker in committed code. Clearly
   named test mocks are fine; silently routing around a missing dependency is not.

`PROGRESS.md` entry format (one block per blocker, newest appended at the end):
```
BLOCKED: <NN-stepname> — <UTC timestamp>
  need:   <exactly what Dave must supply>
  where:  <exact path / command / .env key / BIOS field involved>
  detail: <one or two lines of context>
```
Dave clears it by editing that block's first line to:
```
UNBLOCKED: <NN-stepname> — <UTC timestamp Dave cleared it>
  supplied: <what he did — file in place, creds in .env, toggle set, etc.>
```

### On resume after a blocker is cleared
When `PROGRESS.md` shows an `UNBLOCKED:` entry for a step:
1. Set that step's status in `BUILD_STATE.md` back to `PENDING`.
2. Re-run that step's commands and gate per the normal execution loop above.
3. Leave the `UNBLOCKED:` block in `PROGRESS.md` as the historical record; do not
   delete it.

### Hard rules
- Never edit `CLAUDE.md`, the ADRs, or `docs/decisions/*` during build execution.
- Never put raw build output anywhere except `logs/`.
- Never skip a step except for an active `BLOCKED:`.
- All timestamps are UTC.
