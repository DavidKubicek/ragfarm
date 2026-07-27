#!/usr/bin/env python3
"""Idempotent Open WebUI configuration for the ragfarm agent layer (ADR-0003).

Registers the mcpo OpenAPI tool server(s) and creates TWO OWUI model presets:
  - "ragfarm"        — text engine (tables + code + mermaid, greedy sampler).
  - "ragfarm-vision" — vision engine (image input, draw.io HTML render, temp>=0.6
                       sampler — Qwen3-VL Thinking requires non-greedy per Qwen's
                       own guidance; determinism knobs like top_k / top_p / min_p /
                       seed are DROPPED so llama.cpp's defaults apply).
Both presets get the same tools (rag + placement + reboot_guarded) so either engine
can drive the infra. Only one llama-server model is loaded at a time (the wrapper's
--alias); the preset whose base_model_id matches the live alias is the one that
actually works — the other stays as a stored config waiting for a model swap.

Auto-detection: the vision preset's base_model_id defaults to whatever /v1/models
reports as `multimodal`, so it tracks scripts/activate-llm.sh / .env swaps without
manual config. Overridable via VISION_BASE_MODEL_ID / TEXT_BASE_MODEL_ID env.

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
    MCPO_RAG_URL           default http://127.0.0.1:8000/rag
    LLAMA_URL              default http://127.0.0.1:8080 (for vision auto-detect)
    TEXT_BASE_MODEL_ID     default qwen2.5-7b-instruct (the greedy text preset)
    VISION_BASE_MODEL_ID   default auto (query LLAMA_URL /v1/models, first with
                           capability "multimodal"; falls back to qwen_qwen3-vl-8b-thinking)
"""
import os
import sys
import pathlib
import requests

URL = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")
MCPO_RAG_URL = os.environ.get("MCPO_RAG_URL", "http://127.0.0.1:8000/rag")
MCPO_PLACEMENT_URL = os.environ.get("MCPO_PLACEMENT_URL", "http://127.0.0.1:8000/placement")
# TEXT_BASE_MODEL_ID accepts the historical BASE_MODEL_ID as a fallback so old
# invocations that set BASE_MODEL_ID keep working unchanged.
TEXT_BASE_MODEL_ID = os.environ.get("TEXT_BASE_MODEL_ID") or os.environ.get("BASE_MODEL_ID", "qwen2.5-7b-instruct")
VISION_BASE_MODEL_ID_ENV = os.environ.get("VISION_BASE_MODEL_ID")  # None -> auto-detect
# host-control is bridged by mcpo but deliberately NOT registered as an OWUI tool
# server; the model reaches reboot only through the reboot_guarded Python Tool.
REBOOT_TOOL_PY = pathlib.Path(__file__).with_name("tools") / "reboot_guarded.py"


def _detect_vision_model_id(default: str) -> str:
    """Ask llama-server which model is loaded; return its alias iff it advertises
    the `multimodal` capability. Falls back to `default` if llama-server is down or
    the loaded model isn't a vision one (harmless — the preset just points at a
    stored id that isn't live right now)."""
    if VISION_BASE_MODEL_ID_ENV:
        return VISION_BASE_MODEL_ID_ENV
    try:
        r = requests.get(f"{LLAMA_URL}/v1/models", timeout=5)
        r.raise_for_status()
        for m in r.json().get("models", []):
            caps = m.get("capabilities") or []
            if "multimodal" in caps or "vision" in caps:
                return m.get("model") or m.get("id") or default
    except Exception:
        pass
    return default

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


# ============================================================================
# VISION preset — separate system prompt + params for the Qwen3-VL model.
# ============================================================================
# Qwen3-VL Thinking's own guidance forbids greedy decode (reasoning traces loop),
# so this preset holds temp>=0.6 as a floor and deliberately DROPS the sampler-shape
# knobs (top_k/top_p/min_p) so llama.cpp's own nucleus-sampling defaults apply.
# `seed` is also dropped: with temp>0 the seed is a fragile reproducibility anchor
# (small numerical drift from cache warmth defeats it), and removing it is honest
# about "non-deterministic sampling here — outputs will vary between runs."
#
# The prompt duplicates the text preset's tools/tables/coding rules verbatim
# (kept independent so each preset can evolve without coupling), adds RULE 4 for
# image inputs, and replaces the mermaid-only diagram rule with mermaid-OR-drawio
# so requests for "interactive / editable / pan-zoom" diagrams render as draw.io
# via OWUI's HTML preview sandbox (the model must output an <html>-wrapped
# viewer-static.min.js block; OWUI renders it in-chat as an interactive canvas).
VISION_GROUNDING_SYSTEM = (
    "You are an infrastructure and vision assistant for the ŠA / EPC hosting "
    "environment. Sampling is non-greedy (temperature ~0.6): outputs vary between "
    "runs — stay concise, stay on task, and re-check any list or number you produce.\n\n"

    "RULE 1 — act via tools first, silently. Pick the right tool and call it BEFORE writing "
    "anything. Never answer infrastructure questions from your own knowledge; never write text "
    "or announce the tool before calling it. Call the tool again on every NEW user question that "
    "depends on live state — never reuse a previous turn's result. Routing:\n"
    "  - documented facts (hosts, IPs, VLANs, FQDNs, access/backup/contact info, procedures) -> search_corpus\n"
    "  - where a VM runs / what runs on a host -> where_is_vm / list_vms_on_host\n"
    "  - reboot / restart / bounce a hypervisor host -> reboot_host (it asks the user to confirm)\n"
    "  Call each of these ragfarm tools AT MOST ONCE per user turn. The single call returns every "
    "relevant chunk our custom retrieval was designed to return — do not 'refine', do not "
    "'double-check', do not re-query with a shortened phrase, do not call the same tool a second "
    "time in the same turn even if the first result feels partial. If two DIFFERENT ragfarm tools "
    "are legitimately needed to answer (e.g. where_is_vm then reboot_host), one call each is fine. "
    "Multi-step iteration is for the OWUI built-in code interpreter only — never for our custom "
    "ragfarm tools.\n\n"

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

    "RULE 4 — images attached by the user. Describe what is present, quoting any visible text "
    "verbatim (do not translate unless asked). Do not invent objects, labels, values, brands, or "
    "people not visible; if the image is blurry, cropped, or you are uncertain, say so instead "
    "of guessing. For OCR / receipt / form tasks, output the fields exactly as printed, keeping "
    "the original punctuation and number formatting.\n\n"

    "RULE 5 — diagrams. The user asks which format they want; you produce exactly that one, "
    "never both. No tool call for diagram requests. The two supported formats:\n"
    "\n"
    "  a) Mermaid — when the user says \"mermaid\" (or the request is ambiguous, ask which). "
    "Output ONE fenced ```mermaid block, then at most one caption sentence, then stop.\n"
    "\n"
    "  b) draw.io — when the user says \"draw.io\" / \"drawio\" / \"editable\" / \"interactive\" / "
    "\"pan-zoom\". Output ONE fenced ```html block using EXACTLY this wrapper so Open WebUI "
    "renders it in-chat (window.*_PATH overrides point the viewer at our LOCAL mirror of the "
    "draw.io webapp — no external fetches, so the diagram renders even air-gapped):\n"
    "```html\n"
    "<!DOCTYPE html>\n"
    "<html><head><meta charset=\"utf-8\">\n"
    "<style>body{margin:0;padding:10px;background:#fff}"
    ".mxgraph{width:100%;height:500px;border:1px solid #ccc;border-radius:6px}</style>\n"
    "<script>\n"
    "  window.STYLE_PATH   = 'http://127.0.0.1/styles';\n"
    "  window.SHAPES_PATH  = 'http://127.0.0.1/shapes';\n"
    "  window.STENCIL_PATH = 'http://127.0.0.1/stencils';\n"
    "  window.DRAW_MATH_URL = 'http://127.0.0.1/math4/es5';\n"
    "  window.GRAPH_IMAGE_PATH = 'http://127.0.0.1/img';\n"
    "</script>\n"
    "</head><body>\n"
    "<div class=\"mxgraph\" data-mxgraph='{\"highlight\":\"#0000ff\",\"nav\":true,\"resize\":true,\"toolbar\":\"zoom layers lightbox\"}'>\n"
    "<xml>\n"
    "  <mxfile>... your syntactically valid draw.io XML here, RAW (no escaping, no JSON) ...</mxfile>\n"
    "</xml>\n"
    "</div>\n"
    "<script type=\"text/javascript\" src=\"http://127.0.0.1/js/viewer-static.min.js\"></script>\n"
    "</body></html>\n"
    "```\n"
    "The XML must start with `<mxfile>` and end with `</mxfile>`, live directly inside `<xml>` "
    "unescaped, and be a valid draw.io document. One block, one caption, then stop — never a "
    "second block, never the other format next to it.\n\n"

    "RULE 6 — coding. Every time you write or change code, do all of this automatically:\n"
    "  1. Output the full current code first, in a fenced code block.\n"
    "  2. Then call the code interpreter to run it on small canned test cases you pick (inputs "
    "with known expected outputs), instrumented to measure wall time and CPU time.\n"
    "  3. Report per-test pass/fail and the measured timings after every code-changing prompt.\n"
    "  4. Offer 1-3 concrete improvement options (efficiency, algorithm, or structure).\n"
    "  5. If the user's approach is worse than an alternative, say so briefly — then implement "
    "exactly what the user chose. The user's decision always overrules you."
)


VISION_MODEL_PARAMS = {
    "system": VISION_GROUNDING_SYSTEM,
    "function_calling": "native",
    # Non-greedy sampling required by Qwen3-VL Thinking. temp 0.6 is Qwen's own
    # recommended floor; top_k/top_p/min_p/seed intentionally OMITTED so llama.cpp's
    # defaults (nucleus sampling) apply — mixing an explicit temp with hand-set
    # sampler-shape knobs re-introduces the determinism trap this preset is meant
    # to avoid.
    "temperature": 0.6,
    # client-side agent behavior — matches text preset's 24000 (wrapper ctx 32768).
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

    # 3. Create/update BOTH model presets (text + vision). Same tools on both so
    #    whichever llama-server model is loaded can drive the infra; only the
    #    preset whose base_model_id matches the live alias is actually usable at
    #    any given moment (see module docstring).
    tool_ids = server_ids + ["reboot_guarded"]

    # Capability matrix per preset — matches Workspace -> Model Advanced settings
    # verbatim (screenshot committed 2026-07-26). Only `vision` differs between
    # presets (VL model only); file_upload is on for BOTH so both engines can
    # accept files, image_generation stays OFF (OWUI's built-in image gen depends
    # on an external DALL-E/SD backend we don't run). Re-running this script MUST
    # NOT silently drop code_interpreter or usage (RULE 6 + tests/tracing depend
    # on them), so we pin the whole set here rather than let anything default.
    caps_text = {
        "file_context": True, "vision": False, "file_upload": True,
        "web_search": False, "image_generation": False, "code_interpreter": True,
        "terminal": False, "citations": True, "status_updates": True,
        "usage": True, "builtin_tools": True,
    }
    caps_vision = {**caps_text, "vision": True}

    # Default Features (defaultFeatureIds) — per-chat toggles pre-selected in new
    # conversations. Code interpreter only — web_search removed (its capability is
    # off across both presets, and pre-checking it just presents a broken affordance).
    default_features = ["code_interpreter"]

    # Builtin Tools (builtinTools) — OWUI's opt-out convention: only false-flagged
    # keys are persisted; absent keys default to ENABLED. Automations / tasks /
    # web_search are explicitly OFF because they encourage the Thinking model to
    # invent multi-step iteration where the single-shot RAG call was already
    # complete (observed on the ragfarm-vision preset — multi-loop tool traps).
    # Knowledge and Calendar OFF because we don't run those backends.
    builtin_tools = {"knowledge": False, "calendar": False,
                     "automations": False, "tasks": False, "web_search": False}

    vision_base = _detect_vision_model_id(default="qwen_qwen3-vl-8b-thinking")

    presets = [
        {
            "id": "ragfarm",
            "base_model_id": TEXT_BASE_MODEL_ID,
            "name": "ragfarm (corpus RAG + infra)",
            "description": "Text engine: greedy Qwen2.5-7B with corpus retrieval, OpenNebula placement, and guarded host reboot.",
            "params": MODEL_PARAMS,
            "capabilities": caps_text,
        },
        {
            "id": "ragfarm-vision",
            "base_model_id": vision_base,
            "name": "ragfarm-vision (Qwen3-VL + infra + draw.io)",
            "description": "Vision engine: Qwen3-VL Thinking (temp>=0.6, non-greedy) with image input, corpus RAG, placement, reboot, and draw.io in-chat rendering.",
            "params": VISION_MODEL_PARAMS,
            "capabilities": caps_vision,
        },
    ]

    for p in presets:
        body = {
            "id": p["id"],
            "base_model_id": p["base_model_id"],
            "name": p["name"],
            "meta": {
                "description": p["description"],
                "toolIds": tool_ids,
                "capabilities": p["capabilities"],
                "defaultFeatureIds": default_features,
                "builtinTools": builtin_tools,
            },
            "params": p["params"],
            "access_grants": [],
            "is_active": True,
        }
        r = requests.post(URL + "/api/v1/models/create", headers=H, json=body, timeout=30)
        if r.status_code != 200:
            r = requests.post(URL + "/api/v1/models/model/update", headers=H, json=body, timeout=30)
        r.raise_for_status()
        print(f"model preset '{p['id']}' ready (tools={tool_ids}, base={p['base_model_id']})")


if __name__ == "__main__":
    main()
