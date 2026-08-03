# PROGRESS — blocker channel between the agent and Dave

This is the only file Dave writes into to steer the build. The agent appends
`BLOCKED:` entries when it needs something only Dave can supply; Dave flips them
to `UNBLOCKED:` once supplied. Linear build progress lives in `BUILD_STATE.md`,
not here — this file carries only blockers and their resolution.

See `CLAUDE.md` Chapter 2 for exactly when and how this file is read and written.
All timestamps are UTC. Newest entries are appended at the end. Resolved entries
are kept as historical record, never deleted.

## Format

```
BLOCKED: <NN-stepname> — <UTC timestamp>
  need:   <exactly what Dave must supply>
  where:  <exact path / command / .env key / BIOS field involved>
  detail: <one or two lines of context>
```

Dave clears a blocker by editing that block's first line in place:

```
UNBLOCKED: <NN-stepname> — <UTC timestamp Dave cleared it>
  supplied: <what he did — file in place, creds in .env, toggle set, etc.>
  need:   <original need, left for the record>
  where:  <original where, left for the record>
  detail: <original detail, left for the record>
```

## Entries

UNBLOCKED: 01-npu-bringup — 2026-06-15T12:55Z
  need:   Reboot the machine so the staged kernel parameter takes effect.
  where:  /etc/default/grub — GRUB_CMDLINE_LINUX_DEFAULT now contains `amd_iommu=force_isolation`;
          update-grub has already been run; GRUB config is current.
  detail: The NPU PCI device (0000:c6:00.1) is in an IOMMU identity domain because
          the BIOS ACPI IVRS table has a unity-mapping entry for it. AMD IOMMU SVA
          (required by the amdxdna driver for PASID-based DMA) only works when the
          device is in a DMA-translated domain. The in-tree amdxdna driver logs
          "SVA bind device failed, ret -95" on every open(). The fix is to add
          `amd_iommu=force_isolation` which overrides IVRS unity-mapping entries and
          forces all devices into isolated (translated) domains.
          After reboot: re-run `xrt-smi examine` and `python infra/npu/quicktest.py`
          to verify the gate (NPU Strix in examine output, "Test Finished" from quicktest).

BLOCKED: 05-mcp-placement — 2026-06-18T11:30Z
  need:   live OpenNebula access — ONE_XMLRPC endpoint + ONE_AUTH credentials,
          and network reachability to the ON frontend
  where:  .env keys ONE_XMLRPC, ONE_AUTH (shape in .env.example)
  detail: PoC MiniPC is not yet placed on the live network; ON access lands at
          deployment. The placement MCP code + XML parsing are unit-tested; only
          the live one.vm.info/one.vmpool.info round-trip is unverified.

BLOCKED: 06-mcp-fs-host-control — 2026-06-18T11:30Z
  need:   live OpenNebula access (same as 05) before host-control real actions
  where:  .env keys ONE_XMLRPC, ONE_AUTH
  detail: fs-agent (sandboxed read) can be implemented and tested now, but
          host-control's drain-then-reboot is via ON and cannot be verified
          without a live cluster. Keep host-control dry-run/confirm-gated; do not
          enable real actions until ON is reachable at deployment.


NOTE: ADR-0006 activated — 2026-07-14 UTC
  owner:   Dave Kubicek (owner-directed capability change, not a build-step)
  summary: Content-addressed corpus sync with SQLite manifest + alias switch +
           autonomous watcher deployed and validated end-to-end.
  changes:
    - Step-04 "ingester frozen" rule superseded by ADR-0006 for owner-driven work;
      xlsx_tables.py parser remains off-limits to build agents.
    - Project venv moved: .venv on python3.12 replaces /opt/ryzenai/venv for both
      ingester and embedder. NPU env stripped from ragfarm-embedder.service.
    - ragfarm-ingester-watcher.service installed, enabled, wired into ragfarm-stack.
    - Legacy physical corpus collection migrated to alias corpus ->
      corpus_20260714-025915_901856e8 via --recreate (zero blackout after migration).
    - infra/compose.yaml ingester: block removed (superseded by host watcher).
  gate:    All §9 gates passed; see logs/ingester-adr0006.log.

NOTE: OWUI serving tuning (context handling + tool-calling) — 2026-07-14 UTC
  owner:   Dave Kubicek (owner-directed debugging, not a build-step)
  problem: Multi-turn RAG chats in Open WebUI hit a slow CPU-busy "before-tool"
           delay, context overflow (hard 400 errors), stalls, and degraded
           tool-calling (model narrated instead of calling reboot_host). Root
           cause: unbounded accumulation of REPLAYED tool-result blobs in OWUI
           conversation history — each turn resends the full prior tool outputs
           (a single docx "How-Tos" chunk is ~4k tokens), so prompts reached
           10-14k+ tokens. A warm llama prompt-cache had hidden the cost for days;
           a llama restart exposed it (cold full re-prefill of the whole history).
           NOT caused by the .venv/embedder move or the watcher.
  changes:
    - manifests/ragfarm-llama.service: -c 16384 -> -c 32768; added
      --context-shift --keep 3072. NOTE: --context-shift only rescues GENERATION
      overflow, NOT an oversized prompt (verified: 28k-token prompt still returns
      400 exceed_context_size_error). So 32k only raises the ceiling; it does not
      make overflow impossible. --keep 3072 preserves system prompt + tool schemas
      across shifts. (-parallel 1 was tried then reverted to default 4 slots;
      prompt-cache restore makes single-slot safe but 4 slots matched the proven
      baseline.)
    - OWUI model "ragfarm": function_calling native -> default. THIS was the
      effective fix — restored immediate/correct tool calls, faster generation,
      and cut per-turn accumulation. Stored in the openwebui_data docker volume
      (webui.db), NOT version-controlled; revert with a one-field DB update.
  tradeoff / PROD caveat:
    - In "default" mode the model answers a REPEATED question from context
      instead of re-calling the tool (observed: 2nd "Kde bezi sftp-gw?" and 2nd
      "Jak se prihlasim?" made zero tool calls). Harmless in test, but STALE
      ANSWERS are unacceptable for production.
    - PROD PLAN: return to function_calling = "native" for the real deployment,
      paired with a larger/newer model + bigger context window (which is what
      lets native mode carry the accumulated trace without overflowing).
    - A reboot sentence that prefaced one login answer was verified as a pure
      context echo (that turn called zero tools) — no host-control execution.
  deferred lever #2 (accumulation suppression via retrieval; NOT applied):
    - In services/rag-retrieval/server.py, cap each result's "text" (~800 chars)
      and lower default k (5 -> 4) so each tool result is ~1k instead of ~4k
      tokens, slowing history growth. Parked because it degrades answer grounding
      to patch a plumbing issue; the clean fix is finer docx chunking in the
      (frozen) ingester parser. Keep #2 as a fallback if context buildup returns.

BLOCKED: 02-vllm-serving — 2026-08-03T12:34Z
  need:   a decision on WHERE vLLM is installed. BUILD_STATE step 02 command 1
          says "vLLM into the step-01 venv" (.venv/bin/pip install -U vllm).
          Verified by dry-run on the Spark: doing that dismantles the environment
          step 01 just gated. Recommendation: give vLLM its OWN venv (.venv-vllm)
          and leave .venv as the pinned CUDA-13 env for embedder/ingester/MCP.
  where:  BUILD_STATE.md step 02 command 1; scripts/deploy.sh phase_venv;
          manifests/ragfarm-vllm.service (not yet written); .venv vs .venv-vllm
  detail: `.venv/bin/pip install --dry-run -U 'vllm>=0.22.0'` resolves vllm 0.26.0
          and would change, in the SAME venv the ingester and embedder run from:
            torch            2.13.0+cu130 -> 2.11.0   (plain PyPI, NOT a cu130
                                                       build — this alone undoes
                                                       step 01 gate 3, sm_121)
            numpy            1.26.4       -> 2.3.5    (major)
            transformers     4.57.6       -> 5.14.1   (major)
            huggingface_hub  0.36.2       -> 1.26.0   (major)
            nvidia-cudnn-cu13 9.20.0.48   -> 9.19.0.56
            nvidia-nccl-cu13  2.29.7      -> 2.28.9
            protobuf         7.35.1       -> 6.33.6
            fastapi          0.137.1      -> 0.136.3
          numpy 1.x->2.x and transformers 4.x->5.x sit directly under the FROZEN
          services/ingester parser and FlagEmbedding/BGE-M3 (step 03/04), so this
          is not just a torch problem. Nothing in .venv needs to `import vllm`:
          vLLM is reached over HTTP on :8080 and ADR-0003 keeps the retrieval
          pipeline serving-engine agnostic, so a second venv costs only disk.
          Also for the record: resolved stable vLLM is 0.26.0, not the v0.22.x
          BUILD_STATE anticipates (">=0.22.0" is still satisfied, so PR #40082 is
          in), and flashinfer-python 0.6.14 IS in the resolved set, so the b12x
          native-FP4 target looks reachable. Step 01 remains DONE and intact — no
          package was installed; --dry-run only.
