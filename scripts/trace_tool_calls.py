#!/usr/bin/env python3
"""
trace_tool_calls.py — for a set of prompts, show WHICH tools the model calls and
with WHAT arguments, without executing anything (canned tool responses).

WHY: tool routing is the fragile part of a small (7B) agent. This drives
llama-server directly with (a) the SAME tool schemas OWUI presents — pulled live
from mcpo's per-mount openapi, so the function names are the real operationIds
(e.g. tool_search_corpus_post) — and (b) the deployed grounding system prompt, at
deterministic settings. For each prompt it prints every tool call the model makes
(name + JSON arguments) across up to --rounds turns; after each call it feeds a
CANNED tool result so you can watch the model chain tools, then prints the final
text. Great for regression-checking routing after a prompt or schema change.

USAGE
  .venv/bin/python scripts/trace_tool_calls.py
  .venv/bin/python scripts/trace_tool_calls.py "kde běží vm sftp-gw" "reboot host X"
  MOUNTS=rag,placement .venv/bin/python scripts/trace_tool_calls.py --rounds 4

ENV (defaults match the loopback deployment):
  LLM_URL=http://127.0.0.1:8080   MCPO_URL=http://127.0.0.1:8000
  MODEL=qwen2.5-7b-instruct        MOUNTS=rag,placement,host-control
Only needs `requests`. The grounding system prompt is imported from
infra/openwebui/setup_openwebui.py (override with --system-file, or --no-system).
"""
import os
import sys
import json
import pathlib
import argparse

try:
    import requests
except ImportError as e:
    sys.exit(f"missing dep ({e}); run with the project venv: .venv/bin/python {sys.argv[0]} ...")

REPO = pathlib.Path(__file__).resolve().parent.parent
LLM_URL  = os.environ.get("LLM_URL", "http://127.0.0.1:8080").rstrip("/")
MCPO_URL = os.environ.get("MCPO_URL", "http://127.0.0.1:8000").rstrip("/")
MODEL    = os.environ.get("MODEL", "qwen2.5-7b-instruct")
MOUNTS   = os.environ.get("MOUNTS", "rag,placement,host-control").split(",")

# deterministic sampler, mirroring the deployed model preset (ADR-0007)
DET = {"temperature": 0, "top_k": 1, "top_p": 0, "seed": 42, "stream": False}

DEFAULT_PROMPTS = [
    "Jak se přihlásím do EPC?",
    "kde běží vm sftp-gw",
    "co běží na hostiteli hsmbvxip001ts",
    "rebootni host hsmbvxip001ts",
    "Ahoj, jak se máš?",            # should NOT call a tool
]


def _deref(node, root, seen=()):
    """Inline local $ref so tool `parameters` is a self-contained JSON Schema
    (the model endpoint won't resolve #/components/... references)."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                return {}
            target = root
            for part in ref.lstrip("#/").split("/"):
                target = target.get(part, {})
            return _deref(target, root, seen + (ref,))
        return {k: _deref(v, root, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(v, root, seen) for v in node]
    return node


def load_tools() -> list:
    """Build OpenAI-format tools from every mcpo mount's openapi, using the exact
    operationId as the function name (what the model actually sees)."""
    tools = []
    for m in MOUNTS:
        m = m.strip()
        if not m:
            continue
        try:
            spec = requests.get(f"{MCPO_URL}/{m}/openapi.json", timeout=10).json()
        except Exception as e:
            print(f"  ! skip mount {m!r}: {e}", file=sys.stderr)
            continue
        for path, methods in spec.get("paths", {}).items():
            for method, op in methods.items():
                name = op.get("operationId") or f"{m}_{path.strip('/')}"
                schema = {}
                body = op.get("requestBody", {}).get("content", {}).get("application/json", {})
                if "schema" in body:
                    schema = _deref(body["schema"], spec)
                tools.append({"type": "function", "function": {
                    "name": name,
                    "description": (op.get("description") or op.get("summary") or "").strip(),
                    "parameters": schema or {"type": "object", "properties": {}},
                }})
    return tools


def system_prompt(args) -> str | None:
    if args.no_system:
        return None
    if args.system_file:
        return pathlib.Path(args.system_file).read_text()
    # import the deployed grounding prompt without running setup's main()
    sys.path.insert(0, str(REPO / "infra" / "openwebui"))
    try:
        import setup_openwebui
        return setup_openwebui.GROUNDING_SYSTEM
    except Exception as e:
        print(f"  ! no system prompt ({e}); running without one", file=sys.stderr)
        return None


def trace(prompt: str, tools: list, system: str | None, rounds: int):
    print(f"\n=== {prompt!r} ===")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    for r in range(1, rounds + 1):
        body = {"model": MODEL, "messages": msgs, "tools": tools, "tool_choice": "auto", **DET}
        resp = requests.post(f"{LLM_URL}/v1/chat/completions", json=body, timeout=180)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            text = (msg.get("content") or "").strip()
            print(f"  [round {r}] ANSWER: {text[:300]}")
            return
        msgs.append(msg)
        for c in calls:
            fn = c["function"]
            print(f"  [round {r}] CALL {fn['name']}({fn.get('arguments','')})")
            # canned result so the trace can continue to the next tool / final answer
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""),
                         "content": json.dumps({"note": "CANNED — tool not executed", "results": []})})
    print(f"  ! still calling tools after {rounds} rounds (stopped)")


def main():
    ap = argparse.ArgumentParser(description="trace model tool-call routing with canned responses")
    ap.add_argument("prompts", nargs="*", help="prompts (default: a built-in set)")
    ap.add_argument("--rounds", type=int, default=3, help="max tool-call rounds per prompt (default 3)")
    ap.add_argument("--system-file", help="use this file as the system prompt")
    ap.add_argument("--no-system", action="store_true", help="send no system prompt")
    ap.add_argument("--show-tools", action="store_true", help="print the loaded tool names + exit")
    args = ap.parse_args()

    tools = load_tools()
    if args.show_tools:
        for t in tools:
            print(t["function"]["name"], "->", t["function"]["description"][:70])
        return
    if not tools:
        sys.exit("no tools loaded from mcpo — is the stack up? (check MCPO_URL / MOUNTS)")
    print(f"model={MODEL} tools={[t['function']['name'] for t in tools]}")

    system = system_prompt(args)
    for p in (args.prompts or DEFAULT_PROMPTS):
        trace(p, tools, system, args.rounds)


if __name__ == "__main__":
    main()
