#!/usr/bin/env python3
"""Idempotent Open WebUI configuration for the ragfarm agent layer (ADR-0003).

Registers the mcpo OpenAPI tool server (search_corpus) and creates the
"ragfarm (corpus RAG)" model preset: base Qwen2.5-7B + the rag tool pre-attached
+ native function calling + a grounding system prompt that makes the model answer
from retrieved chunks verbatim instead of generalizing.

Open WebUI stores this in its Docker volume, so it survives restarts; this script
is the reproducible source of that config (re-run any time / against a fresh UI).

Usage (from the host, with the stack up):
    OWUI_URL=http://127.0.0.1:3000 \
    OWUI_TOKEN=<admin JWT>  python3 infra/openwebui/setup_openwebui.py
  or, to sign in / bootstrap the first admin:
    OWUI_URL=http://127.0.0.1:3000 \
    OWUI_EMAIL=admin@ragfarm.local OWUI_PASSWORD=... \
    python3 infra/openwebui/setup_openwebui.py

Config knobs (env, with sensible defaults):
    MCPO_RAG_URL     default http://127.0.0.1:8000/rag
    BASE_MODEL_ID    default qwen2.5-7b-instruct
"""
import os
import sys
import pathlib
import requests

URL = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
MCPO_RAG_URL = os.environ.get("MCPO_RAG_URL", "http://127.0.0.1:8000/rag")
MCPO_PLACEMENT_URL = os.environ.get("MCPO_PLACEMENT_URL", "http://127.0.0.1:8000/placement")
BASE_MODEL_ID = os.environ.get("BASE_MODEL_ID", "qwen2.5-7b-instruct")
# host-control is bridged by mcpo but deliberately NOT registered as an OWUI tool
# server; the model reaches reboot only through the reboot_guarded Python Tool.
REBOOT_TOOL_PY = pathlib.Path(__file__).with_name("tools") / "reboot_guarded.py"

# Two hard rules, not a bulleted list: the "call BEFORE writing anything" compulsion
# and the no-preamble rule must reinforce each other. Softer phrasings either let the
# 7B skip the tool (answering from its own knowledge) or make it narrate the tool call
# ("Použiji tool_search_corpus_post…") before answering. Measured: this phrasing gives
# 5/5 tool calls and 0/5 preambles; see infra/openwebui/ tuning in build log 07.
GROUNDING_SYSTEM = (
    "You are an infrastructure assistant for the ŠA / EPC hosting environment.\n\n"
    "RULE 1 — act via tools first, silently: choose the right tool and call it BEFORE "
    "writing anything. NEVER answer from your own knowledge, and NEVER write text before a "
    "tool call — no announcements, no explanations, no mention of tools/functions. Routing:\n"
    "  - documented facts (hosts, IPs, VLANs, FQDNs, access/backup procedures) -> search_corpus\n"
    "  - where a VM runs / what runs on a host (live placement) -> where_is_vm / list_vms_on_host\n"
    "  - reboot/restart/bounce a hypervisor host -> reboot_host (it will require the user to confirm)\n\n"
    "RULE 2 — answer only from results: base your answer ONLY on what the tools return. "
    "Quote specific values verbatim (hostnames, IPs, VLAN IDs, FQDNs, VM names, steps). Do "
    "not generalize or invent. If a tool reports it cannot find or do something (e.g. a "
    "reboot was cancelled or a host is not allowlisted), say exactly that. Reply in the same "
    "language as the question (Czech question -> Czech answer); name the source when useful."
)


def get_token() -> str:
    tok = os.environ.get("OWUI_TOKEN")
    if tok:
        return tok
    email, pw = os.environ.get("OWUI_EMAIL"), os.environ.get("OWUI_PASSWORD")
    if not (email and pw):
        sys.exit("Provide OWUI_TOKEN, or OWUI_EMAIL + OWUI_PASSWORD.")
    # try signin, then signup (first user becomes admin)
    for path in ("/api/v1/auths/signin", "/api/v1/auths/signup"):
        body = {"email": email, "password": pw}
        if path.endswith("signup"):
            body["name"] = "admin"
        r = requests.post(URL + path, json=body, timeout=30)
        if r.ok and r.json().get("token"):
            return r.json()["token"]
    sys.exit("Could not obtain a token via signin/signup.")


def _tool_server(url, name, desc):
    return {"url": url, "path": "openapi.json", "auth_type": "none", "key": "",
            "config": {"enable": True, "access_control": None},
            "info": {"name": name, "description": desc}}


def main() -> None:
    tok = get_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    # 1. Register the read-only mcpo tool servers, in order -> server:0, server:1.
    #    host-control is intentionally absent so the model cannot call reboot directly.
    conns = [
        _tool_server(MCPO_RAG_URL, "rag", "Corpus hybrid retrieval (search_corpus)"),
        _tool_server(MCPO_PLACEMENT_URL, "placement", "OpenNebula placement (where_is_vm, list_vms_on_host)"),
    ]
    r = requests.post(URL + "/api/v1/configs/tool_servers", headers=H,
                      json={"TOOL_SERVER_CONNECTIONS": conns}, timeout=30)
    r.raise_for_status()
    server_ids = [f"server:{i}" for i in range(len(conns))]  # order-defined
    print(f"tool servers registered: {[c['url'] for c in conns]} -> {server_ids}")

    # 2. Create/update the reboot_guarded Python Tool (human-confirmation gate).
    tool_body = {
        "id": "reboot_guarded",
        "name": "Guarded host reboot",
        "content": REBOOT_TOOL_PY.read_text(),
        "meta": {"description": "Drain-then-reboot a hypervisor behind a confirmation modal."},
        "access_grants": [],
    }
    r = requests.post(URL + "/api/v1/tools/create", headers=H, json=tool_body, timeout=30)
    if r.status_code != 200:
        r = requests.post(URL + "/api/v1/tools/id/reboot_guarded/update", headers=H, json=tool_body, timeout=30)
    r.raise_for_status()
    print("python tool 'reboot_guarded' ready")

    # 3. Create/update the ragfarm preset with all three tools attached.
    tool_ids = server_ids + ["reboot_guarded"]
    body = {
        "id": "ragfarm",
        "base_model_id": BASE_MODEL_ID,
        "name": "ragfarm (corpus RAG + infra)",
        "meta": {
            "description": "Qwen2.5-7B with corpus retrieval, OpenNebula placement, and guarded host reboot.",
            "toolIds": tool_ids,
            "capabilities": {"citations": True},
        },
        "params": {"system": GROUNDING_SYSTEM, "function_calling": "native"},
        "access_grants": [],
        "is_active": True,
    }
    r = requests.post(URL + "/api/v1/models/create", headers=H, json=body, timeout=30)
    if r.status_code != 200:
        r = requests.post(URL + "/api/v1/models/model/update", headers=H, json=body, timeout=30)
    r.raise_for_status()
    print(f"model preset 'ragfarm' ready (tools={tool_ids}, base={BASE_MODEL_ID})")


if __name__ == "__main__":
    main()
