# ADR-0011 — Workbook-driven agent execution: the runbook is the authority, phased rollout
Author: David Kubicek (david.kubicek@eywo.cz)

Status: PROPOSED (2026-07-30). The **workbook model** — human-authored runbooks as
the core, atomic, non-chunked unit that both *selects* and *bounds* agent behaviour —
is the accepted leading principle and the reason this ADR exists. The **execution
mechanism** rolls out in two phases: **Phase 1** (a deliberately unsafe, lab-only
bootstrap — a plain Python agent listening as root, driven from OWUI, no safeguards)
proves the workbook → plan → execute → verify loop end-to-end; **Phase 2** hardens it
to a privilege-separated, signed-instruction executor before *any* exposure beyond a
throwaway lab box. Phase 1 is explicitly **never** run on production, never
internet-exposed, and time-boxed to the loop-proving exercise. Promote to ACCEPTED
once Phase 1 validates the loop and Phase 2's threat model is signed off.
Date: 2026-07-30
Builds on: ADR-0003 (Open WebUI + mcpo agent layer — the planner front-end lives
here), ADR-0004 (**infra actuation via bounded MCP tools; typed params, server builds
argv, least-privilege exec, dry-run+confirm, audit** — Phase 2 restores every one of
these guarantees; Phase 1 deliberately suspends them in the lab only), ADR-0005
(naming/layout conventions), ADR-0006 (content-addressed corpus sync — workbooks ride
the same manifest as any other corpus file).
Scope: how procedural knowledge is authored (`workbook.md`), ingested (whole, never
chunked; skill-manifest selection), and executed by an agent under a planner. Governs
a **new execution path** (an agent running *on the target host*, taking instructions
from a planner) distinct from ADR-0004's SSH-from-agent-host MCP probes. Does **not**
change retrieval (ADR-0007/0008/0010) or the corpus table/prose parsers.

## Context

ADR-0004 established the reliability ladder for actuation and ended at "playbooks
later". This ADR is that step. Two things force a first-class *procedural* unit:

1. **A 7B (and even a 30B) plans control-flow unreliably.** ADR-0004 measured it:
   tool selection and argument extraction are reliable; **ordering / conditional /
   branching logic is not**. The model must not be trusted to *invent* the sequence
   of a backup or a failover. The sequence has to come from somewhere deterministic.
2. **Operators already think in runbooks.** A backup, a PITR restore, a cluster
   migration — each is a known, human-authored procedure: these commands, in this
   order, check these logs for these patterns, roll back like this on failure. That
   artifact is the missing unit. Call it a **workbook**.

The workbook is where the intelligence *of the procedure* lives, so the agent doesn't
have to be intelligent. Combined with the model (on the box) and the planner (the
graph), it means the agent code itself can be a thin, boring, auditable executor —
which for production infra is the reliability property we want, not a compromise.

## Decision

### 1. The workbook: format and authoring

Path convention under the corpus (content-addressed like any file, ADR-0006):

```
corpus/workbooks/<project>/<process>[/<host>[/<app>]]/workbook.md
```

Structure (the parts that make it machine-executable rather than an essay):

- **`# <Canonical Task Name>`** — the H1 is the stable task identity / selection key.
- **`## Purpose` with USE / USE-NOT.** Positive triggers *and* explicit anti-scope
  ("do NOT use this for …"). The negative conditions are what make selection reliable
  — without them the planner picks plausible-but-wrong procedures. This block is the
  selection signal.
- **`## Preconditions`** — what must be true before step 1.
- **`## Steps`** — each step is *command + generic description + expected success
  signal*. The success signal is non-negotiable: it is how the agent knows a step
  worked before advancing.
- **`## Verification`** — log locations and the specific patterns that mean success
  vs failure ("grep `BACKUP COMPLETED` in `/var/log/…`").
- **`## Failure / Rollback`** — what to do when a step's success signal does not fire.
  This is what separates a workbook from a blog post and what makes it safe on prod.

### 2. Ingestion: whole, never chunked — a skill manifest

- **Atomic retrieval unit.** A workbook is retrieved whole or not at all. Procedures
  chopped into chunks are dangerous — an agent handed step 3 without step 2's
  precondition is a failure waiting to happen. `ingester.py` routes
  `workbooks/**/workbook.md` to a **whole-document** path (no sentence/section
  splitting), bypassing the prose chunker of ADR-0007.
- **Selection, not chunk-RAG.** Only the **H1 + Purpose** block is embedded, into a
  small manifest the planner queries to *select* a workbook by task intent. The full
  body is loaded into the executing context only once selected. This is progressive
  disclosure — the same manifest-then-load pattern OWUI itself adopted for skills, and
  it keeps N full procedures out of context (directly helping the ADR-0010 context
  story).
- **Rides the manifest.** Content-addressed, alias-switched, watched — no special
  sync path; a workbook is just a corpus file with a dedicated route.

### 3. The workbook is the command authority (keeps ADR-0004's guarantee)

This is the through-line that keeps the whole design consistent with ADR-0004 §1
("the LLM supplies structured fields, never a command string"):

- **Commands are human-authored, in the workbook.** The planner does **not** invent
  commands. It *selects* the workbook and *concretizes typed parameters* (hostnames,
  paths, dates) into the command templates the workbook already declares.
- **The human approves the concrete command — and the UID it runs as — before
  execution.** Approval binds to *this exact* `{uid, argv}`, not a category.
- The agent executes only commands that match a step the selected workbook declares,
  concretized. It never executes free-form model output. The model fills blanks; it
  cannot introduce a command the workbook doesn't contain.

### 4. Execution architecture and the UID model

- **Planner on the box (Spark).** A LangGraph planner runs on the inference box:
  selects the workbook from the manifest, concretizes params, drives the agent step
  by step, holds overall state, and owns the human-approval interrupt. LangGraph earns
  its place *here* (durable, checkpointed, interrupt-for-approval) — one install, one
  place. It is **not** shipped to every agent.
- **Thin agent on the target host.** Given `{workbook, target, params}` it runs the
  step loop: concretize → (approval gate) → execute → check success signal → log →
  next. Its dependencies are minimal (see Phase 2: C + libc + openssl).
- **Per-instruction UID, chosen by the planner, approved by the human.** The planner
  specifies which user each command/section runs as; the human approves the UID
  transition as part of the command approval. This is *why* the executor needs
  `EUID=0` — so it can `setuid()` to whichever target user each instruction names,
  successively. This is the crond model (root-resident, executes as arbitrary users),
  **not** the sshd model (drop to one user for good) — the sshd shape can't retain the
  ability to switch users per instruction.
- **Rejected: sudoers allowlist.** A dumb account with per-command `sudo` entries was
  considered and rejected: enumerating every possible command across the infra is a
  maintenance nightmare that fails *open* (a forgotten entry or an over-broad wildcard
  becomes an unaudited hole). The signed-instruction + root-executor model moves the
  authority into a single verified payload instead of sprawling sudoers files.

### 5. Phased rollout

**Phase 1 — lab bootstrap, deliberately unsafe (prove the loop).**
- A plain Python agent listening on a port, running as root, taking instructions from
  the OWUI-driven planner. **No signing, no privsep, no approval hardening** — CLI/OWUI
  review is the only gate.
- Purpose: validate the full workbook → select → concretize → execute → check-signal →
  report loop against real procedures, before investing in the security machinery.
- **Hard boundaries (non-negotiable):** lab/throwaway host **only**; **never**
  production; **never** internet- or LAN-exposed beyond the operator's own machine;
  bound to `127.0.0.1`; time-boxed to the loop-proving exercise and torn down after.
  Phase 1 is a scaffold, not a product. It exists because proving the control loop is
  worthless if it's entangled with half-built crypto, and dangerous if mistaken for
  the real thing — so it is walled off explicitly.

**Phase 2 — privilege-separated, signed-instruction executor (before any real use).**
The OpenSSH/crond-shaped split that quarantines the CVE-dense code away from root:
- **Unprivileged network client** — does all transport (server-auth TLS + bearer
  token to the planner), runs as a dedicated unprivileged uid, holds no privilege.
  This is where openssl lives; a TLS-stack vuln here lands the attacker as *nobody*.
- **Privileged executor** — retains `EUID=0` across instructions, reads validated
  instructions from the unprivileged half over a **local unix socket only** (mode
  0600, `SO_PEERCRED` to confirm the peer uid), and does *only*: verify signature →
  `setuid` → `execve` → report. **It never links openssl and never touches the
  network.** Reachable only through the peer-cred'd socket — no SUID entry point on
  the filesystem (daemon preferred over SUID helper for exactly this reason).
- **End-to-end signed instructions.** The executor verifies an **ed25519** signature
  over `{uid, abs_path, argv, nonce, expiry, approver}` against a **pinned planner
  signing pubkey** (SPKI/raw-key pin, current+next for rotation — not a leaf cert that
  expires). Authenticity is end-to-end regardless of transport, so TLS is confidential-
  channel only, no longer load-bearing for integrity, and **mutual-TLS/client certs
  are dropped** — a bearer token authenticates the agent→planner pull; the *signature*
  authenticates the instruction's authority to run as a given UID. The root executor's
  entire crypto surface is one ed25519 verify — vendorable, no openssl, essentially no
  attacker-controlled parsing.
- **setuid hygiene (Linux specifics on top of known-good practice):** `setgroups`/
  `setgid` before `setuid`, `setres*`, **check every return value** (an unchecked
  `setuid` that fails keeps root — the canonical CVE), verify the drop took before
  exec; `execve` absolute path + explicit argv, **no shell** (`system`/`popen`
  forbidden — no metacharacters to inject); rebuilt-empty environment
  (`PATH`, clear `LD_*`/`LIBPATH`); close inherited fds; `prctl(PR_SET_NO_NEW_PRIVS)`
  so the child can never regain privilege; a seccomp-bpf whitelist on the executor's
  own syscalls.
- **Audit before privilege.** Append-only log of `{instruction, signature, approver,
  resolved uid, argv}` written **before** the exec, mirrored to **auditd** (kernel-
  level immutable trail that survives an app compromise) — the shape NIS2 assessors
  look for. Under systemd, wrap the daemon with `NoNewPrivileges=`,
  `CapabilityBoundingSet=`, `ProtectSystem=strict`, `RestrictAddressFamilies=AF_UNIX`
  as defence-in-depth around the executor's own checks.
- **Language:** C, libc + openssl, tiny footprint — and openssl is confined to the
  *unprivileged* half; the root executor is plain C + a vendored ed25519 verify.

## Consequences

Positive:
- Control-flow reliability comes from the human-authored workbook, not the model —
  directly fixing ADR-0004's measured weak gate.
- The agent stays dumb, small, and auditable; intelligence is in model + workbook +
  planner. Boring executor = production reliability.
- Phase 2 quarantines all attacker-controlled parsing (TLS) into an unprivileged
  process; the root TCB is "verify one signature, setuid, exec".
- Workbooks reuse the corpus manifest/sync wholesale; no new sync machinery.
- Consistent with ADR-0004: commands are authored + typed + approved, never invented.

Negative / cost:
- A privilege-separated C executor + signing infrastructure + key rotation is real
  engineering, deferred to Phase 2 — Phase 1 buys loop-proof at the price of a
  deliberately unsafe scaffold that must be disciplined about its boundaries.
- Workbook authoring is human effort; a procedure is only as safe as its declared
  success signals and rollback.
- A second execution path (on-host agent) alongside ADR-0004's SSH-MCP probes — two
  actuation surfaces to reason about.

Neutral / open:
- LangGraph on the box is one dependency in one place; acceptable per §4.

## Open questions

1. **Approval binding UX.** How the human co-signs `{uid, argv}` — an approval token
   the planner folds into the signed payload, or a co-signature — is a Phase-2 design
   detail; the requirement (approval binds to the exact command+UID) is fixed.
2. **Destructive-step policy.** Which steps demand approval every run vs once-per-
   plan, and which are hard-blocked without a second approver, belongs in the workbook
   schema (a per-step `destructive:`/`approver:` field) — specify during Phase 2.
3. **Workbook versioning in the audit trail.** The audit log records the workbook
   version/checksum (available free from the ADR-0006 manifest) so a run is always
   attributable to an exact procedure revision.
4. **Planner model choice.** Which model plans (the 120B-class MoE brain vs a 30B) is
   a box-sizing question tracked in `docs/hardware/NVIDIA-Spark.md`, not here.

## References
- ADR-0004 (bounded actuation; typed params; least-privilege; audit — Phase 2 is its
  continuation, "playbooks later" realised), ADR-0003 (OWUI/mcpo planner front-end),
  ADR-0006 (manifest/sync the workbooks ride).
- `services/mcp-host-control/server.py` — the dry-run-default + confirm gate pattern
  Phase 2's approval model generalises.
- `services/ingester/ingester.py` — add the `workbooks/**` whole-document route.
- `docs/hardware/NVIDIA-Spark.md` — planner/agent model sizing on the box.
