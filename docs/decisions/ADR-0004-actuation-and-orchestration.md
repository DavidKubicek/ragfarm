# ADR-0004 — Infra actuation via bounded MCP tools; composite-tool orchestration

Status: PROPOSED  (drafted for Dave's review; acceptance is the owner's call per
CLAUDE.md — do not treat as binding until flipped to ACCEPTED)
Date: 2026-07-09
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
