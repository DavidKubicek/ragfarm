# ADR-0005 — Naming, layout, port and collocation conventions
Author: David Kubicek (david.kubicek@eywo.cz)

Status: ACCEPTED  (conventions effective immediately; the one-time migration of
existing files to match is the checklist at the end, executed on David Kubicek's go-ahead)
Date: 2026-07-11
Builds on: ADR-0003 (durable layer = MCP servers behind mcpo + Open WebUI),
ADR-0004 (confirm-gated actuation wrappers).

## Context

The stack has grown to ~8 running components across three planes (host services,
containers, Open WebUI tools) with a matching spread of directories, compose
services, systemd units, mcpo mounts and ports. Left un-governed, each of those
acquired a slightly different name for the same thing — `mcp-infra-placement` the
directory vs `placement` the mcpo mount; `rag-retrieval` (no `mcp-`) next to
`mcp-host-control`; `llama-server.service` and `embedder-server.service` next to
`ragfarm-stack.service`. Nothing is broken, but the *map* is harder to read than
the system is complex, which is the failure mode this project explicitly wants to
avoid.

The operating principle, stated once so the rest of this ADR is just its mechanics:

> **Easy to see, easy to read.** A newcomer (or David Kubicek in six months) should be able
> to infer every component's directory, container, unit, port and endpoint from its
> one short name, and read the whole topology off a single table. Consistency is a
> feature; a clever exception is a bug.

Two shape decisions fall out of that and are worth stating explicitly because they
sound like they conflict but do not:

- **One aggregate endpoint.** Everything the agent calls goes through the single
  mcpo endpoint at `127.0.0.1:8000`, each MCP mounted at `/<mount>`. From Open
  WebUI's side there is exactly one tool endpoint to reason about.
- **Sequential ports behind it.** The raw MCP servers *also* each own a stable,
  sequential port in a documented block (`81xx`), so a human debugging one can
  `curl` it directly. Single-endpoint (for the agent) and sequential-ports (for the
  operator) are complementary, not competing — mcpo is the front, the `81xx` block
  is the back.

## Decision

### The canonical component registry

> **Live inventory moved (2026-07-20, ADR-0008).** This ADR remains authoritative
> for the *rules* — how every name/port/mount is derived from the short name (the
> sections below). The *current running inventory* (which now includes the
> reranker service — now GPU llama.cpp on :8081) is maintained in `docs/deployment.md`; treat that as
> the live registry and this table as the snapshot at this ADR's decision. The
> naming rules a new component must follow are still governed here.

This table is the source of truth for the naming derivation. Every other name is
derived from the **short name** column. It is mirrored (abbreviated) in
`services/mcp-gateway/README.md`.

| plane      | short name    | directory                    | compose service / container* | port  | mcpo mount     | agent-facing surface            | mutates |
|------------|---------------|------------------------------|------------------------------|-------|----------------|---------------------------------|---------|
| host       | llama         | (built at ~/llama.cpp)       | — (systemd `ragfarm-llama`)  | 8080  | —              | OpenAI base URL (swappable)     | no      |
| host       | embedder      | services/embedder            | — (systemd `ragfarm-embedder`)| 8090 | —              | internal (`/embed`) — not a tool| no      |
| container  | qdrant        | (upstream image)             | qdrant / infra-qdrant        | 6333/6334 | —          | internal (retrieval store)      | no      |
| container  | mcpo          | (upstream image)             | mcpo / infra-mcpo            | 8000  | — (is the front)| the single OpenAPI tool endpoint| n/a    |
| container  | open-webui    | (upstream image)             | open-webui / infra-open-webui| 3000  | —              | the UI (only 0.0.0.0 exposure)  | n/a     |
| container  | rag           | services/rag-retrieval       | rag-retrieval / infra-rag-retrieval | 8104 | /rag    | **tool server** (registered)    | no      |
| container  | placement     | services/mcp-placement       | mcp-placement / infra-mcp-placement | 8101 | /placement | **tool server** (registered) | no    |
| container  | host-control  | services/mcp-host-control    | mcp-host-control / infra-mcp-host-control | 8102 | /host-control | **wrapper only** (`reboot_guarded`) | YES |
| container  | fs            | services/mcp-fs              | mcp-fs / infra-mcp-fs        | 8103  | (unbridged)    | none — experimental (see below) | no      |
| container  | ingester      | services/ingester            | ingester / infra-ingester    | —     | —              | batch job, not in agent path    | no      |

\* Container name = compose project prefix (`infra`, from the compose file's
directory) + service name. Setting `container_name:` explicitly to `infra-<service>`
(dropping the trailing `-1`) is recommended for readability on a single-replica
on-prem stack; see migration.

### Naming rules (how every column is derived from the short name)

- **Directories** live under `services/` and are prefixed by domain family:
  - `mcp-<short>` for **infra-control** MCP servers (`mcp-placement`,
    `mcp-host-control`, `mcp-fs`, and future `mcp-net-diag`).
  - `rag-<short>` for **retrieval** MCP servers (`rag-retrieval`).
  Both families are MCP servers (streamable-http); the prefix denotes *domain*, not
  protocol. Do not add a third prefix without a line in this ADR.
- **Compose service name == directory name** (`mcp-placement`, `rag-retrieval`).
  No redundant segments: it is `mcp-placement`, never `mcp-infra-placement` — the
  `infra` is already supplied by the compose project prefix in the container name.
- **Container name** = `infra-<service>` (project prefix + service). This is where
  David Kubicek's `infra-{rag,mcp}-<name>` shape comes from and it now holds uniformly:
  `infra-rag-retrieval`, `infra-mcp-placement`, `infra-mcp-host-control`,
  `infra-mcp-fs`.
- **mcpo mount** = the short name, at `/<short>` on the single `:8000` endpoint
  (`/rag`, `/placement`, `/host-control`). Declared in
  `services/mcp-gateway/mcpo-config.json`, one entry per bridged MCP.
- **`FastMCP(name=...)`** inside each `server.py` == the short name (`"placement"`,
  `"host-control"`, `"fs"`, `"rag"`) so logs and the MCP handshake match the table.
- **systemd units** (host plane) = `ragfarm-<component>`: `ragfarm-llama`,
  `ragfarm-embedder`, `ragfarm-stack`. No bare `llama-server`/`embedder-server`.

### Port allocation (stable, sequential, documented)

| range      | owner                                            |
|------------|--------------------------------------------------|
| 3000       | open-webui (the only `0.0.0.0` bind; auth-gated) |
| 6333/6334  | qdrant                                           |
| 8000       | mcpo — the single aggregate OpenAPI endpoint     |
| 8080       | llama-server (host, OpenAI-compatible LLM)       |
| 8090       | embedder (host, `/embed`)                        |
| **81xx**   | **raw MCP servers, one sequential port each**    |
| 8101       | mcp-placement                                    |
| 8102       | mcp-host-control                                 |
| 8103       | mcp-fs                                            |
| 8104       | rag-retrieval                                     |
| 8105+      | next MCP (e.g. `mcp-net-diag` from ADR-0004)      |

New MCP → take the next free `81xx` port, add the row to the registry table, add
the mount to `mcpo-config.json`. All raw MCP ports bind `127.0.0.1` only; the sole
LAN-facing surface is open-webui:3000.

### Confirm-gated wrapper naming (cross-ref ADR-0004 §4)

- Wrappers live in `infra/openwebui/tools/` as `<action>_guarded.py`, OWUI tool id
  `<action>_guarded` (`reboot_guarded`). One wrapper per mutating tool.
- A wrapper targets `MCPO/<mount>/<tool>` (e.g. `reboot_guarded` →
  `/host-control/reboot_host`). It holds no creds and no infra logic (ADR-0004 §4).
- The mutating MCP it fronts is bridged in `mcpo-config.json` but **omitted** from
  the tool-server registration in `setup_openwebui.py`. That omission is load-bearing
  (ADR-0004 §4 non-registration rule), so annotate it in-file where it is omitted.

### Collocation

- All first-party MCP servers live under `services/`, one directory each, each with
  `server.py`, `Dockerfile`, `requirements.txt` (+ `.env.example` if it takes
  secrets). No MCP lives anywhere else.
- All bridged MCPs are declared in the single `services/mcp-gateway/mcpo-config.json`
  and reached through the single mcpo endpoint. `mcp-gateway/` is the one place that
  answers "what is wired to the agent"; `setup_openwebui.py` is the one place that
  answers "what the model may call directly (vs via a wrapper)."
- The compose file (`infra/compose.yaml`) is the one place that answers "what runs
  and on which port." These three files together are the map — keep them in sync
  with the registry table above and nothing else needs reading to understand the
  topology.

## Consequences

- Renames touch directories, compose service names, systemd unit filenames and a
  handful of cross-references. Because directory renames move `build:` contexts and
  unit renames move `After=`/`Wants=` targets, they are done as one coordinated
  commit (checklist below), not piecemeal.
- The registry table becomes a maintenance obligation: adding a component without
  adding its row is the one thing this ADR forbids. Cheap to honour, and it keeps
  the "read the whole topology off one table" promise true.
- `mcp-fs` is currently unbridged and out of the running agent path (not in
  `mcpo-config.json`, not in `ragfarm-stack.service`, not registered in
  `setup_openwebui.py`). This ADR does not decide its fate — it only makes its name
  consistent. Track the keep-or-drop decision separately; until then it is labelled
  *experimental* in the registry so its half-present state is intentional and visible
  rather than looking like a wiring bug.

## Migration checklist (one coordinated commit, on David Kubicek's go-ahead)

Nothing below changes behaviour; it only makes names match the registry. Run from
the repo root. `git mv` preserves history.

1. **Placement MCP — drop the redundant `infra-` segment**
   - `git mv services/mcp-infra-placement services/mcp-placement`
   - `infra/compose.yaml`: service `mcp-infra-placement:` → `mcp-placement:`,
     `build: ../services/mcp-infra-placement` → `../services/mcp-placement`,
     `env_file` path likewise.
   - `services/mcp-placement/server.py`: `FastMCP("infra-placement"...)` →
     `FastMCP("placement"...)` (align to short name; verify the current literal).
   - `manifests/ragfarm-stack.service`: `mcp-infra-placement` → `mcp-placement` in
     `ExecStart`/`ExecStop`.
   - `mcpo-config.json` mount key is already `placement` — no change.

2. **fs MCP — normalise to the `mcp-<short>` shape**
   - `git mv services/mcp-fs-agent services/mcp-fs`
   - `infra/compose.yaml`: service `mcp-fs-agent:` → `mcp-fs:`, build path likewise.
   - `services/mcp-fs/server.py`: `FastMCP("fs-agent"...)` → `FastMCP("fs"...)`.
   - (Defer the keep-vs-drop question; this is rename-only.)

3. **systemd units — `ragfarm-<component>`**
   - `git mv manifests/llama-server.service manifests/ragfarm-llama.service`
   - `git mv manifests/embedder-server.service manifests/ragfarm-embedder.service`
   - `manifests/ragfarm-stack.service`: update `After=`/`Wants=` from
     `llama-server.service embedder-server.service` →
     `ragfarm-llama.service ragfarm-embedder.service`.
   - Update the install one-liners in each unit's header comment.
   - On the host, the old unit files must be disabled/removed and the new ones
     enabled (`systemctl disable --now llama-server; systemctl enable --now
     ragfarm-llama`, same for embedder) — a host action, flagged here so it is not
     forgotten.

4. **Explicit container names (recommended, readability)**
   - In `infra/compose.yaml` add `container_name: infra-<service>` to each service
     (`infra-qdrant`, `infra-mcpo`, `infra-open-webui`, `infra-rag-retrieval`,
     `infra-mcp-placement`, `infra-mcp-host-control`, `infra-mcp-fs`,
     `infra-ingester`). Single-replica stack, so losing compose scaling is a
     non-cost.

5. **Doc sync (no code change)**
   - `services/mcp-gateway/README.md`: replace the stale service-registry table with
     the registry above (done in the same change set as this ADR).
   - `services/mcp-gateway/mcpo-config.json` + `infra/compose.yaml` comment: the
     "add placement/host-control when they unblock" note is stale — they are already
     mounted and run in MOCK mode. Reword to "mounted; switch ONE_MOCK/HOST_MOCK=0 at
     deployment."
   - `services/rag-retrieval/README.md` "Extending" section: same correction.

6. **Low-priority, optional**
   - `manifests/ragfarm-embedder.service` still carries NPU/XRT env
     (`XILINX_XRT`, flexml/voe `LD_LIBRARY_PATH`) though BGE-M3 runs on CPU
     (ADR-0002, step 03). Harmless (shared venv) but misleading; trim to the CPU
     path or add a one-line comment that the XRT env is vestigial.

## Alternatives considered

- **Leave names as-is, document the exceptions.** Rejected: the exceptions are the
  cost. A table with footnotes for every irregular name is exactly the "hard to
  read" state this ADR exists to remove.
- **One prefix for all MCPs (`mcp-*`), fold retrieval in as `mcp-rag`.** Viable and
  arguably purer (everything is an MCP). Rejected narrowly: the `rag-`/`mcp-` split
  carries useful domain information (retrieval vs infra-control) that maps to a real
  policy boundary — retrieval is read-only-forever, infra-control contains the
  mutating surface. Keeping them visually distinct is worth one extra prefix.
- **Non-sequential / ephemeral ports (let compose assign).** Rejected: stable
  sequential ports are a debugging affordance (operator `curl`s `:8102` without a
  lookup) and cost nothing on a fixed on-prem stack.
