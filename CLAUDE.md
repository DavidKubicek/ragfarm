# CLAUDE.md — project contract (auto-loaded every session)

This file is the stable contract for the `ragfarm/` build. It is loaded into
context at the start of every Claude Code session in this directory. It is
**read-mostly**: do not edit it, the ADRs, or any `docs/decisions/*` file as part
of executing a build step. If the plan itself is wrong, raise it through the
blocker channel (Chapter 2) and wait.

Progress state does NOT live in this file or in conversation history (sessions
are siloed across CLI / Remote Control / IDE). It lives in `BUILD_STATE.md`
(linear build progress) and `PROGRESS.md` (human-gated blockers) and in the detailed
logs of stdout+stderr from previous executions in `logs/<NN-stepname>.log`. Chapter 2
defines exactly how to use the first two.

---

## Chapter 1 — Architecture, constraints, build-order definition

You are continuing a project scaffolded in a planning session. Read `README.md`
and `docs/decisions/ADR-0013` first — it is the live engine-split decision and it
**supersedes ADR-0001**. Read ADR-0003 (agent layer) and ADR-0006 (manifest) next.
Do not redesign the engine split without updating ADR-0013.

> **ADR-0001 and everything about the AMD Ryzen box (NPU, iGPU, Vulkan, Quark,
> ROCm, `docs/ryzenai/`) is HISTORICAL.** It is kept for provenance. Do not act on
> it, do not "restore" it, and do not treat its numbers as current.

### The single most important constraint
Target is **Linux on an NVIDIA DGX Spark (GB10 Grace Blackwell)**, compute
capability **sm_121**, 128 GB unified memory, ~221–273 GB/s memory bandwidth
(**measure it** — model sizing keys off this number). The engine split is:

- **CUDA / vLLM** runs the generative LLM.
- **CUDA / llama.cpp `--reranking`** runs the cross-encoder reranker, at *lowered
  scheduling priority* so the interactive LLM wins contention.
- **CUDA** runs the BGE-M3 embedder (bursty: heavy at ingest, trivial per query).
- **CPU** runs Qdrant + the MCP services.

**Decode is memory-bandwidth-bound.** Choose models by *active* parameters, not
total — which is why the target is an MoE and never a dense 70B. All three GPU
consumers share one memory pipe; there is no separate VRAM to hide in. Argue every
"just add another model" against that budget.

**The sm_121 FP4 trap — read this before debugging any garbage output.** GB10 is
sm_121, *not* sm_120. FlashInfer-TRTLLM's FP4 MoE kernels gate on SM100+ and
reject 12.x; the CUTLASS FP4 GEMM fallback fails on sm_120/121. The working MoE
path is **Marlin W4A16**, so **`--moe-backend marlin` is mandatory**. Omit it and
the model loads cleanly, then emits streams of `!!!!!` — a silent numerical
failure, not a crash. Check this **first** when output is garbage. Full reasoning
in ADR-0013.

### Models and how to engage them
- **Generative LLM:** `Qwen3-VL-30B-A3B-Instruct`, **NVFP4** safetensors, served by
  **vLLM ≥ 0.19.0** on `127.0.0.1:8080`, OpenAI-compatible, with
  `--enable-auto-tool-choice --tool-call-parser hermes` for tool calling and
  `--moe-backend marlin`. Cap context with `--max-model-len`. This is the model the
  agent/gateway drives, and the one all tuning happens on.
- **Reranker:** `bge-reranker-v2-m3` at full quality on a llama.cpp CUDA
  `llama-server --reranking`, `127.0.0.1:8081/reranking` (ADR-0008). Never shrink
  it to buy bandwidth — that trades retrieval precision for latency, the wrong way
  round for this system.
- **Embedding model:** **BGE-M3** on CUDA, HTTP `/embed` on `:8090`, dense+sparse
  (ADR-0002 — which *rejected* the NPU/Quark path; the filename misleads). Record
  exact model+revision in `models/embeddings/MODEL.md`. Its output vectors must
  match what is already in Qdrant, or the collection needs a re-ingest.
- **Quantization:** prefer **NVFP4**; FP8 is the fallback if an NVFP4 checkpoint
  misbehaves. GGUF/Q4_K_M is the *old* box's format — not the target here.

### Build-order definition (authoritative sequence)
The order below is fixed and matches ADR-0013. Per-step commands and gate-checks
live in `BUILD_STATE.md`; this is the canonical list of *what* the steps are and
*why they are ordered this way*.

1. **venv-cuda13** — build the Python environment against CUDA 13
   (`--profile cu13`). Everything downstream imports from it, so it is first.
2. **vllm-serving** — install/serve vLLM with the NVFP4 model; the agent layer
   depends on this endpoint.
3. **embedder-service** — BGE-M3 on CUDA behind `:8090/embed`; ingestion and
   retrieval depend on it.
4. **qdrant-ingester** — bring up Qdrant and ingest the corpus; retrieval depends
   on a populated collection.
5. **mcp-placement** — already written and unit-tested; it is the **reference
   implementation**. Wire real OpenNebula creds and verify against the live
   cluster. Model the other MCPs on it.
6. **mcp-fs-host-control** — fs-agent and host-control stubs. host-control is
   SAFETY-GATED (dry-run default, allowlist, confirm flag) — keep it that way;
   implement drain-then-reboot via OpenNebula before enabling real actions.
7. **agent-wiring** — Open WebUI + mcpo over the OpenAI-compatible endpoint, MCP
   client → the HTTP MCP servers, tools exposed to the model, including the
   `rag-retrieval` MCP that queries Qdrant (`search_corpus`).

### Salvaged context from the originating planning session
- The live engine-split decision and the sm_121 FP4 analysis live in ADR-0013;
  `docs/hardware/NVIDIA-Spark.md` is the hardware record.
- OpenNebula is the placement owner (XML-RPC `one.vm.info` / `one.vmpool.info`);
  the placement MCP is built around that, **not** libvirt.
- The retrieval pipeline is deliberately **serving-engine agnostic** (ADR-0003).
  That is why swapping llama.cpp → vLLM touches the serving plane and almost
  nothing else. Keep it that way.

### Open questions for Dave (not blockers for steps 1–4)
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

The repo is shared between this agent and Dave's IDE, and is the sync channel
between them. To keep that safe without branches or merges, two more mechanisms
apply, both defined inline in the flow below:
- `/tmp/ragfarm.lock` — a local-only presence heartbeat (NOT in the repo, never
  committed). It holds a Unix epoch timestamp while an agent is active, or the
  literal word `IDLE` on clean exit. Dave checks it on the target before editing,
  to confirm no agent is live. You refresh it as you work.
- Git sync on `main` — single branch, no feature branches, no merges. You commit
  and push your own changes, and `pull --rebase` before every push so you never
  clobber a change Dave pushed while idle.

All later instructions — `git pull --rebase`, `docker compose -f infra/...`,
`python services/ingester/...`, reading/writing BUILD_STATE.md and PROGRESS.md,
the `logs/` paths — assume the current directory is `~dave/ragfarm`. If you are not
in the repo directory, `cd` there first. The `/tmp/ragfarm.lock` path is absolute and
unaffected.

### Working directory (applies to every command in this file)
The repo lives at `~dave/ragfarm` (the home dir also contains other files; the
repo is the `ragfarm/` subdirectory, NOT $HOME itself). Every command, path, and
git operation in this contract is relative to the repo root. Before doing anything
else in a session:

### On session start
0. **Refuse to start if another agent is live.** Before taking the heartbeat,
   inspect `/tmp/ragfarm.lock`:
   - If it contains the literal `IDLE`, or the file is missing → proceed.
   - If it contains a timestamp, compute its age:
     `age=$(( $(date +%s) - $(cat /tmp/ragfarm.lock) ))`
     - age > 300 (stale; previous agent died without clean exit) → proceed, and
       note in your first reply that you reclaimed a stale lock of `<age>`s.
     - age ≤ 300 (another agent is active) → **STOP IMMEDIATELY.** Do not take
       the heartbeat, do not pull, do not read state, do not commit, do not touch
       the repo at all. Reply exactly: "Agent already active (lock age <age>s) —
       refusing to start to avoid a concurrent writer. Stop the other session or
       wait for it to go IDLE." Then end the session.
   Only once past this check do you continue to step 1.
1. Take the presence heartbeat:
   `date +%s > /tmp/ragfarm.lock`
   Refresh it (same command) at the start of every step, so the timestamp stays
   current while you work. Dave treats a timestamp older than ~5 minutes, or the
   value `IDLE`, as "no agent active, safe to edit."
2. Integrate anything Dave pushed while you were off:
   `git pull --rebase origin main`
   If this reports a CONFLICT, do NOT resolve it: `git rebase --abort`, append a
   `BLOCKED:` entry to `PROGRESS.md` naming the conflicted file(s), and stop until
   Dave clears it on the laptop.
3. Read `BUILD_STATE.md`. Identify the first step whose status is not `DONE`.
4. Read `PROGRESS.md`. If any entry for a step is still `BLOCKED:`, that step is
   not eligible to run — skip it and take the next non-`DONE`, non-`BLOCKED:` step
   in order. If an entry is now `UNBLOCKED:`, that step is eligible again: re-run
   its gate-check and proceed with it, if gate-check fails.
5. Resume from the first step whose status is none of `DONE` / `SKIP` / `BLOCKED:`.
   A `SKIP` step is ignored: never run it, re-run it, or gate-check it, until it is
   changed to `PENDING`. Do not re-run `DONE` steps unless asked to, or unless its
   gate-check now fails. 
6. Do NOT skip the planned order for any reason other than an active `SKIP` or `BLOCKED:`.

### For each step you execute
1. Refresh the heartbeat: `date +%s > /tmp/ragfarm.lock`.
2. Run the step's commands exactly as defined in `BUILD_STATE.md`.
3. Append all of stdout+stderr to a log file `logs/<NN-stepname>.log` (create if missing)
   Do NOT paste raw output into BUILD_STATE.md or your reply.
4. Run the step's **Gate** (defined in that step's row in BUILD_STATE.md).
   - Gate passes → set status `DONE`.
   - Gate fails → set status `FAILED`.
5. **On `DONE` only: write the step's deploy fragment into `scripts/deploy.sh`.**
   See "The deploy.sh fragment contract" below. This is not optional bookkeeping —
   it is the deliverable that decouples deployment from the AI.
6. Update that step's status line in `BUILD_STATE.md`: status, UTC timestamp,
   log path, and a short summary (<120 chars). Keep the file small — summary
   references what's in the log; do not reproduce it elsewhere.
7. Commit and push the result on `main` (never a feature branch):
   ```
   git add -A          # logs/ and .env are gitignored; never force them in
   git commit -m "Step <NN-name>: <DONE|FAILED|BLOCKED> — <≤60 char summary>"
   git pull --rebase origin main
   ```
   - Rebase clean → `git push origin main`.
   - Rebase CONFLICT → `git rebase --abort`; append a `BLOCKED:` entry to
     `PROGRESS.md` naming the conflicted file(s); set the step `BLOCKED`; STOP.
     Never resolve a conflict yourself — Dave does that on the laptop.
   Never `git push --force`. Never commit `logs/` or `.env`.

### The deploy.sh fragment contract

**Why this exists.** The build steps are executed once, by an agent, with a human
watching. Deployment must be repeatable forever, by a human, with no agent. As you
execute a step you are the only party that knows the *exact* command sequence that
actually worked — including the corrections you made after a failure. That
knowledge has to land in `scripts/deploy.sh` while you still have it, or it is
lost. `deploy.sh` is therefore not written up-front; it **accretes**, one verified
fragment per completed step, and it is the real output of the build.

**The two jobs deploy.sh must do, which is why fragments carry guards:**
- **Bare-metal reproduction** — on a machine with nothing (no `.venv`, no models,
  no units), `scripts/deploy.sh --fresh` must build the entire system.
- **Code-release deploy** — on a working machine, `scripts/deploy.sh` must activate
  new code and **not** touch `.venv` or re-download models unless the update
  genuinely requires it.

Both are satisfied by the same file because every fragment is individually
idempotent: it checks whether its work is already done and skips if so.

**Fragment format.** Each step owns exactly one marked region in `scripts/deploy.sh`:

```bash
# >>> deploy-step-NN-stepname >>>
# <one line: what this does and why it is here>
if <cheap check that the work is already done> && [ "${FORCE_ALL:-0}" != 1 ]; then
    info "step NN: already satisfied, skipping"
else
    <the exact commands that worked, in order>
fi
# <<< deploy-step-NN-stepname <<<
```

Rules, all of them load-bearing:
- **Guard every fragment.** The check must be cheap and honest (`[ -d .venv ]` is
  weak; `.venv/bin/python -c 'import torch'` is real). `--fresh` sets `FORCE_ALL=1`
  so every guard falls through to the work.
- **Commands only, no diagnostics.** Gate probes, `curl` checks, and exploratory
  commands stay in the step log. The fragment is what *builds*, not what *verifies*.
- **Verbatim what worked.** If you had to correct a command after a failure, the
  fragment gets the corrected form — never the first attempt, never an idealized
  version you did not run.
- **Fragments are ordered by NN** and must remain so; `deploy.sh` executing top to
  bottom is exactly the build order.
- **Never delete another step's fragment.** You own exactly the region matching the
  step you just completed.
- **Safety check before every fragment edit:**
  `grep -c '^# >>> deploy-step-NN' scripts/deploy.sh`
  - `1` → replace that region in place.
  - `0` → append a new region in NN order.
  - `>1` → **STOP.** Duplicate markers mean a previous write went wrong. Append a
    `BLOCKED:` entry to `PROGRESS.md` and wait for Dave. Do not guess which to keep.
- **Re-running a step replaces its fragment**, exactly as it replaces its status
  line. Never append a second copy.
- Secrets never go in a fragment. Read them from `.env`, which is gitignored.

### On FAILED (agent can act; needs Dave's confirm to retry)
A `FAILED` step is one you ran but whose gate did not pass, and which you can
diagnose yourself.
- Stop. Read the relevant tail of `logs/<NN-stepname>.log` and summarize the
  probable cause in ≤5 lines.
- Propose the fix and **WAIT for Dave's explicit confirmation before retrying.**
  Never loop unattended on a failing step.
- After Dave confirms, retry the **same** step with the fix. Re-running replaces
  that step's status line and appends (never truncates) its log.

### On BLOCKED (only Dave can clear; hand off and move on)
A step is `BLOCKED`, not `FAILED`, when you **cannot proceed without Dave**:
an account-gated file is missing, OpenNebula creds/reachability are absent, a
BIOS/EC toggle is needed, or anything else only Dave can supply. When you hit
such an obstacle:
1. Append a `BLOCKED:` entry to `PROGRESS.md` (format below) stating exactly what
   is needed and the exact command/file path/credential involved.
2. Set that step's status in `BUILD_STATE.md` to `BLOCKED` with the same UTC
   timestamp, so the table and the ledger agree.
3. Commit and push so Dave sees the blocker from the laptop without SSH:
   `git add -A && git commit -m "step <NN-name>: BLOCKED — <reason>" && git pull --rebase origin main && git push origin main`.
4. Continue with the next eligible (non-`DONE`, non-`BLOCKED`, non-`SKIP`) step in order.
5. Do NOT fake, mock, or work around a hard blocker in committed code. Clearly
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

### On session end (clean exit)
1. Release the heartbeat so Dave can edit freely:
   `echo IDLE > /tmp/ragfarm.lock`
2. Make sure all work is pushed:
   `git pull --rebase origin main && git push origin main`
If the session dies without reaching this (crash, network drop), the heartbeat is
left holding a stale timestamp — Dave's ~5-minute staleness check is what covers
that case, so a missed clean exit is safe, just less tidy.

### For Dave — checking before you edit (run on the target)
Before editing the repo from the laptop/IDE, confirm no agent is live:
```
cat /tmp/ragfarm.lock
```

### Hard rules
- Never edit `CLAUDE.md`, the ADRs, or `docs/decisions/*` during build execution.
- Never put raw build output anywhere except `logs/`.
- Every step that reaches `DONE` MUST leave a guarded fragment in `scripts/deploy.sh`
  (see "The deploy.sh fragment contract"). A `DONE` step with no fragment is an
  incomplete step. Never hand-write a fragment for a step you did not actually run.
- Never skip a step except for an active `BLOCKED:` or `SKIP` state.
- Never `git push --force`; never commit `logs/` or `.env`.
- Never resolve a git conflict yourself — abort and `BLOCKED:` it for Dave.
- All work lands on `main`; `git pull --rebase` before every push.
- All timestamps are UTC (the `/tmp/ragfarm.lock` epoch is UTC by definition).
- Do NOT spawn subagents (Task tool) or agent teams for build steps. The build
  is strictly sequential with dependency-gated steps; parallel workers would
  create concurrent committers that the /tmp/ragfarm.lock guard cannot see.
  Execute every step yourself in this single session.
- Run as user `dave`, never root. Use `sudo` ONLY for the specific privileged
  actions a step requires (package installs, driver load in step 01, Vulkan dev
  packages in step 02). Never `sudo` an entire script or a git/python/llama-server
  command. Never run the session itself under root. If a step seems to need broad
  root access beyond package/driver install, treat that as a BLOCKED and ask Dave.
