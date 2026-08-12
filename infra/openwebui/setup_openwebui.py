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
    RAGFARM_PUBLIC_HOST    address a REMOTE BROWSER uses to reach this box; default
                           autodetected from the default route. Only affects the
                           draw.io wrapper URLs in RULE 5, which the client fetches.
    DRAWIO_VIEWER_URL      full override of the drawio-viewer base URL (default
                           http://$RAGFARM_PUBLIC_HOST, i.e. compose's 0.0.0.0:80)
    FALLBACK_ALIAS         used when the served alias matches no MODEL_TUNING entry
    TEXT_BASE_MODEL_ID     optional PIN; forces this alias instead of autodetect
    VISION_BASE_MODEL_ID   optional PIN; takes precedence over TEXT_BASE_MODEL_ID
"""
import json
import os
import sys
import pathlib
import requests
from pathlib import Path

URL = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")
MCPO_RAG_URL = os.environ.get("MCPO_RAG_URL", "http://127.0.0.1:8000/rag")
MCPO_PLACEMENT_URL = os.environ.get("MCPO_PLACEMENT_URL", "http://127.0.0.1:8000/placement")


# ---------------------------------------------------------------------------
# PUBLIC HOST — the address a REMOTE BROWSER uses to reach this box.
# ---------------------------------------------------------------------------
# Everything else in this file is server-side and rightly says 127.0.0.1. The
# draw.io wrapper in RULE 5 is the one exception: those URLs are fetched by the
# user's browser, inside OWUI's HTML-preview iframe. On a remote client
# 127.0.0.1 is the CLIENT's own loopback, so viewer-static.min.js never loads
# and the preview pane renders blank — no error, just an empty box.
#
# Autodetected from the route to the default gateway (a UDP "connect" sets up
# no traffic, it only asks the kernel which source address it would use), so a
# DHCP change is picked up on the next activation without anyone editing a
# file. Override in .env when autodetect picks the wrong face: multi-homed box,
# or clients that reach us via VPN/NAT/a DNS name rather than the LAN address.
def _detect_public_host() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: routed nowhere, sends nothing
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


PUBLIC_HOST = os.environ.get("RAGFARM_PUBLIC_HOST") or _detect_public_host()
# Base URL of the drawio-viewer nginx (compose binds it to 0.0.0.0:80).
VIEWER_BASE = os.environ.get("DRAWIO_VIEWER_URL", f"http://{PUBLIC_HOST}").rstrip("/")

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

    # ---- PROFILES: tuning selected by models/llm/active.json's `profile` ----
    # The registry owns IDENTITY (model dir, alias, preset id, display name); this
    # owns TUNING (prompt, sampler, capabilities). Splitting them keeps the ~200-line
    # prompt bodies out of JSON while letting a newly fetched model inherit a known
    # -good configuration by naming one word. `models` above stays for aliases that
    # predate the registry.
    "profiles": {
        "vision-thinking": {
            "prompt": "vision",
            # Qwen3-VL Thinking forbids greedy decode. These are Qwen's own
            # recommended Thinking values (the Instruct checkpoint asks for
            # 0.7/0.8 instead — see models/llm/MODEL.md).
            "params": {
                "temperature": 0.6, "top_p": 0.95, "top_k": 20,
                # A Thinking turn must fit reasoning + tool call + the full table.
                # Too low and <think> burns the budget before the tool call lands.
                #
                # Sized against draw.io generation, the hungriest task we have.
                # Measured 2026-08-09 on a 24-entity ER diagram: 13,317 completion
                # tokens, of which ~9,000 were REASONING — the diagram itself was
                # only ~4,400. Reasoning and answer share this one budget, which is
                # why "16k tokens vs 14k characters of XML" looks like plenty and
                # is not: real draw.io XML tokenises at 2.73 chars/token, so 14k
                # chars is ~5.1k tokens, and the think comes out of the same purse.
                #
                # CEILING: prompt + max_tokens must stay under --max-model-len
                # (32768). A vision turn's prompt is ~5.5k (3.5k system + image +
                # question), so 24576 fits a first turn with ~2.5k to spare but
                # NOT a long follow-up thread. Start a fresh chat for big diagram
                # conversions. Raising this further means raising --max-model-len,
                # which costs KV cache (~96 KiB/token) and a slot restart.
                "max_tokens": 8192,
            },
            # file_context OFF: OWUI prepends `<attached_files>...` as TEXT in
            # ADDITION to routing image bytes through the vision encoder, and that
            # dual signal put Qwen3-VL Thinking into a metacognitive loop.
            "capabilities": {"vision": True, "file_context": False},
        },
        "vision-instruct": {
            # Same system prompt as vision-thinking, deliberately: nothing in
            # RULE 0-6 depends on whether the model emits a <think> block, and
            # keeping them identical is what makes Instruct-vs-Thinking a clean
            # single-variable comparison.
            "prompt": "vision",
            "params": {
                # Qwen's recommended sampling for the INSTRUCT checkpoint, which
                # is not the same as Thinking's 0.6/0.95. This profile carried
                # the Thinking values until 2026-08-09 — contradicting the note
                # in vision-thinking that says Instruct asks for 0.7/0.8.
                "temperature": 0.7, "top_p": 0.8, "top_k": 20,
                # Instruct spends nothing on reasoning, so the whole budget is
                # answer. Same ceiling arithmetic as vision-thinking: a ~5.1k
                # vision prompt plus 24576 fits under --max-model-len 32768 for a
                # first turn, not for a long thread.
                #
                # Do NOT read this as headroom that fixes hard diagrams. Measured
                # 2026-08-09 on tests/fixtures/Splunk_in_KB.png, three runs
                # (16384, 24576, and 24576 with presence_penalty 1.5): all three
                # ran the budget dry inside an EDGE REPETITION LOOP. Vertices come
                # out right — 23-24 of them, well laid out, which is why the
                # render looks far better than Thinking's — and then it emits 312+
                # edges for a source that has ~26, most of them duplicates. More
                # budget buys more loop, and presence_penalty does not break it.
                # The failure is on the edge half of the task, not the node half.
                "max_tokens": 8192,
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

    "RULE 3 — how to PRESENT search_corpus records. FIRST decide what the user actually asked "
    "for, because the two cases need different answers:\n"
    "  (a) TARGETED question — the user asks about ONE entity, or narrows explicitly ('only', "
    "'just', 'which one of', 'who is the X'). Answer with THAT ONE record and nothing else. "
    "Retrieval deliberately returns extra candidates so you can CHOOSE; returning all of them is "
    "not thoroughness, it is failing to answer. If several records look plausible, pick the one "
    "whose fields actually satisfy the question (e.g. for 'who is the PM', the record whose "
    "role/position field says PM — not every person from the same company) and give only that. "
    "Never pad a targeted answer with the other candidates.\n"
    "  (b) OVERVIEW question — the user wants a list ('list all', 'which hosts', 'what are the', "
    "'give me the rules for'). THEN use the full table:\n"
    "    1. Collect every distinct key that appears in those records. Each distinct key becomes "
    "one column. Copy keys verbatim — never translate, shorten, rename, merge, or invent a column "
    "(do not add a row-number column).\n"
    "    2. One table row per record. Put each value under its own key's column; leave a cell "
    "empty only if that record truly lacks that key.\n"
    "    3. Include every key and every value from the records you show — do not omit a column or "
    "drop a field.\n"
    "  In BOTH cases: quote values verbatim, and the line `Source: <source_file>` (value from the "
    "records' `source_file` field) must appear exactly once in the whole reply, as the very last "
    "line. Never write the word \"Source\" anywhere above it.\n\n"

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
    "reboot_host, and (for RULE 6) the built-in code interpreter. Anything else is a hallucination.\n"
    "  PARAMETERS — one call, so make it a WIDE one. 'Call it once' does NOT mean 'ask for as "
    "little as possible'. Never lower a tool's default limit to be economical: retrieval is cheap, "
    "a missed record is expensive, and the reranker already throws away what is irrelevant, so a "
    "larger result set costs you nothing in answer quality.\n"
    "    - search_corpus `k`: NEVER pass k=1. Leave k at its default of 8 unless you have a "
    "concrete factual reason to change it, and RAISE it (12-20) for 'list all' / 'who are' / "
    "'which hosts' questions where many distinct records legitimately match. Narrowing k does NOT "
    "make an answer more precise — it makes it far more likely you answer confidently from the "
    "one wrong record you happened to receive.\n"
    "  QUERY WORDING — write a specific, keyword-rich query, not a terse one. Carry over the "
    "concrete nouns from the user's question (company, role, hostname, service, document) so that "
    "BOTH retrieval branches have something to match: the exact-token branch needs the literal "
    "identifiers, the semantic branch needs the surrounding words. A query like "
    "'projektovy manazer EPC' is weaker than 'projektovy manazer PM firma EPC rizeni projektu'.\n"
    "  EMIT THE CALL, DO NOT DESCRIBE IT. Deliberating about which tool to use, quoting a rule "
    "back to yourself, or writing out what the call would look like belongs in your reasoning, "
    "never in the reply. The user's reply must never contain the words 'tool_call', 'RULE 0', "
    "'RULE 1', or a narration of your own decision process. Either you emit a real tool call, or "
    "you answer — there is no third option where you talk about calling one.\n\n"

    "RULE 2 — answer only from tool results. Use only what the tool returned; never generalize "
    "or invent. Quote values verbatim (hostnames, IPs, VLANs, FQDNs, VM names, steps). If a tool "
    "says it cannot find or do something, say exactly that. Reply in the same language as the "
    "question (Czech question -> Czech answer).\n\n"

    "RULE 3 — how to PRESENT search_corpus records. FIRST decide what the user actually asked "
    "for, because the two cases need different answers:\n"
    "  (a) TARGETED question — the user asks about ONE entity, or narrows explicitly ('only', "
    "'just', 'which one of', 'who is the X'). Answer with THAT ONE record and nothing else. "
    "Retrieval deliberately returns extra candidates so you can CHOOSE; returning all of them is "
    "not thoroughness, it is failing to answer. If several records look plausible, pick the one "
    "whose fields actually satisfy the question (e.g. for 'who is the PM', the record whose "
    "role/position field says PM — not every person from the same company) and give only that. "
    "Never pad a targeted answer with the other candidates.\n"
    "  (b) OVERVIEW question — the user wants a list ('list all', 'which hosts', 'what are the', "
    "'give me the rules for'). THEN use the full table:\n"
    "    1. Collect every distinct key that appears in those records. Each distinct key becomes "
    "one column. Copy keys verbatim — never translate, shorten, rename, merge, or invent a column "
    "(do not add a row-number column).\n"
    "    2. One table row per record. Put each value under its own key's column; leave a cell "
    "empty only if that record truly lacks that key.\n"
    "    3. Include every key and every value from the records you show — do not omit a column or "
    "drop a field.\n"
    "  In BOTH cases: quote values verbatim, and the line `Source: <source_file>` (value from the "
    "records' `source_file` field) must appear exactly once in the whole reply, as the very last "
    "line. Never write the word \"Source\" anywhere above it.\n\n"

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
    "renders it in-chat (the window.*_PATH overrides point the viewer at our LOCAL mirror of the "
    "draw.io webapp — no external fetches, so the diagram renders even air-gapped). Copy the "
    "wrapper verbatim, including every URL: they are already correct, do not change them to "
    "localhost, to a CDN, or to anything else.\n"
    "\n"
    "  THE FENCE IS MANDATORY AND COMES FIRST. The very first characters of your reply are "
    "```html on its own line — no preamble, no blank lines, no explanation before it — and the "
    "reply ends with the closing ``` plus at most one caption sentence. Raw HTML with no fence "
    "does not render: Open WebUI only turns a FENCED html block into a diagram pane, so an "
    "unfenced reply shows the user nothing at all. This is easy to forget on a long diagram, "
    "after a long think. Open the fence before you write anything else.\n"
    "```html\n"
    "<!DOCTYPE html>\n"
    "<html><head><meta charset=\"utf-8\">\n"
    "<style>body{margin:0;padding:10px;background:#fff}"
    ".mxgraph{width:100%;height:500px;border:1px solid #ccc;border-radius:6px}</style>\n"
    "</head><body>\n"
    "<div class=\"mxgraph\" id=\"ragfarm-graph\"></div>\n"
    "<script type=\"application/xml\" id=\"ragfarm-xml\">\n"
    "  <mxfile host=\"app.diagrams.net\">\n"
    "    <diagram name=\"example\" id=\"d0\">\n"
    "      <mxGraphModel dx=\"800\" dy=\"600\" grid=\"1\" gridSize=\"10\" guides=\"1\" tooltips=\"1\" "
    "connect=\"1\" arrows=\"1\" fold=\"1\" page=\"1\" pageScale=\"1\" pageWidth=\"850\" pageHeight=\"600\" "
    "math=\"0\" shadow=\"0\">\n"
    "        <root>\n"
    "          <mxCell id=\"0\" />\n"
    "          <mxCell id=\"1\" parent=\"0\" />\n"
    "          <mxCell id=\"n1\" value=\"First box\" style=\"rounded=1;whiteSpace=wrap;html=1;\" "
    "vertex=\"1\" parent=\"1\">\n"
    "            <mxGeometry x=\"80\" y=\"120\" width=\"140\" height=\"60\" as=\"geometry\"/>\n"
    "          </mxCell>\n"
    "          <mxCell id=\"n2\" value=\"Second box\" style=\"rounded=1;whiteSpace=wrap;html=1;\" "
    "vertex=\"1\" parent=\"1\">\n"
    "            <mxGeometry x=\"300\" y=\"120\" width=\"140\" height=\"60\" as=\"geometry\"/>\n"
    "          </mxCell>\n"
    "          <mxCell id=\"e1\" style=\"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;"
    "endArrow=classic;\" edge=\"1\" parent=\"1\" source=\"n1\" target=\"n2\">\n"
    "            <mxGeometry relative=\"1\" as=\"geometry\"/>\n"
    "          </mxCell>\n"
    "        </root>\n"
    "      </mxGraphModel>\n"
    "    </diagram>\n"
    "  </mxfile>\n"
    "</script>\n"
    f"<script src=\"{VIEWER_BASE}/ragfarm-drawio.js\"></script>\n"
    "</body></html>\n"
    "```\n"
    "That is the WHOLE wrapper — eleven lines, and every one of them is required. The only part "
    "you author is the XML between the two `ragfarm-xml` script tags: it must start with "
    "`<mxfile>`, end with `</mxfile>`, and be written RAW — no backslash escaping, no JSON "
    "string, no HTML entities, no nested code fence. The last `<script src=...>` line is what "
    "renders it; a page without that line shows the user nothing, so never drop it and never "
    "replace it with viewer-static.min.js or any other URL. One block, one caption, then stop — "
    "never a second block, never the other format next to it.\n"
    "\n"
    "  draw.io XML — four rules the viewer enforces silently. Break any one of them and the pane "
    "renders BLANK with no error message, so there is no feedback to correct against: get them "
    "right the first time.\n"
    "    1. WRITE EVERY ATTRIBUTE OUT IN FULL. Never abbreviate any part of the XML with `...`, "
    "`… />`, `<!-- unchanged -->`, \"same as above\", or any other elision — not for style strings, "
    "not for geometry, not for repeated cells. This is machine-parsed input, not a summary for a "
    "human reader; a `...` is a syntax error, not a shorthand. If the diagram is long, write it "
    "long.\n"
    "    2. `id=\"0\"` and `id=\"1\"` are RESERVED for the root and the default layer, exactly as "
    "in the wrapper above. Never give a box or an arrow one of those two ids. Use descriptive ids "
    "(`n1`, `db`, `e1`).\n"
    "    3. EVERY box and arrow needs `parent=\"1\"` plus its own `<mxGeometry>` child — a box "
    "needs `vertex=\"1\"` and `x`/`y`/`width`/`height`, an arrow needs `edge=\"1\"`, `source`, "
    "`target` and `relative=\"1\"`. A cell without geometry has no position and no size, so it "
    "draws nothing.\n"
    "    4. TRANSFORMING a diagram the user supplied (reverse the arrows, rename a node, add a "
    "box): start from THEIR XML and change only what they asked for. Keep their ids, styles, "
    "geometry, labels and `mxGraphModel` attributes byte-for-byte — reversing an arrow means "
    "swapping that edge's `source` and `target` and nothing else. Re-deriving the diagram from "
    "scratch loses their layout and colours, which is a wrong answer even if it looks fine.\n\n"

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


REGISTRY_PATH = Path(__file__).resolve().parent.parent.parent / "models" / "llm" / "active.json"


def load_registry() -> dict | None:
    """models/llm/active.json, or None if this deployment predates it."""
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        return None


def registry_presets(reg: dict) -> list[dict]:
    """One ready-to-POST preset per ACTIVE slot.

    Identity comes from the registry, tuning from MODEL_TUNING['profiles'].
    Only active[] entries are written: a downloaded-but-unbound model has no
    endpoint serving it, and a preset pointing at nothing is the trap that made
    an earlier session's first chat answer ungrounded.
    """
    out, dl = [], reg.get("downloaded", [])
    for slot, idx in enumerate(reg.get("active", [])):
        if not isinstance(idx, int) or not (0 <= idx < len(dl)):
            continue
        e = dl[idx]
        prof = MODEL_TUNING["profiles"].get(e.get("profile"))
        if prof is None:
            print(f"NOTE: slot {slot} model {e['model']} names unknown profile "
                  f"{e.get('profile')!r} — skipping (add it to MODEL_TUNING['profiles'])")
            continue
        params = {**MODEL_TUNING["params_common"], **prof.get("params", {})}
        params["system"] = PROMPT_BODIES[prof["prompt"]]
        out.append({
            "preset_id": e["preset"],
            "base_model_id": e["alias"],
            "name": f"{e['preset']} ({e['display']})",
            "description": e.get("comment") or e["display"],
            "params": params,
            "capabilities": {**MODEL_TUNING["caps_common"], **prof.get("capabilities", {})},
            "default_features": MODEL_TUNING["default_features_common"],
            "builtin_tools": MODEL_TUNING["builtin_tools_common"],
            "_slot": slot,
        })
    return out


def live_aliases() -> list[str]:
    """Aliases the inference server currently advertises on /v1/models.

    Engine-agnostic on purpose: llama.cpp reports `--alias`, vLLM reports
    `--served-model-name`, and both answer the same OpenAI-compatible route — which
    is exactly the seam ADR-0013 relies on. Returns [] if the server is unreachable,
    which is not fatal: presets are stored config and can be written ahead of the
    model being up.
    """
    found: list[str] = []
    # Every slot, not just :8080 — with two slots live the second model is served
    # by its own vLLM process on its own port (vLLM serves ONE base model per
    # process), so a single-endpoint probe would report it missing.
    for url in slot_urls():
        try:
            r = requests.get(f"{url}/v1/models", timeout=5)
            r.raise_for_status()
            payload = r.json()
            # OpenAI shape is {"data": [{"id": ...}]}; llama.cpp also emits {"models": [...]}.
            rows = payload.get("data") or payload.get("models") or []
            found += [m.get("id") or m.get("model") for m in rows
                      if (m.get("id") or m.get("model"))]
        except Exception:
            continue
    return found


def slot_urls() -> list[str]:
    """Base URLs for every configured slot. Mirrors activate_llm.py's port map
    (slot N -> 8080 + 2N; 8081 is the reranker). LLAMA_URL stays first so a
    single-slot deployment behaves exactly as before."""
    urls = [LLAMA_URL]
    reg = load_registry()
    if reg:
        for slot, idx in enumerate(reg.get("active", [])):
            if not isinstance(idx, int):
                continue
            u = f"http://127.0.0.1:{8080 + 2 * slot}"
            if u not in urls:
                urls.append(u)
    return urls


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


def check_drawio_viewer() -> None:
    """Verify the draw.io mirror is actually reachable at the URL we just baked
    into RULE 5, and warn if PUBLIC_HOST was guessed rather than configured.

    Both failure modes this catches are SILENT at runtime — OWUI's preview pane
    renders an empty white box, no error in the UI, none in any log:

      1. Mirror not rehydrated. infra/drawio-viewer/ is 153 MB and gitignored,
         so a fresh clone has only the two tracked HTML files and nginx 404s
         js/viewer-static.min.js. Fix: scripts/fetch-drawio-viewer.sh.
      2. RAGFARM_PUBLIC_HOST unset. We autodetect and bake the right URL, but
         compose cannot autodetect, so IFRAME_CSP falls back to 127.0.0.1-only
         and the browser refuses the load we just configured.
    """
    probe = f"{VIEWER_BASE}/js/viewer-static.min.js"
    try:
        rc = requests.head(probe, timeout=5).status_code
    except requests.RequestException as e:
        rc = f"unreachable ({e.__class__.__name__})"
    if rc == 200:
        print(f"drawio viewer OK at {VIEWER_BASE}")
    else:
        print(f"WARNING: draw.io viewer NOT serving at {probe} ({rc}).\n"
              "         RULE 5 diagrams will render as an EMPTY pane with no error.\n"
              "         If the mirror is missing: scripts/fetch-drawio-viewer.sh")
    if not os.environ.get("RAGFARM_PUBLIC_HOST"):
        print(f"WARNING: RAGFARM_PUBLIC_HOST unset — autodetected {PUBLIC_HOST}.\n"
              "         compose cannot autodetect, so IFRAME_CSP is pinned to 127.0.0.1\n"
              "         and will BLOCK that URL for any remote browser. Set it in .env,\n"
              "         then: source scripts/proxy-env.sh &&"
              " docker compose -f infra/compose.yaml up -d open-webui")


def main() -> None:
    tok = get_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    check_drawio_viewer()

    # 1. Register the read-only mcpo tool servers, in order -> server:0, server:1.
    #    host-control is intentionally absent so the model cannot call reboot directly.
    conns = [
        _tool_server(MCPO_RAG_URL, "rag", "Corpus hybrid retrieval (search_corpus)"),
        _tool_server(MCPO_PLACEMENT_URL, "placement", "OpenNebula placement (where_is_vm, list_vms_on_host)"),
    ]
    r = requests.post(URL + "/api/v1/configs/tool_servers", headers=H,
                      json={"TOOL_SERVER_CONNECTIONS": conns}, timeout=30)
    r.raise_for_status()

    # 1b. Point OWUI at EVERY vLLM slot.
    #     This must go through the API, not compose env: OWUI seeds
    #     OPENAI_API_BASE_URLS from the environment on FIRST start only and then
    #     persists it in its own DB, so a compose change alone leaves an existing
    #     deployment on one endpoint — the second model silently never appears in
    #     the model list, which is exactly what breaks mid-chat model switching.
    urls = [f"{u}/v1" for u in slot_urls()]
    if len(urls) > 1:
        r = requests.post(URL + "/openai/config/update", headers=H, timeout=30, json={
            "ENABLE_OPENAI_API": True,
            "OPENAI_API_BASE_URLS": urls,
            "OPENAI_API_KEYS": ["sk-no-auth-local"] * len(urls),
            "OPENAI_API_CONFIGS": {},
        })
        r.raise_for_status()
        print(f"OpenAI endpoints registered: {urls}")
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

    # Registry-driven when models/llm/active.json exists: one preset per ACTIVE
    # slot, identity from the registry and tuning from MODEL_TUNING['profiles'].
    # Falls back to the legacy alias table for deployments without a registry.
    reg = load_registry()
    presets = registry_presets(reg) if reg else []
    if presets:
        served = set(live_aliases())
        for p in presets:
            slot = p.pop("_slot")
            mark = "  <- LIVE" if p["base_model_id"] in served else "  (slot not serving yet)"
            print(f"preset '{p['preset_id']}' <- slot {slot} "
                  f"alias={p['base_model_id']}{mark}")
        aliases, active = None, None
    else:
        aliases, active = select_aliases()
        if not aliases:
            sys.exit("No presets to write (--only-active with no live/known alias).")
        presets = [resolve_preset(a) for a in aliases]

    for p in presets:
        alias = p["base_model_id"]
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
        mark = "  <- LIVE" if (active and alias == active) else ""
        print(f"preset '{p['preset_id']}' ready (alias={alias}, tools={tool_ids}){mark}")

    prune_stale_presets(H, {p["preset_id"] for p in presets})

    if aliases is not None and active is None:
        print("WARNING: no preset matches a served model — check the alias the "
              "inference server advertises against MODEL_TUNING['models'] keys.")


def prune_stale_presets(H: dict, keep: set[str]) -> None:
    """Delete presets we previously created whose model is no longer served.

    A preset outlives the slot binding that produced it: it is a row in OWUI's
    database, not a view of the registry. So every model swap leaves the old
    preset behind, still listed in the picker, still selectable — and it fails at
    the first message, because its base_model_id names an alias nothing serves.
    By 2026-08-12 there were four of those against two live ones, which is a
    demo waiting to go wrong.

    SCOPE IS DELIBERATELY NARROW. Only ids this script is known to have created
    are candidates: the preset field of every registry entry, plus the ids in
    MODEL_TUNING["models"]. Anything a human made in the UI is never touched, no
    matter how dead it looks — guessing wrong here destroys someone's work, and
    the cost of leaving one stale preset is a confusing picker, not lost data.
    """
    reg = load_registry()
    ours = {e["preset"] for e in (reg or {}).get("downloaded", []) if e.get("preset")}
    ours |= {m.get("preset_id") for m in MODEL_TUNING["models"].values() if m.get("preset_id")}
    ours -= keep
    if not ours:
        return

    # /api/v1/models/list, NOT /api/v1/models/ — the trailing-slash route collides
    # with the SPA and returns the HTML shell with a 200, so a naive .json() blows
    # up rather than 404ing. The router source says so in a comment. Paginated.
    existing: set[str] = set()
    for page in range(1, 21):
        r = requests.get(URL + "/api/v1/models/list", headers=H,
                         params={"page": page}, timeout=30)
        if not r.ok:
            print("WARNING: could not list presets to prune — stale ones may remain")
            return
        items = r.json().get("items", [])
        if not items:
            break
        existing |= {m.get("id") for m in items}

    for pid in sorted(ours & existing):
        d = requests.post(URL + "/api/v1/models/model/delete", headers=H,
                          json={"id": pid}, timeout=30)
        if d.ok:
            print(f"pruned stale preset '{pid}' (its model is not served)")
        else:
            print(f"WARNING: could not prune '{pid}': {d.status_code}")


if __name__ == "__main__":
    main()
