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
