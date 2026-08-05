#!/usr/bin/env python3
"""Idempotent Open WebUI configuration for the ragfarm agent layer (ADR-0003).

Registers the mcpo OpenAPI tool server(s) and upserts the OWUI model presets
described by MODEL_TUNING.

>>> TO CHANGE ANY PER-MODEL SETTING, EDIT `MODEL_TUNING` NEAR THE TOP OF THIS
>>> FILE. Nothing else needs touching. It is keyed by the model ALIAS — the id the
>>> inference server advertises on /v1/models (llama.cpp `--alias`, vLLM
>>> `--served-model-name`) — so settings for several models sit side by side and
>>> the served model selects its own. The long system-prompt bodies live further
>>> down as GROUNDING_SYSTEM / VISION_GROUNDING_SYSTEM and are referenced by the
>>> table's `prompt` key; every tunable knob is in the table.

Every preset gets the same tools (rag + placement + reboot_guarded) so whichever
engine is served can drive the infra. Only one model is served at a time; the
preset whose base_model_id matches the live alias is the usable one, and the rest
stay as stored config awaiting a model swap.

Aliases may share a `preset_id` on purpose (the 8B and 30B VL models both drive
"ragfarm-vision"), in which case the live one wins and the collapse is announced —
the upsert keys on preset_id, so writing both would otherwise be last-one-wins.

Flags:
  --only-active   write just the preset for the currently-served model.

Open WebUI stores this in its Docker volume, so it survives restarts; this script
is the reproducible source of that config (re-run any time / against a fresh UI).

Usage (from the host, with the stack up):
    OWUI_URL=http://127.0.0.1:3000 \
    OWUI_TOKEN=<admin JWT>  python3 infra/openwebui/setup_openwebui.py
  or, to sign in / bootstrap the first admin:
    OWUI_URL=http://127.0.0.1:3000 \
    OWUI_EMAIL=admin@ragfarm.local OWUI_PASSWORD=... \
    python3 infra/openwebui/setup_openwebui.py

Config knobs (env). Model settings are NOT here — they are in MODEL_TUNING:
    MCPO_RAG_URL           default http://127.0.0.1:8000/rag
    MCPO_PLACEMENT_URL     default http://127.0.0.1:8000/placement
    LLAMA_URL              default http://127.0.0.1:8080 (queried for the live alias)
    FALLBACK_ALIAS         used when the served alias matches no MODEL_TUNING entry
    TEXT_BASE_MODEL_ID     optional PIN; forces this alias instead of autodetect
    VISION_BASE_MODEL_ID   optional PIN; takes precedence over TEXT_BASE_MODEL_ID
"""
import os
import sys
import pathlib
import requests

URL = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")
MCPO_RAG_URL = os.environ.get("MCPO_RAG_URL", "http://127.0.0.1:8000/rag")
MCPO_PLACEMENT_URL = os.environ.get("MCPO_PLACEMENT_URL", "http://127.0.0.1:8000/placement")
# Optional alias PINS. Normally unset: the served alias is autodetected from
# /v1/models and matched against MODEL_TUNING. Setting either forces that alias.
# `BASE_MODEL_ID` is accepted as the historical spelling of TEXT_BASE_MODEL_ID.
TEXT_BASE_MODEL_ID_ENV = os.environ.get("TEXT_BASE_MODEL_ID") or os.environ.get("BASE_MODEL_ID")
VISION_BASE_MODEL_ID_ENV = os.environ.get("VISION_BASE_MODEL_ID")
# host-control is bridged by mcpo but deliberately NOT registered as an OWUI tool
# server; the model reaches reboot only through the reboot_guarded Python Tool.
REBOOT_TOOL_PY = pathlib.Path(__file__).with_name("tools") / "reboot_guarded.py"


# ============================================================================
# MODEL TUNING TABLE — **THE ONE PLACE TO EDIT PER-MODEL SETTINGS**
# ============================================================================
# Keyed by the model's ALIAS, i.e. exactly the id the inference server advertises
# on /v1/models (llama.cpp's --alias, vLLM's --served-model-name). Keying by alias
# — rather than by preset name — is what keeps settings for several models side by
# side: swap the served model and the matching entry is what gets applied, with no
# edits anywhere else in this file.
#
# Each entry may set:
#   preset_id         OWUI model id (stable; this is what upsert keys on)
#   name              display name in the OWUI model picker
#   description       shown under the name
#   prompt            which system-prompt body to use: see PROMPT_BODIES below
#   params            sampler + agent knobs; MERGED OVER params_common
#   capabilities      OWUI capability matrix; MERGED OVER caps_common
#   default_features  per-chat toggles pre-selected in new conversations
#   builtin_tools     OWUI opt-out map (only false-flagged keys persist)
#
# Only DIFFERENCES from the *_common blocks belong in an entry. To add a model,
# copy an entry and change the alias key — nothing below this table needs touching.
MODEL_TUNING = {
    # ---- shared defaults, merged under every entry ------------------------
    "params_common": {
        "function_calling": "native",   # schema-side, survives OWUI context compaction
        "compact_token_threshold": 24000,
        "stream_response": True,
        "stream_delta_chunk_size": 1,
        "use_mmap": True,
        "use_mlock": True,
    },
    "caps_common": {
        # Matches Workspace -> Model Advanced settings verbatim. Pinned in full
        # rather than left to default, so re-running this script can never
        # silently drop code_interpreter or usage (RULE 6 + tests/tracing need them).
        "file_context": True, "vision": False, "file_upload": True,
        "web_search": False, "image_generation": False, "code_interpreter": True,
        "terminal": False, "citations": True, "status_updates": True,
        "usage": True, "builtin_tools": True,
    },
    "builtin_tools_common": {
        # Automations/tasks/web_search OFF: they encourage a Thinking model to
        # invent multi-step iteration where the single-shot RAG call was already
        # complete (observed as multi-loop tool traps on the vision preset).
        # knowledge/calendar OFF because we do not run those backends.
        "knowledge": False, "calendar": False,
        "automations": False, "tasks": False, "web_search": False,
    },
    "default_features_common": ["code_interpreter"],

    # ---- per-model entries, keyed by served alias -------------------------
    "models": {
        # Text engine, greedy + fixed seed. Retired as the default on the Spark
        # (ADR-0013) but kept so the preset still resolves if this model is served.
        "qwen2.5-7b-instruct": {
            "preset_id": "ragfarm",
            "name": "ragfarm (corpus RAG + infra)",
            "description": "Text engine: greedy Qwen2.5-7B with corpus retrieval, "
                           "OpenNebula placement, and guarded host reboot.",
            "prompt": "text",
            "params": {
                # determinism (greedy, fixed seed)
                "temperature": 0, "top_k": 1, "top_p": 0, "min_p": 0, "seed": 42,
                "frequency_penalty": 0, "presence_penalty": 0,
                "repeat_penalty": 1, "repeat_last_n": 0,
                "mirostat": 0, "mirostat_eta": 0, "mirostat_tau": 0, "tfs_z": 1,
                # Response ceiling — without it OWUI's frontend default (~2-4k)
                # truncates long FW-rules tables mid-value.
                "max_tokens": 8192,
            },
        },

        # Vision engine (outgoing AMD box). Qwen3-VL Thinking forbids greedy decode
        # — sampler-shape knobs (top_k/top_p/min_p/seed) are deliberately ABSENT so
        # the server's own nucleus-sampling defaults apply.
        "qwen_qwen3-vl-8b-thinking": {
            "preset_id": "ragfarm-vision",
            "name": "ragfarm-vision (Qwen3-VL + infra + draw.io)",
            "description": "Vision engine: Qwen3-VL Thinking (non-greedy) with image "
                           "input, corpus RAG, placement, reboot, and draw.io rendering.",
            "prompt": "vision",
            "params": {
                "temperature": 0.6,
                # 16k: a Thinking turn must fit reasoning + tool call + full table.
                # Too low and <think> burns the budget before the tool call is emitted.
                "max_tokens": 16384,
            },
            "capabilities": {
                "vision": True,
                # file_context OFF: OWUI prepends `<attached_files>...` as TEXT in
                # ADDITION to routing image bytes through the vision encoder. That
                # dual signal put Qwen3-VL-Thinking into a metacognitive loop
                # ("am I looking at an XML description or the real image?").
                "file_context": False,
            },
        },

        # PRIMARY on the DGX Spark (ADR-0013). Served by vLLM as an NVFP4 MoE.
        # Sampler values follow Qwen3-VL guidance; re-tune against the real model —
        # a 30B MoE may need less prompt scaffolding than the 8B did.
        "qwen3-vl-30b-a3b": {
            "preset_id": "ragfarm-vision",
            "name": "ragfarm-vision (Qwen3-VL 30B-A3B + infra + draw.io)",
            "description": "Vision engine: Qwen3-VL-30B-A3B NVFP4 on vLLM with image "
                           "input, corpus RAG, placement, reboot, and draw.io rendering.",
            "prompt": "vision",
            "params": {
                "temperature": 0.6, "top_p": 0.95, "top_k": 20,
                "max_tokens": 16384,
            },
            "capabilities": {"vision": True, "file_context": False},
        },
    },
}

# Applied when the live alias matches no entry in MODEL_TUNING["models"], so a
# freshly-swapped model still gets a usable preset instead of silently none.
FALLBACK_ALIAS = os.environ.get("FALLBACK_ALIAS", "qwen3-vl-30b-a3b")


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

    "RULE 5 — coding. The code interpreter is a PYTHON (Pyodide) sandbox. It cannot run "
    "JavaScript, HTML, CSS, SQL, bash, or anything else. So:\n"
    "  - PYTHON code you write or change: do steps 1-5 below, automatically, without being asked.\n"
    "  - ANY OTHER language: do steps 1, 4 and 5 only. Output the code and stop. Do NOT call the "
    "code interpreter, do NOT invent test cases for it, and do NOT apologise for not running it — "
    "not running non-Python code is correct behaviour, not a limitation worth narrating.\n"
    "  1. Output the full current code first, in a fenced code block.\n"
    "  2. (Python only) Then call the code interpreter to run it on small canned test cases you "
    "pick (inputs with known expected outputs), with the code instrumented to measure wall time "
    "and CPU time.\n"
    "  3. (Python only) Report per-test pass/fail and the measured timings.\n"
    "  4. Offer 1-3 concrete improvement options (efficiency, algorithm, or structure) for the user "
    "to choose from.\n"
    "  5. If the user's approach is worse than an alternative, say so and explain briefly — then "
    "implement exactly what the user chose. The user's decision always overrules you."
)


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

    "RULE 0 — WHICH questions need a tool, which do not. This rule takes precedence over RULE 1 "
    "and RULE 2.\n"
    "  Tool-required questions (call a ragfarm tool — see RULE 1):\n"
    "    - anything about SPECIFIC hosts, VMs, IP addresses, VLANs, FQDNs, credentials, backup "
    "procedures, contact people, or any fact that only exists in our internal corpus / OpenNebula\n"
    "    - reboot / restart / drain operations on hosts\n"
    "  Tool-forbidden questions (answer DIRECTLY from your own vision, reasoning, or knowledge — "
    "NEVER call any tool, NEVER invent a tool name like 'describe_image' or 'analyze_image'):\n"
    "    - any question about an ATTACHED image (\"what is on this picture?\", OCR, describe, "
    "translate, extract data from a chart, regenerate as mermaid/drawio) — you have native vision, "
    "USE it. There is no image-analysis tool available; if you emit one you are hallucinating.\n"
    "    - diagram generation from a natural-language description (mermaid, draw.io) — see RULE 5\n"
    "    - coding, code review, code explanation — see RULE 6\n"
    "    - chit-chat, greetings, meta-questions about your own capabilities, arithmetic\n"
    "  If unsure, look at the user message: does it require a fact you cannot possibly know from "
    "training or from the attached image? If yes -> ragfarm tool. Otherwise -> answer directly.\n\n"

    "RULE 1 — for tool-required questions only (see RULE 0), act via tools first, silently. Pick "
    "the right tool and call it BEFORE writing anything. Never answer infrastructure questions "
    "from your own knowledge; never write text or announce the tool before calling it. Call the "
    "tool again on every NEW user question that depends on live state — never reuse a previous "
    "turn's result. Routing:\n"
    "  - documented facts (hosts, IPs, VLANs, FQDNs, access/backup/contact info, procedures) -> search_corpus\n"
    "  - where a VM runs / what runs on a host -> where_is_vm / list_vms_on_host\n"
    "  - reboot / restart / bounce a hypervisor host -> reboot_host (it asks the user to confirm)\n"
    "  Call each of these ragfarm tools AT MOST ONCE per user turn. The single call returns every "
    "relevant chunk our custom retrieval was designed to return — do not 'refine', do not "
    "'double-check', do not re-query with a shortened phrase, do not call the same tool a second "
    "time in the same turn even if the first result feels partial. If two DIFFERENT ragfarm tools "
    "are legitimately needed to answer (e.g. where_is_vm then reboot_host), one call each is fine. "
    "Multi-step iteration is for the OWUI built-in code interpreter only — never for our custom "
    "ragfarm tools. The ONLY tools that exist are: search_corpus, where_is_vm, list_vms_on_host, "
    "reboot_host, and (for RULE 6) the built-in code interpreter. Anything else is a hallucination.\n\n"

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

    "RULE 4 — images attached by the user. USE YOUR NATIVE VISION — do NOT call any tool. "
    "There is no image-analysis tool in this system; if you emit a tool call for an image task "
    "(names like 'describe_image', 'analyze_image', 'ocr_tool', 'image_url' — anything image-shaped) "
    "you are hallucinating a tool that does not exist and the request will fail. Look at the image "
    "directly and answer. Describe what is present, quoting any visible text verbatim (do not "
    "translate unless asked). Do not invent objects, labels, values, brands, or people not "
    "visible; if the image is blurry, cropped, or you are uncertain, say so instead of guessing. "
    "For OCR / receipt / form tasks, output the fields exactly as printed, keeping the original "
    "punctuation and number formatting.\n\n"

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

    "RULE 6 — coding. The code interpreter is a PYTHON (Pyodide) sandbox. It cannot run "
    "JavaScript, HTML, CSS, SQL, bash, or anything else. So:\n"
    "  - PYTHON code you write or change: do steps 1-5 below, automatically.\n"
    "  - ANY OTHER language: do steps 1, 4 and 5 only. Output the code and stop. Do NOT call the "
    "code interpreter, do NOT invent test cases for it, and do NOT apologise for not running it — "
    "not running non-Python code is correct behaviour, not a limitation worth narrating.\n"
    "  1. Output the full current code first, in a fenced code block.\n"
    "  2. (Python only) Then call the code interpreter to run it on small canned test cases you "
    "pick (inputs with known expected outputs), instrumented to measure wall time and CPU time.\n"
    "  3. (Python only) Report per-test pass/fail and the measured timings.\n"
    "  4. Offer 1-3 concrete improvement options (efficiency, algorithm, or structure).\n"
    "  5. If the user's approach is worse than an alternative, say so briefly — then implement "
    "exactly what the user chose. The user's decision always overrules you."
)


# ---------------------------------------------------------------------------
# Resolution: alias -> the fully-merged preset MODEL_TUNING describes.
# ---------------------------------------------------------------------------
# The long prose bodies stay as module-level names above (scripts/agent.py and
# scripts/trace_tool_calls.py import GROUNDING_SYSTEM by name — do not rename),
# and MODEL_TUNING selects between them by key. Prose lives below, knobs live at
# the top; that is the split.
PROMPT_BODIES = {
    "text": GROUNDING_SYSTEM,
    "vision": VISION_GROUNDING_SYSTEM,
}


def resolve_preset(alias: str) -> dict:
    """Merge MODEL_TUNING's shared defaults with the entry for `alias` and return
    a ready-to-POST preset dict. Raises KeyError for an unknown prompt body — a
    typo there is a bug, not something to paper over with a default."""
    models = MODEL_TUNING["models"]
    entry = models[alias]
    params = {**MODEL_TUNING["params_common"], **entry.get("params", {})}
    params["system"] = PROMPT_BODIES[entry["prompt"]]
    return {
        "preset_id": entry["preset_id"],
        "base_model_id": alias,
        "name": entry["name"],
        "description": entry["description"],
        "params": params,
        "capabilities": {**MODEL_TUNING["caps_common"], **entry.get("capabilities", {})},
        "default_features": entry.get("default_features", MODEL_TUNING["default_features_common"]),
        "builtin_tools": entry.get("builtin_tools", MODEL_TUNING["builtin_tools_common"]),
    }


def live_aliases() -> list[str]:
    """Aliases the inference server currently advertises on /v1/models.

    Engine-agnostic on purpose: llama.cpp reports `--alias`, vLLM reports
    `--served-model-name`, and both answer the same OpenAI-compatible route — which
    is exactly the seam ADR-0013 relies on. Returns [] if the server is unreachable,
    which is not fatal: presets are stored config and can be written ahead of the
    model being up.
    """
    try:
        r = requests.get(f"{LLAMA_URL}/v1/models", timeout=5)
        r.raise_for_status()
        payload = r.json()
        # OpenAI shape is {"data": [{"id": ...}]}; llama.cpp also emits {"models": [...]}.
        rows = payload.get("data") or payload.get("models") or []
        return [m.get("id") or m.get("model") for m in rows if (m.get("id") or m.get("model"))]
    except Exception:
        return []


def select_aliases() -> tuple[list[str], str | None]:
    """Decide which MODEL_TUNING entries to apply, and which one is live.

    Default is to write EVERY configured preset, so swapping the served model does
    not require re-running this script. `--only-active` narrows it to the live one.
    Env override: TEXT_BASE_MODEL_ID / VISION_BASE_MODEL_ID still pin a specific
    alias if set, for the historical invocations that rely on them.
    """
    models = MODEL_TUNING["models"]
    configured = list(models)

    # Env pin wins over autodetect, so the historical TEXT_BASE_MODEL_ID /
    # VISION_BASE_MODEL_ID invocations keep working: setting one forces that alias
    # to be treated as live. It must still name a configured alias — silently
    # inventing a preset for an unknown alias would defeat the table.
    pinned = next((v for v in (VISION_BASE_MODEL_ID_ENV, TEXT_BASE_MODEL_ID_ENV) if v), None)
    if pinned:
        if pinned in configured:
            print(f"NOTE: alias pinned to '{pinned}' by env; skipping autodetect.")
            return ([pinned] if "--only-active" in sys.argv else configured), pinned
        print(f"WARNING: pinned alias '{pinned}' has no MODEL_TUNING entry — ignoring it.")

    live = [a for a in live_aliases() if a in configured]
    active = live[0] if live else None

    if active is None:
        served = live_aliases()
        if served:
            print(f"NOTE: served alias(es) {served} have no MODEL_TUNING entry; "
                  f"add one keyed by that alias. Falling back to '{FALLBACK_ALIAS}'.")
            active = FALLBACK_ALIAS if FALLBACK_ALIAS in configured else None
        else:
            print(f"NOTE: {LLAMA_URL} unreachable — writing all presets blind; "
                  f"none marked live.")

    if "--only-active" in sys.argv:
        return ([active] if active else []), active

    # Several aliases may legitimately share a preset_id — e.g. the 8B and 30B VL
    # models both drive "ragfarm-vision", so the UI keeps ONE vision preset that
    # follows whichever model is served. Since the upsert keys on preset_id, writing
    # both would mean "last one wins" silently. Collapse per preset_id, preferring
    # the live alias, and say so rather than resolving it invisibly.
    by_preset: dict[str, list[str]] = {}
    for a in configured:
        by_preset.setdefault(models[a]["preset_id"], []).append(a)

    chosen = []
    for preset_id, group in by_preset.items():
        pick = active if (active in group) else group[0]
        if len(group) > 1:
            others = [a for a in group if a != pick]
            why = "live" if pick == active else "first configured; none of them is live"
            print(f"NOTE: preset '{preset_id}' is claimed by {len(group)} aliases "
                  f"{group}; writing '{pick}' ({why}), skipping {others}.")
        chosen.append(pick)
    return chosen, active


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

    # 3. Create/update the model presets described by MODEL_TUNING. Same tools on
    #    every preset so whichever model is served can drive the infra; only the
    #    preset whose base_model_id matches the live alias is usable right now.
    tool_ids = server_ids + ["reboot_guarded"]

    aliases, active = select_aliases()
    if not aliases:
        sys.exit("No presets to write (--only-active with no live/known alias).")

    for alias in aliases:
        p = resolve_preset(alias)
        body = {
            "id": p["preset_id"],
            "base_model_id": p["base_model_id"],
            "name": p["name"],
            "meta": {
                "description": p["description"],
                "toolIds": tool_ids,
                "capabilities": p["capabilities"],
                "defaultFeatureIds": p["default_features"],
                "builtinTools": p["builtin_tools"],
            },
            "params": p["params"],
            "access_grants": [],
            "is_active": True,
        }
        # Idempotent upsert: create, and on any non-200 fall back to update. Same
        # contract as before — re-running this script converges rather than dupes.
        r = requests.post(URL + "/api/v1/models/create", headers=H, json=body, timeout=30)
        if r.status_code != 200:
            r = requests.post(URL + "/api/v1/models/model/update", headers=H, json=body, timeout=30)
        r.raise_for_status()
        mark = "  <- LIVE" if alias == active else ""
        print(f"preset '{p['preset_id']}' ready (alias={alias}, tools={tool_ids}){mark}")

    if active is None:
        print("WARNING: no preset matches a served model — check the alias the "
              "inference server advertises against MODEL_TUNING['models'] keys.")


if __name__ == "__main__":
    main()
