# mcp-gateway — how MCP services reach the agent/LLM

This directory is the one place that answers **"what is wired to the agent."** It
holds `mcpo-config.json` (the single aggregator config) and this registry.

## Decision (recorded — see ADR-0003)

The generative LLM runs on `llama-server` (OpenAI-compatible at :8080). llama-server
does not discover MCP servers; an agent/client layer does. This README originally
posed two options for that layer:

1. a custom **client-side Python agent** (`mcp` client + OpenAI client → llama-server), or
2. a **gateway/proxy** aggregating the MCP servers behind one endpoint that a single
   (still custom) agent points at.

**Both are superseded by ADR-0003 (ACCEPTED 2026-06-18).** The decision was neither
option as written: the custom agent (option 1) was retired entirely, and instead
**Open WebUI** is the agent loop with **mcpo** (the MCP→OpenAPI proxy) as the bridge.
mcpo does the aggregation option 2 imagined, but its output is OpenAPI consumed by an
off-the-shelf UI, not a single MCP endpoint feeding our own agent. If you only
remember one thing: the agent layer is **Open WebUI + mcpo**, and `services/agent/`
no longer exists.

Related decisions: **ADR-0004** governs how *mutating* tools are exposed (the
confirm-gated wrapper pattern), and **ADR-0005** governs naming, ports and layout.

## Topology: one endpoint at the front, sequential ports at the back

mcpo publishes a **single** OpenAPI endpoint at `127.0.0.1:8000`, with each MCP
mounted under `/<mount>`. That is the only tool endpoint Open WebUI reasons about.
Behind it, each raw MCP server also owns a stable **`81xx`** port so an operator can
`curl` it directly. Single-endpoint (for the agent) and sequential-ports (for the
human) are complementary — see ADR-0005 §ports.

```
Open WebUI (127.0.0.1:3000) ──OpenAI──▶ llama-server (127.0.0.1:8080/v1)   [swappable endpoint, ADR-0003]
     │  tools via the single mcpo OpenAPI endpoint
     ▼
mcpo (127.0.0.1:8000)
     ├── /rag          ──▶ rag-retrieval    (127.0.0.1:8104)   search_corpus
     ├── /placement    ──▶ mcp-placement    (127.0.0.1:8101)   where_is_vm, list_vms_on_host
     └── /host-control ──▶ mcp-host-control (127.0.0.1:8102)   reboot_host  (mutating — wrapper only)
```

## Service registry (source of truth: ADR-0005 registry table)

| short name   | dir (services/)   | port | mcpo mount     | agent-facing surface              | mutates |
|--------------|-------------------|------|----------------|-----------------------------------|---------|
| rag          | rag-retrieval     | 8104 | /rag           | tool server (registered in OWUI)  | no      |
| placement    | mcp-placement     | 8101 | /placement     | tool server (registered in OWUI)  | no      |
| host-control | mcp-host-control  | 8102 | /host-control  | **wrapper only** (`reboot_guarded`) | YES   |
| fs           | mcp-fs            | 8103 | (unbridged)    | none — experimental               | no      |

Notes:
- **rag / placement are read-only** and registered directly as Open WebUI tool
  servers by `infra/openwebui/setup_openwebui.py`. The model calls them straight.
- **host-control mutates**, so per ADR-0004 §4 it is bridged by mcpo but **NOT**
  registered as a tool server. The model's only path to it is the
  `reboot_guarded` Open WebUI Python tool, which does dry-run → confirmation modal →
  execute. The absence of host-control from the tool-server list is a security
  property, not an oversight.
- **fs** is not currently bridged (absent from `mcpo-config.json`) and not in the
  running agent path; it is experimental. Keep-or-drop is tracked separately
  (ADR-0005 §consequences).
- placement + host-control run in **MOCK** mode in the PoC (`ONE_MOCK` / `HOST_MOCK`
  default `1`); flip to `0` at deployment once OpenNebula creds/reachability exist.
  They are already present in `mcpo-config.json` — no "add later" step remains.

## Adding a new MCP (the whole procedure)

1. Create `services/mcp-<short>/` (infra-control) or `services/rag-<short>/`
   (retrieval) with `server.py` (`FastMCP("<short>", host="0.0.0.0", port=<81xx>)`),
   `Dockerfile`, `requirements.txt`. Take the next free `81xx` port.
2. Add the row to the ADR-0005 registry table and to the table above.
3. Add the service to `infra/compose.yaml` (name == dir, bind `127.0.0.1:<port>`).
4. Add the mount to `mcpo-config.json` (`"<short>": {streamable-http, .../mcp}`).
5. **If read-only:** register it as a tool server in `setup_openwebui.py`.
   **If mutating:** do NOT register it; instead add an `<action>_guarded.py` wrapper
   under `infra/openwebui/tools/` from the ADR-0004 §4 template, and attach the
   wrapper to the model preset.

## Guardrails (enforced across the gateway)

- **Mutating tools go through a confirm-gated wrapper, never direct** (ADR-0004 §4).
  The two-phase `confirm=False`→plan / `confirm=True`→act contract stays in the MCP
  server; the human gate lives in the wrapper.
- **Least privilege stays server-side.** Credentials and privileged calls (ONE auth,
  `one.vm.migrate`, forced-command SSH) live inside the MCP container, never in the
  Open WebUI process. Wrappers hold no secrets.
- **Log every tool call** (requester, resolved args, result) for audit — the infra is
  the customer's. The wrapper is the natural choke point for mutations.
- **Read/mutate separation by service** so policy can treat them differently, and so
  the registered-tool-server list stays a truthful manifest of the model's direct
  reach.
