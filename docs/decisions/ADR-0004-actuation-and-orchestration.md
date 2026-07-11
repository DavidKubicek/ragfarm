# ADR-0004 — Infra actuation via bounded MCP tools; composite-tool orchestration

Status: ACCEPTED
Date: 2026-07-09  (amended + accepted 2026-07-11)
Amendment (2026-07-11): §2's "modelled on host-control" confirmation gate is
generalised into an explicit, mandatory pattern — new §4, "Confirm-gated actuation
wrapper" — covering ALL mutating/actuating MCP tools, not just reboot. Naming for
the wrapper files, MCP mounts and ports is factored out into ADR-0005 so this ADR
stays about *actuation semantics* and ADR-0005 owns *layout conventions*.
Builds on: ADR-0003 (Open WebUI + mcpo agent layer; the swappable-endpoint rule and
the MCP servers as the durable tool layer). This ADR governs a new capability class
— *actuation* (doing things on infra) — and how multi-step work is orchestrated.

## Context
The client wants ragfarm to act on infrastructure, not just retrieve — starting with
diagnostics ("test if VM1 can reach sftp.domain.com:22") and progressing to
"intelligent" multi-step, possibly templated, workflows. This crosses a hard trust
boundary: the thing choosing actions is a 7B LLM parsing natural language that
includes *retrieved corpus text* (a prompt-injection surface).

Measured reliability of Qwen2.5-7B tool use on the live PoC (see build log 07 and
the reliability harness):
- **Tool selection + argument extraction: reliable** (right tool, right params;
  single-call rate 5/5; correctly chains a prior tool's output into the next call).
- **Control flow is the weak gate**: without an explicit ordering hint the model
  sometimes reorders steps (ran a port check before DNS and mis-attributed the
  cause). Ordering/conditional/branching logic is *not* reliable on a 7B.
- **Determinism** (temp=0/top_k=1/seed) gives reproducibility for a *fixed prompt*,
  not consistent behaviour across *phrasings* of the same intent. It buys
  testability, not correctness.

## Decision

### 1. Actuation is exposed only as bounded MCP tools — never raw execution
- **Typed params; the server builds the command.** The LLM supplies structured
  fields (`source_vm`, `target_host`, `port`), never a command string. Params are
  validated (hostname/IP regex, port range) and assembled into `argv` without a
  shell, so corpus/user text cannot inject commands.
- **Least-privilege exec principal.** SSH from the agent host to VMs uses a
  dedicated low-priv account with `command=`-forced keys (the key can only run the
  sanctioned probe wrapper), per-VM allowlist, and hard timeouts.
- **Read/mutate separation** (as ADR-0003/host-control): read-only diagnostics in
  their own MCP server; anything mutating stays dry-run-default + allowlist +
  explicit `confirm`, modelled on `services/mcp-host-control/server.py`.
- **Audit** every actuation call (requester, resolved params, command, result).
- Prompt-injection blast radius is bounded by the above (worst case: a logged,
  timeout-bounded probe to an odd allowlisted target).

### 2. Orchestration: composite tools first, OWUI's loop for chaining, playbooks later
The "reliability ladder" — climb only as far as the workload needs:
1. **Bounded primitives + Open WebUI's existing tool loop.** No new orchestrator;
   OWUI already runs multi-step tool loops (capped by
   `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS`). Suitable for *independent* checks +
   summary; inherits the control-flow variance above.
2. **Composite tools own fixed sequences (the default).** Where the sequence is
   deterministic, expose ONE richer tool that does it server-side and returns a
   structured verdict (e.g. `check_reachability` = DNS → port → verdict) instead of
   making the model order fiddly primitives. This removes the measured failure mode
   and leaves the model only genuine decisions. Strictly more reliable, zero
   frameworks. **This is the chosen default for multi-step "guided" workflows.**
3. **Templated playbook runtime (deferred).** For genuinely stateful / branching /
   scheduled automation, a deterministic playbook engine (LangGraph) with the LLM
   only at the edges (intent classification + param extraction in, summary out).
   Not built now; revisit when such workflows are real — likely after the prod
   NVIDIA HW swap, where a larger model raises the multi-step ceiling.

`search_corpus` remains the retrieval tool (ADR-0003 Option B) regardless — no
framework is allowed to take over retrieval.

### 3. Reliability is gated, not assumed
Deterministic sampling (`temperature:0`, `top_k:1`, `seed`) is set in the model
preset params. Before any new actuation tool-set is trusted, it must pass the
**chaining reliability harness** (`infra/openwebui/check_toolchain.py chaining`):
per-intent phrasings run through the tool loop, asserting required tools called,
args valid, no forbidden ordering, and grounded final answers.

### 4. Confirm-gated actuation wrapper (the mandatory pattern for ALL mutations)
Every tool that mutates or actuates infrastructure is reached ONLY through an Open
WebUI native Python **wrapper** that owns a blocking human-confirmation modal. The
wrapper — not the model, not a prompt convention — is the gate. `reboot_guarded.py`
is the reference implementation; this section makes its shape the rule.

**The two-phase contract (server side).** A mutating MCP tool MUST accept a
`confirm: bool = False` argument and be two-phase:
- `confirm=False` (default) → compute and return a **PLAN** (structured: what would
  change, in what order, over what targets). Act on nothing.
- `confirm=True` → perform the action. Still enforce the server-side gate underneath
  (dry-run-default is moot here, but the allowlist + typed-param validation of §1
  always apply). The server never trusts that a human approved — it only knows a
  caller passed `confirm=True`; the *human* gate lives one layer up, in the wrapper.

**The wrapper (client side, Open WebUI Python Tool).** For each mutating MCP the
model-facing surface is a single wrapper tool that:
1. calls the MCP with `confirm=False` through mcpo and grabs the returned PLAN,
2. renders that PLAN into a blocking `__event_call__` confirmation modal
   (`type: "confirmation"`), and refuses to act if `__event_call__ is None`
   (i.e. a non-interactive/headless session — no silent execution),
3. only on human approval calls the MCP again with `confirm=True`, and returns the
   MCP's result string as the tool result.

**The non-registration rule (this is what makes the gate un-bypassable).** A
mutating MCP is bridged by mcpo but is **NOT registered as an Open WebUI tool
server**. The model therefore has no direct route to `confirm=True`; its only path
to the mutation is the wrapper, which cannot proceed past the modal. Read-only tools
(`search_corpus`, `where_is_vm`, `list_vms_on_host`, fs reads) ARE registered as
tool servers and need no wrapper. So the confirmation is a property of the
*architecture*, not a behaviour we hope a 7B exhibits — which is the point, given
the control-flow variance measured above. Prompt-injected corpus text cannot talk
the model into an unconfirmed mutation because the model has no unconfirmed path.

**Why this is the simplification, not extra machinery.** The "report the plan back,
wait, then execute" handshake is multi-step control flow — exactly the 7B's weak
gate (§Context). Pushing it into deterministic Python removes it from the model's
job: the model makes ONE call (`reboot_host(host, reason)`); the wrapper does the
dry-run→modal→execute dance. Fewer model decisions, a hard gate, and one obvious
audit choke point (§1) — all at once.

**Boundaries (do NOT over-apply).**
- Wrappers are for MUTATING/irreversible tools only. Do not wrap read-only tools:
  it adds an Open-WebUI-specific, non-portable layer and loses mcpo's clean
  auto-registration for zero benefit.
- Wrappers hold **no credentials** and contain **no infra logic** — they only POST
  to mcpo and raise the modal. All privilege (SSH exec principal, `one.vm.migrate`,
  ONE auth) stays behind the MCP server's container boundary (an isolation asset
  under NIS2). "Declare the tool client-side" means *own the confirmation UX client
  side*, never *move execution client side*.
- Composite mutating workflows (§2 rung 2) get ONE wrapper showing ONE aggregate
  plan and ONE modal for the whole sequence, then drive the server-side steps. This
  pairs the composite tool's determinism (ordering off the 7B) with a single human
  gate.

**Canonical wrapper template** (fill the four caps-marked fields; naming per
ADR-0005 §wrappers):

```python
"""
title: <HUMAN TITLE>
description: <ONE LINE> — gated by a human confirmation modal.
author: ragfarm
version: 0.1.0
"""
# Open WebUI native Python Tool — confirm-gated actuation wrapper (ADR-0004 §4).
# The target MCP (<MOUNT>) is bridged by mcpo but NOT registered as an OWUI tool
# server, so the model can only reach the mutation through this wrapper.
import requests
MCPO = "http://127.0.0.1:8000"          # single mcpo endpoint (ADR-0005 §ports)

class Tools:
    def __init__(self):
        pass

    async def <ACTION>(self, <PARAMS>, reason: str = "", __event_call__=None) -> str:
        """<DOCSTRING the model reads to route here.>"""
        # 1. dry-run -> PLAN (no action)
        try:
            plan = requests.post(f"{MCPO}/<MOUNT>/<TOOL>",
                                 json={<PARAMS_AS_JSON>, "confirm": False},
                                 timeout=30).json()
        except Exception as e:
            return f"Could not reach <MOUNT>: {e}"
        if not plan.get("ok"):
            return f"Cannot plan: {plan.get('reason')}"

        preview = render_preview(plan)   # human-readable plan text for the modal
        # 2. blocking human confirmation; refuse to act headlessly
        if __event_call__ is None:
            return "This action requires interactive confirmation."
        approved = await __event_call__({"type": "confirmation",
                    "data": {"title": "<CONFIRM TITLE>", "message": preview}})
        if not approved:
            return "Cancelled. No changes were made."

        # 3. execute; server-side allowlist/validation still gates this
        res = requests.post(f"{MCPO}/<MOUNT>/<TOOL>",
                            json={<PARAMS_AS_JSON>, "confirm": True},
                            timeout=60).json()
        return res.get("summary") or ("Done." if res.get("ok") else
                                      f"Did not proceed: {res.get('reason')}")
```

## Consequences
- New read-only diagnostics MCP server (e.g. `mcp-net-diag`) is added, modelled on
  the host-control safety pattern; it is a durable-layer citizen alongside
  `rag-retrieval` and the OpenNebula MCPs.
- Multi-step "intelligence" is delivered primarily by *tool design* (composite
  tools), not by trusting the 7B's control flow. This is a deliberate constraint:
  fewer, richer, unambiguous tools beat many fine-grained ones the model must order.
- No LangChain/LlamaIndex adopted now; LangGraph is the named candidate *if/when*
  rung 3 is needed. The MCP servers stay the swappable-runtime-agnostic tool layer.
- OpenNebula-backed actuation (host-control real drain/reboot) stays BLOCKED until
  live access exists; this ADR does not unblock it.
- **Every mutating tool now ships with a `*_guarded` wrapper** (ADR-0005 §wrappers)
  and is deliberately absent from the OWUI tool-server registration in
  `infra/openwebui/setup_openwebui.py`. `reboot_guarded.py` is the reference; the
  next mutating tool (e.g. a live-migrate or a config-push) copies the template in
  §4 rather than inventing a new confirmation path.
- **Read-only tools are never wrapped** — they stay plain mcpo→OWUI tool servers.
  The registration list in `setup_openwebui.py` is thus the human-readable manifest
  of "what the model can call directly"; anything mutating is conspicuously *not*
  on it. Keep it that way — the absence is a security property, not an oversight.
- The wrapper is the audit choke point required by §1: log (requester, resolved
  params, approved bool, result) there, closest to the human decision.

## Alternatives considered
- **Let the LLM chain fine-grained primitives as the default** (rung 1 everywhere):
  rejected as the *default* — measured control-flow variance makes ordered/
  conditional flows unreliable on a 7B. Kept as an option for genuinely independent
  checks.
- **Adopt LangChain/LlamaIndex now**: rejected. Rungs 1–2 need no framework;
  LlamaIndex would fight the deliberately-engineered hybrid `search_corpus`.
- **Free-form remote shell (LLM writes commands)**: rejected outright — prompt
  injection + hallucination translate directly into commands on infra. Any future
  need goes behind a dry-run→human-approve gate like host-control.
- **Collapse the MCP servers into one Open WebUI Python script** (raised: "if the
  confirm wrapper already lives in OWUI, why keep the MCP layer at all — one script
  could do what the MCP does"): rejected. It conflates *owning the confirmation UX*
  (correctly client-side) with *owning execution* (must stay server-side). Doing so
  would (a) forfeit ADR-0003's durable-layer thesis — the MCP servers survive a UI
  swap and the NVIDIA HW swap; OWUI Python does not — (b) put credentials and
  privileged calls (`one.vm.migrate`, the forced-command SSH principal) inside the
  OWUI process, collapsing the container isolation boundary that is a NIS2 asset,
  and (c) lose reuse by any future headless/CI consumer (ADR-0003 kept that door
  open). The single-endpoint/collocation goal that motivated the idea is met
  instead by mcpo (one endpoint at :8000, every MCP mounted under /<mount>) plus the
  layout conventions in ADR-0005 — not by deleting the tool layer.
