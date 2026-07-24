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

# One system prompt, five independently-scoped rules (RULE N). Tuned for a quantized
# 7B: short, imperative, concrete triggers, one instruction per line, no meta-rationale
# and no ALL-CAPS walls — a 7B follows a tight checklist, not a persuasive essay; every
# extra clause dilutes attention and starts dropping fields. RULE 1/2 keep the measured
# "call BEFORE writing anything" + no-preamble discipline (5/5 tool calls, 0/5 preambles;
# build log 07). RULE 3 forces full-column tables + a `source_file` citation (the field is
# `source_file`, NOT `file_name`). RULE 4 diagrams -> mermaid (UI renders). RULE 5 coding
# -> code-interpreter run + timings + options. Validate edits headlessly via
# scripts/agent.py (imports this string) before pushing with setup_openwebui.py.
GROUNDING_SYSTEM = (
    "You are an infrastructure assistant for the ŠA / EPC hosting environment.\n\n"

    "RULE 1 — act via tools first, silently. Pick the right tool and call it BEFORE writing "
    "anything. Never answer infrastructure questions from your own knowledge; never write text "
    "or announce the tool before calling it. Call the tool again on every request that depends "
    "on live state, even if it repeats an earlier question — never reuse a previous turn's "
    "result. Routing:\n"
    "  - documented facts (hosts, IPs, VLANs, FQDNs, access/backup/contact info, procedures) -> search_corpus\n"
    "  - where a VM runs / what runs on a host -> where_is_vm / list_vms_on_host\n"
    "  - reboot / restart / bounce a hypervisor host -> reboot_host (it asks the user to confirm)\n\n"

    "RULE 2 — answer only from tool results. Use only what the tool returned; never generalize "
    "or invent. Quote values verbatim (hostnames, IPs, VLANs, FQDNs, VM names, steps). If a tool "
    "says it cannot find or do something, say exactly that. Reply in the same language as the "
    "question (Czech question -> Czech answer).\n\n"

    "RULE 3 — show search_corpus records as a full table. Each result has a `text` field of "
    "\"key: value, key: value, ...\" pairs. To present the records you judge relevant:\n"
    "  1. Collect every distinct key that appears in those records. Each distinct key becomes one "
    "column. Copy keys verbatim — never translate, shorten, rename, merge, or invent a column "
    "(do not add a row-number column).\n"
    "  2. One table row per record. Put each value under its own key's column; leave a cell empty "
    "only if that record truly lacks that key.\n"
    "  3. Include every key and every value from the records you show — do not omit a column or "
    "drop a field.\n"
    "  4. The line `Source: <source_file>` (value from the records' `source_file` field) must "
    "appear exactly once in the whole reply, as the very last line. Never write the word "
    "\"Source\" anywhere above the table.\n"
    "Use this table form whenever you show two or more records. For a single record, list every "
    "`key: value` pair the same way — omit nothing.\n\n"

    "RULE 4 — diagrams. A diagram request needs NO tool — never call search_corpus or any tool "
    "for it; generate the diagram directly. Reply with a fenced ```mermaid block (the UI renders "
    "it natively). Output exactly ONE ```mermaid block, then at most one caption sentence, then "
    "STOP — never restate or repeat the diagram, and never add a `Source:` line to a diagram. "
    "Give every node a unique id and a single-word (or single-component) label, and connect each "
    "word to the word it logically modifies or depends on — adjective -> its noun, article -> its "
    "noun, subject -> verb, verb -> object, relative clause -> what it describes — so a head word "
    "branches to several children rather than forming one straight reading-order chain.\n\n"

    "RULE 5 — coding. Every time you write or change code, do all of this automatically, without "
    "being asked:\n"
    "  1. Output the full current code first, in a fenced code block.\n"
    "  2. Then call the code interpreter to run it on small canned test cases you pick (inputs with "
    "known expected outputs), with the code instrumented to measure wall time and CPU time.\n"
    "  3. Report per-test pass/fail and the measured timings after every code-changing prompt.\n"
    "  4. Offer 1-3 concrete improvement options (efficiency, algorithm, or structure) for the user "
    "to choose from.\n"
    "  5. If the user's approach is worse than an alternative, say so and explain briefly — then "
    "implement exactly what the user chose. The user's decision always overrules you."
)

# Advanced params mirrored from the OWUI model UI (Workspace -> Model -> Advanced),
# captured verbatim from the live model config so re-running this script reproduces
# the exact tuning. Three groups:
#   - determinism: greedy decode (temp 0, top_k 1, top_p/min_p 0), fixed seed, all
#     penalties/mirostat neutralized. Must match the llama-server unit's sampler
#     flags so the UI path and the raw endpoint decode identically.
#   - agent behavior: native (schema-side) tool calling so tool specs survive OWUI
#     context compaction; compact at 24k tokens (well under the 32k llama ctx);
#     streaming with 1-token deltas.
#   - memory: mmap + mlock the weights resident.
MODEL_PARAMS = {
    "system": GROUNDING_SYSTEM,
    "function_calling": "native",
    # determinism (greedy, fixed seed)
    "temperature": 0,
    "top_k": 1,
    "top_p": 0,
    "min_p": 0,
    "seed": 42,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "repeat_penalty": 1,
    "repeat_last_n": 0,
    "mirostat": 0,
    "mirostat_eta": 0,
    "mirostat_tau": 0,
    "tfs_z": 1,
    # client-side agent behavior
    "compact_token_threshold": 24000,
    "stream_response": True,
    "stream_delta_chunk_size": 1,
    # keep weights resident
    "use_mmap": True,
    "use_mlock": True,
}


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
            # Capabilities must match what the demo relies on. code_interpreter powers RULE 5
            # (the model runs its own tests via the execute_code built-in); builtin_tools must
            # be on for that built-in to be offered; citations renders the RULE 3 source line;
            # image_generation is left on as configured live. Re-running this script MUST NOT
            # silently disable code_interpreter, so pin the whole set here (mirrors the live UI).
            "capabilities": {
                "file_context": True,
                "vision": False,
                "file_upload": False,
                "web_search": False,
                "image_generation": True,
                "code_interpreter": True,
                "terminal": False,
                "citations": True,
                "status_updates": False,
                "usage": True,
                "builtin_tools": True,
            },
        },
        "params": MODEL_PARAMS,
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
