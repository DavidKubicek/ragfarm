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
import requests

URL = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
MCPO_RAG_URL = os.environ.get("MCPO_RAG_URL", "http://127.0.0.1:8000/rag")
BASE_MODEL_ID = os.environ.get("BASE_MODEL_ID", "qwen2.5-7b-instruct")

GROUNDING_SYSTEM = (
    "You are an infrastructure assistant for the ŠA / EPC hosting environment. "
    "For ANY question about hosts, VMs, hostnames, IP addresses, VLANs, FQDNs, access "
    "or backup procedures, or other documented facts, you MUST call the search_corpus "
    "tool first and base your answer ONLY on the chunks it returns.\n\n"
    "Grounding rules:\n"
    "- Quote the specific values verbatim: hostnames, IP addresses, VLAN IDs, FQDNs, "
    "usernames, and step-by-step procedures exactly as they appear in the retrieved text.\n"
    "- Do NOT generalize, give generic networking advice, or invent details that are not "
    "present in the retrieved chunks.\n"
    "- If the retrieved chunks do not contain the answer, say so plainly instead of guessing.\n"
    "- Reply in the same language as the user's question (Czech question -> Czech answer).\n"
    "- When useful, name the source (e.g. the xlsx sheet or the notes document)."
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


def main() -> None:
    tok = get_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    # 1. Register the mcpo OpenAPI tool server (search_corpus) as tool id server:<n>.
    conn = {
        "url": MCPO_RAG_URL, "path": "openapi.json", "auth_type": "none", "key": "",
        "config": {"enable": True, "access_control": None},
        "info": {"name": "rag", "description": "Corpus hybrid retrieval (search_corpus)"},
    }
    r = requests.post(URL + "/api/v1/configs/tool_servers", headers=H,
                      json={"TOOL_SERVER_CONNECTIONS": [conn]}, timeout=30)
    r.raise_for_status()
    print(f"tool server registered: {MCPO_RAG_URL}")

    # Resolve the tool id (server:<idx>) the connection got.
    tools = requests.get(URL + "/api/v1/tools/", headers=H, timeout=30).json()
    rag_tool_id = next((t["id"] for t in tools if t.get("name") == "rag"
                        or str(t.get("id", "")).startswith("server:")), "server:0")

    # 2. Create/update the ragfarm model preset.
    body = {
        "id": "ragfarm",
        "base_model_id": BASE_MODEL_ID,
        "name": "ragfarm (corpus RAG)",
        "meta": {
            "description": "Qwen2.5-7B with corpus retrieval (search_corpus) pre-attached and a grounding prompt.",
            "toolIds": [rag_tool_id],
            "capabilities": {"citations": True},
        },
        "params": {"system": GROUNDING_SYSTEM, "function_calling": "native"},
        "access_grants": [],
        "is_active": True,
    }
    r = requests.post(URL + "/api/v1/models/create", headers=H, json=body, timeout=30)
    if r.status_code != 200:
        # already exists (or otherwise) -> update in place (id is carried in the body)
        r = requests.post(URL + "/api/v1/models/model/update", headers=H, json=body, timeout=30)
    r.raise_for_status()
    print(f"model preset 'ragfarm (corpus RAG)' ready (tool={rag_tool_id}, base={BASE_MODEL_ID})")


if __name__ == "__main__":
    main()
