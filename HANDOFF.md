# HANDOFF — build plan for Claude Code (autonomous)

You are continuing a project scaffolded in a planning session. This document is
authoritative. Read `README.md` and `docs/decisions/ADR-0001` and `ADR-0002`
first; they explain *why* the architecture is shaped this way. Do not redesign
the engine split without updating ADR-0001.

## The single most important constraint
Target is **Linux** on an AMD **Ryzen AI 9 HX 370**. On Linux, AMD RyzenAI 1.7.1
gives an **NPU-only LLM flow**; the hybrid single-model NPU+iGPU flow is
**Windows-only**, and **llama.cpp reaches the iGPU only, never the NPU**.
Therefore: **iGPU runs the generative LLM (llama.cpp+Vulkan); NPU runs the
embedding model (RyzenAI EP, quantized with Quark); CPU runs Qdrant + MCP
services.** If you ever feel tempted to "just use the NPU for the LLM," re-read
ADR-0001 — decode on the NPU is ~17 tok/s and unsuitable for an interactive agent.

## Models and how to engage them
- **Generative LLM:** `Qwen2.5-7B-Instruct`, GGUF **Q4_K_M**, served by
  `llama-server` (Vulkan) on `127.0.0.1:8080`, OpenAI-compatible, `--jinja` on
  for tool-calling. This is the model the agent/gateway drives.
- **Embedding model (NPU):** a BF16-friendly sentence encoder (BGE/E5/MiniLM
  class). Quantize/compile with **AMD Quark** → RyzenAI ONNX EP. Expose it as a
  tiny HTTP `/embed` service on the host at `:8090` (the ingester and retrieval
  call it). Record exact model+revision in `models/embeddings/MODEL.md`.
- **ROCm vs Vulkan:** use **Vulkan**. ROCm on gfx1150 is unofficial — only
  explore after Vulkan is working, and keep it behind its own ADR.

## Build order (do these in sequence; each is independently verifiable)
1. **NPU bring-up.** Dave must manually download (account-gated) into
   `~/Downloads/ryzenai/`: `RAI_1.7.1_Linux_NPU_XRT.zip` and `ryzen_ai-1.7.1.tgz`.
   Then run `infra/npu/install_npu.sh`. Gate: `xrt-smi examine` shows `NPU Strix`
   and `quicktest.py` prints `Test Finished`. **If the files are absent, STOP and
   leave a note — you cannot fetch them.**
2. **iGPU LLM.** Follow `infra/llama/README.md`: build llama.cpp with Vulkan,
   drop the GGUF in `models/gguf/`, launch `llama-server`. Gate:
   `curl 127.0.0.1:8080/v1/models` returns the alias, and a chat completion works.
3. **Embedder service.** Wrap the Quark-compiled encoder behind `:8090/embed`
   returning `{"embeddings": [[...]]}`. Gate: a probe request returns a vector.
4. **Qdrant + ingester.** `docker compose -f infra/compose.yaml up -d qdrant`,
   then run `services/ingester/ingester.py` against a small test corpus. Gate:
   collection `corpus` exists and has points.
5. **MCP: placement (already written, tested).** `services/mcp-infra-placement`
   is complete and its XML parsing is unit-tested. Fill `.env` from `.env.example`
   with real `ONE_XMLRPC` + `ONE_AUTH`, run it, and verify `where_is_vm("VM1")`
   returns the live host. This is the reference implementation — model the other
   MCPs on it.
6. **MCP: fs-agent, host-control.** Both are working stubs. host-control is
   SAFETY-GATED (dry-run default, allowlist, confirm flag) — keep it that way;
   implement drain-then-reboot via OpenNebula before enabling real actions.
7. **Gateway/agent.** Per `services/mcp-gateway/README.md`, wire a client-side
   agent: OpenAI-compatible client → llama-server, MCP client → the HTTP MCP
   servers, expose their tools to the model. Add a `rag-retrieval` MCP that
   queries Qdrant (`search_corpus`).

## How to run autonomously, and how to resume
- Work top-down through the build order. After each step, run its **Gate** check
  and commit. Keep a running log in `docs/decisions/PROGRESS.md` (create it):
  date, step, result, next action.
- **When you hit an obstacle you cannot clear** (missing account-gated file,
  missing OpenNebula credentials/reachability, a hardware/BIOS toggle, anything
  needing Dave), do this and then pause that thread:
  1. append a `BLOCKED:` entry to `docs/decisions/PROGRESS.md` stating exactly
     what is needed and the exact command/file path involved;
  2. continue with any *other* build-order step that is not blocked;
  3. do not fake or mock around a hard blocker in committed code (test mocks are
     fine and must be clearly named as such).
- **To resume after Dave clears a blocker:** Dave updates the `BLOCKED:` entry to
  `UNBLOCKED:` with whatever he supplied (file in place, creds in `.env`, etc.).
  On your next run, scan `PROGRESS.md` for `UNBLOCKED:` entries, re-run that
  step's Gate, and proceed.

## Salvaged from the originating planning session
- The engine-split decision and the measured NPU prefill/decode numbers
  (ADR-0001, `docs/ryzenai/AMD_FACTS.md`).
- OpenNebula is the placement owner (XML-RPC `one.vm.info`/`one.vmpool.info`);
  the placement MCP is built around that, not libvirt.
- "Quark" = AMD's quantizer for the NPU embedding path (ADR-0002).
- Discarded from the half-asleep first brief: "Optane tool" (Optane is
  discontinued; the real item was Quark) and the assumption that llama.cpp or
  ROCm gets you onto the NPU (it does not).

## Open questions for Dave (not blockers for steps 1-4)
- Exact embedding model preference, or accept the agent's BF16-friendly pick.
- BIOS version string + confirmation the NPU is enabled (transcribe into
  `docs/hardware/bios-f5x.md` from the screenshot).
- Corpus location on the host (compose assumes `/srv/corpus`, read-only).
