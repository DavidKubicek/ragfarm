# mcp-gateway — registering MCP services with the agent/LLM

The generative LLM runs on `llama-server` (iGPU/Vulkan, OpenAI-compatible at
:8080). llama-server itself does NOT discover MCP servers; an agent/client layer
does. Two supported options — pick one and record it in an ADR:

1. **Client-side agent (recommended to start):** a small Python agent using the
   official `mcp` client + an OpenAI-compatible client pointed at llama-server.
   It connects to each MCP over streamable HTTP, lists their tools, and exposes
   them to the model as tool definitions. Lowest-magic, easiest to debug.

2. **Gateway/proxy:** run an MCP aggregator that multiplexes the 4-5 MCP servers
   behind one endpoint, then point a single agent at it.

## Service registry (HTTP streamable MCP endpoints)
| service              | port | tools                                  | mutates infra |
|----------------------|------|----------------------------------------|---------------|
| mcp-infra-placement  | 8101 | where_is_vm, list_vms_on_host          | no            |
| mcp-host-control     | 8102 | reboot_host (guarded, dry-run default) | YES           |
| mcp-fs-agent         | 8103 | list_files, read_text (sandboxed RO)   | no            |
| (rag-retrieval)      | TBD  | search_corpus (Qdrant query)           | no            |

## Guardrails to enforce at the gateway
- host-control tools require explicit confirm + allowlist (already in the tool).
- Log every tool call (who/what/args) for audit — infra is the customer's.
- Keep retrieval/read tools and mutating tools on separate services so policy
  can treat them differently.
