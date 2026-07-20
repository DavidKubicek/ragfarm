#!/usr/bin/env python3
"""
agent.py — headless multi-turn agent harness over the SAME stack OWUI drives
(llama-server + mcpo tools + the deployed grounding prompt), but with a context
loop WE own, measure, and debug. It is a control/benchmark first (prove whether a
bug is OWUI's loop or the model/stack) and a genuinely useful CLI agent second.

WHY THIS EXISTS
  OWUI's context management is a black box: its compaction fires unpredictably,
  yo-yos the context size, and — critically — drops the native reboot_host tool
  from the spec after a summarization pass, so a mutating action silently narrates
  a fake success. reboot is only the canary; the real problem is a loop we neither
  control nor can instrument. This harness removes OWUI as a variable while keeping
  EVERYTHING else byte-identical (same model, same deterministic sampler, same mcpo
  tool schemas pulled live, same grounding system prompt). If a scenario stays
  green here where OWUI goes red, OWUI's loop is the culprit — definitively.

WHAT IT DOES DIFFERENTLY FROM trace_tool_calls.py
  trace_tool_calls.py feeds CANNED tool results and never executes. This EXECUTES
  tools for real (mcpo /rag, /placement; reboot via a CLI confirm gate that mirrors
  reboot_guarded.py), carries real multi-turn history, and implements ADR-0007 §3
  the way it was specified but OWUI does not:
    - tool SCHEMAS are always present (cheap, never evicted) — they can't be
      "compacted away", so tool-calling never silently degrades;
    - context is bounded by EVICTING THE OLDEST TOOL-RESULT BODIES (never
      summarizing), preserving the tool_call<->result pairing and recent turns;
    - every turn is measured and SPLIT INTO STAGES — deliberate (decide + emit the
      tool call) vs tool execution vs answer generation — each with its own prefill
      and decode timing: the same three phases OWUI shows, but with real numbers.
      Multiple tool calls per turn are supported, both parallel (several tool_calls
      in one step) and chained (tool -> result -> another tool across rounds).

MODES
  interactive REPL (default):   .venv/bin/python scripts/agent.py
  scripted benchmark:           .venv/bin/python scripts/agent.py --scenario reboot-canary --yes
  one-shot prompts:             .venv/bin/python scripts/agent.py "Rebootuj node-03." "Kolik je hodin?"

ENV (defaults match the loopback deployment):
  LLM_URL=http://127.0.0.1:8080   MCPO_URL=http://127.0.0.1:8000
  MODEL=qwen2.5-7b-instruct        MOUNTS=rag,placement   CTX_BUDGET=12000
Only needs `requests`. Grounding prompt imported from infra/openwebui/setup_openwebui.py.
"""
import os
import sys
import json
import time
import pathlib
import argparse
from datetime import datetime, timezone

try:
    import requests
except ImportError as e:
    sys.exit(f"missing dep ({e}); run with the project venv: .venv/bin/python {sys.argv[0]} ...")

REPO = pathlib.Path(__file__).resolve().parent.parent
LLM_URL  = os.environ.get("LLM_URL", "http://127.0.0.1:8080").rstrip("/")
MCPO_URL = os.environ.get("MCPO_URL", "http://127.0.0.1:8000").rstrip("/")
MODEL    = os.environ.get("MODEL", "qwen2.5-7b-instruct")
MOUNTS   = os.environ.get("MOUNTS", "rag,placement").split(",")   # read tools; reboot is injected below
CTX_BUDGET = int(os.environ.get("CTX_BUDGET", "12000"))           # elide old tool bodies past this
DEBUG = False                                                     # --debug: dump raw llama responses

# Deterministic sampler, mirroring the deployed model preset (ADR-0007 §3).
DET = {"temperature": 0, "top_k": 1, "top_p": 0, "min_p": 0, "seed": 42, "stream": False}

# Built-in scenarios. reboot-canary reproduces the boss's OWUI test: reboot works
# early, then several RAG turns bloat context, then reboot AGAIN — the turn where
# OWUI drops the tool and fakes success. `expect` is the tool we assert was called
# (None = a pure text answer, no tool).
SCENARIOS = {
    "reboot-canary": [
        ("Rebootuj host node-03.",                          "reboot_host"),
        ("Kolik je hodin?",                                 "get_current_timestamp"),
        ("Kdo je prezident USA?",                           "search"),   # RAG fires, answers "not in docs"
        ("Jak se přihlásím ze ŠA do EPC?",                  "search"),
        ("Jaké jsou kontakty na projektové vedení v EPC?",  "search"),
        ("Kam se ukládají hesla pro EPC?",                  "search"),
        ("Rebootuj host node-03.",                          "reboot_host"),   # <-- the canary
        ("Kde běží VM sftp-gw?",                            "where_is_vm"),
    ],
    "smoke": [
        ("Kde běží VM sftp-gw?", "where_is_vm"),
        ("Rebootuj host node-03.", "reboot_host"),
    ],
    # multi-tool: a turn's `expect` may be a TUPLE — the turn passes only if EVERY
    # named tool was called. Both turns avoid RAG so the demo stays fast.
    "multi": [
        # chained: locate the VM, then reboot the host it runs on (2 sequential tools)
        ("Zjisti kde běží VM sftp-gw a pak rebootuj ten host.", ("where_is_vm", "reboot_host")),
        # two independent facts in one question (clock + placement)
        ("Kolik je hodin a kde běží VM sftp-gw?", ("get_current_timestamp", "where_is_vm")),
    ],
}


# --------------------------------------------------------------------------- #
# Tool loading: live mcpo schemas (read tools) + injected reboot_host + clock
# --------------------------------------------------------------------------- #
def _deref(node, root, seen=()):
    """Inline local $ref so `parameters` is a self-contained JSON Schema."""
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


def load_tools():
    """Return (openai_tools, registry). registry[name] tells the executor how to
    run a call: ("mcpo", mount, path) | ("reboot",) | ("clock",)."""
    tools, registry = [], {}
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
            for _method, op in methods.items():
                name = op.get("operationId") or f"{m}_{path.strip('/')}"
                body = op.get("requestBody", {}).get("content", {}).get("application/json", {})
                schema = _deref(body["schema"], spec) if "schema" in body else {}
                tools.append({"type": "function", "function": {
                    "name": name,
                    "description": (op.get("description") or op.get("summary") or "").strip(),
                    "parameters": schema or {"type": "object", "properties": {}},
                }})
                registry[name] = ("mcpo", m, path)

    # reboot_host — the human-gated wrapper the MODEL sees (no `confirm` param, just
    # like the OWUI reboot_guarded tool). host-control is NOT auto-loaded above.
    tools.append({"type": "function", "function": {
        "name": "reboot_host",
        "description": ("Drain a hypervisor host's VMs and reboot it. Shows the plan and REQUIRES "
                        "explicit human confirmation before doing anything. Use whenever the user asks "
                        "to reboot / restart / bounce a hypervisor host."),
        "parameters": {"type": "object", "properties": {
            "hostname": {"type": "string", "description": "the hypervisor host to reboot (e.g. node-03)"},
            "reason": {"type": "string", "description": "short reason, recorded for the audit trail"},
        }, "required": ["hostname"]},
    }})
    registry["reboot_host"] = ("reboot",)

    # get_current_timestamp — parity with the deployed clock tool.
    tools.append({"type": "function", "function": {
        "name": "get_current_timestamp",
        "description": "Return the current date and time (UTC and the user's local time).",
        "parameters": {"type": "object", "properties": {}},
    }})
    registry["get_current_timestamp"] = ("clock",)
    return tools, registry


# --------------------------------------------------------------------------- #
# Tool execution (real)
# --------------------------------------------------------------------------- #
def _mcpo_call(mount, path, args):
    r = requests.post(f"{MCPO_URL}/{mount}{path}", json=args, timeout=180)
    r.raise_for_status()
    return r.json()


def _reboot(args, auto_yes):
    """Mirror reboot_guarded.py: dry-run -> plan -> human confirm -> execute."""
    host = args.get("hostname", "")
    reason = args.get("reason", "")
    try:
        plan = _mcpo_call("host-control", "/reboot_host", {"hostname": host, "confirm": False})
    except Exception as e:
        return {"ok": False, "error": f"could not reach host-control: {e}"}
    if not plan.get("ok"):
        return {"ok": False, "error": f"cannot plan reboot of {host}: {plan.get('reason')}"}
    p = plan.get("plan", {})
    vms = p.get("vms_to_drain", [])
    vm_list = ", ".join(f"{v.get('name')}(VM {v.get('vm_id')})" for v in vms) or "(none)"
    print(f"\n    ┌─ CONFIRM: drain+reboot {host}  reason={reason or '—'}")
    print(f"    │  will live-migrate first: {vm_list}")
    if auto_yes:
        print("    └─ auto-approved (--yes)\n")
        approved = True
    else:
        ans = input("    └─ approve? [y/N] ").strip().lower()
        approved = ans in ("y", "yes", "ano", "a")
    if not approved:
        return {"ok": True, "result": f"Reboot of {host} was cancelled. No changes were made."}
    res = _mcpo_call("host-control", "/reboot_host", {"hostname": host, "confirm": True})
    return res if res.get("ok") else {"ok": False, "error": res.get("reason")}


def execute(name, args, registry, auto_yes):
    kind = registry.get(name, (None,))[0]
    try:
        if kind == "mcpo":
            _, mount, path = registry[name]
            return _mcpo_call(mount, path, args)
        if kind == "reboot":
            return _reboot(args, auto_yes)
        if kind == "clock":
            now = datetime.now(timezone.utc)
            return {"current_iso": now.isoformat(),
                    "user_local_iso": now.astimezone().isoformat()}
        return {"error": f"unknown tool {name!r}"}
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


# --------------------------------------------------------------------------- #
# Context management — OURS. Schemas never evicted; oldest tool BODIES elided.
# --------------------------------------------------------------------------- #
ELIDED = json.dumps({"_elided": "old tool result dropped to bound context"})


def bound_context(messages, last_prompt_tokens, budget, keep_recent_tools=4):
    """If the last request's prompt exceeded `budget`, elide the OLDEST tool-role
    message bodies (keeping the most recent `keep_recent_tools`), preserving every
    tool_call<->result pairing and all assistant/user text. Never summarizes, never
    drops a message — so tool-calling structure stays valid. Returns #elided."""
    if last_prompt_tokens <= budget:
        return 0
    tool_idxs = [i for i, m in enumerate(messages)
                 if m.get("role") == "tool" and m.get("content") != ELIDED]
    elide = tool_idxs[:-keep_recent_tools] if keep_recent_tools else tool_idxs
    for i in elide:
        messages[i]["content"] = ELIDED
    return len(elide)


# --------------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------------- #
def llama(messages, tools):
    body = {"model": MODEL, "messages": messages, "tools": tools, "tool_choice": "auto", **DET}
    t0 = time.time()
    r = requests.post(f"{LLM_URL}/v1/chat/completions", json=body, timeout=300)
    r.raise_for_status()
    d = r.json()
    choice = d["choices"][0]
    msg = choice["message"]
    if DEBUG:
        tc = msg.get("tool_calls") or []
        print(f"      [debug] finish={choice.get('finish_reason')!r} "
              f"tool_calls={[c['function']['name'] for c in tc]} "
              f"content={(msg.get('content') or '')[:160]!r}", file=sys.stderr)
    return msg, d.get("usage", {}) or {}, d.get("timings", {}) or {}, time.time() - t0


def run_turn(messages, tools, registry, auto_yes, max_rounds=6):
    """One user turn -> (deliberate -> tools)* -> answer. Mutates `messages`.
    Records each STAGE separately so we can time deliberation (deciding + emitting
    the tool call) vs tool execution vs answer generation — the phases OWUI shows.
    Handles multiple tool calls both parallel (several tool_calls in one model step)
    and chained (across rounds). Returns a metrics dict incl. per-stage `steps`."""
    steps, called, prompt_tok = [], [], 0
    for _ in range(max_rounds):
        msg, usage, tim, dt = llama(messages, tools)
        prompt_tok = max(prompt_tok, usage.get("prompt_tokens", 0))
        calls = msg.get("tool_calls") or []
        steps.append({
            "kind": "deliberate" if calls else "answer",
            "wall": dt,
            "prefill_ms": tim.get("prompt_ms", 0.0),
            "decode_n": tim.get("predicted_n", usage.get("completion_tokens", 0)),
            "tps": tim.get("predicted_per_second", 0.0),
            "calls": [c["function"]["name"] for c in calls],
        })
        if not calls:
            return _turn_metrics((msg.get("content") or "").strip(), called, prompt_tok, steps)
        messages.append(msg)
        for c in calls:
            fn = c["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            called.append(name)
            t0 = time.time()
            result = execute(name, args, registry, auto_yes)
            # Read (don't strip) the server's per-stage breakdown so the model still
            # sees byte-identical content to OWUI; we just also surface it in metrics.
            timing = result.get("_timing_ms") if isinstance(result, dict) else None
            steps.append({"kind": "tool", "name": name, "wall": time.time() - t0, "timing": timing})
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": json.dumps(result, ensure_ascii=False)})
    return _turn_metrics("! still calling tools after max rounds", called, prompt_tok, steps)


def _turn_metrics(answer, called, prompt_tok, steps):
    stage = lambda k: sum(s["wall"] for s in steps if s["kind"] == k)
    think, tool, ans = stage("deliberate"), stage("tool"), stage("answer")
    return {"answer": answer, "called": called, "prompt_tokens": prompt_tok,
            "steps": steps, "think_s": think, "tool_s": tool, "answer_s": ans,
            "wall_s": think + tool + ans}


def render_steps(steps) -> list:
    """Per-stage lines: the deliberate / tool / answer split with real timings."""
    out = []
    for s in steps:
        if s["kind"] == "tool":
            line = f"      · tool        {s['name']:<28} {s['wall']:6.2f}s"
            tm = s.get("timing")
            if tm:  # server-side per-stage split (search_corpus): embed/fuse/rerank/expand
                line += "   [" + " ".join(f"{k[:-3]} {v/1000:.2f}s" for k, v in tm.items()) + "]"
            out.append(line)
        else:
            label = "deliberate" if s["kind"] == "deliberate" else "answer    "
            arrow = ("  →  " + ", ".join(s["calls"])) if s["calls"] else ""
            out.append(f"      · {label}  {s['wall']:6.1f}s  "
                       f"(prefill {s['prefill_ms']/1000:4.1f}s, decode {s['decode_n']:>3} tok @ {s['tps']:4.1f}/s)"
                       f"{arrow}")
    return out


def _expect_ok(expect, called):
    """None if no expectation; else True iff EVERY expected tool (str, or each of a
    tuple) substring-matches something in `called`. Supports multi-tool turns."""
    if expect is None:
        return None
    wants = expect if isinstance(expect, (list, tuple)) else (expect,)
    return all(any(w in c for c in called) for w in wants)


def _fmt_row(m, n=0, expect=None, elided=0):
    tools_str = ",".join(m["called"]) or "—"
    ok_flag = _expect_ok(expect, m["called"])
    ok = "  " if ok_flag is None else ("OK" if ok_flag else "!!")
    want = f"  (want {expect})" if expect is not None else ""
    return (f"{ok} {n:>2} | ctx {m['prompt_tokens']:>6} | elided {elided:>2} | "
            f"think {m['think_s']:4.1f}s · tools {m['tool_s']:4.1f}s · answer {m['answer_s']:5.1f}s | "
            f"total {m['wall_s']:5.1f}s | tools: {tools_str}{want}")


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def build(system_none):
    tools, registry = load_tools()
    names = [t["function"]["name"] for t in tools]
    system = None
    if not system_none:
        sys.path.insert(0, str(REPO / "infra" / "openwebui"))
        try:
            import setup_openwebui
            system = setup_openwebui.GROUNDING_SYSTEM
        except Exception as e:
            print(f"  ! no grounding prompt ({e}); running without one", file=sys.stderr)
    messages = [{"role": "system", "content": system}] if system else []
    print(f"model={MODEL}  tools={names}  ctx_budget={CTX_BUDGET}  "
          f"system={'yes' if system else 'NONE'}\n")
    return tools, registry, messages


def run_scenario(name, args):
    if name not in SCENARIOS:
        sys.exit(f"unknown scenario {name!r}; have: {', '.join(SCENARIOS)}")
    tools, registry, messages = build(args.no_system)
    print(f"=== scenario: {name} ===")
    rows, fails = [], 0
    for n, (prompt, expect) in enumerate(SCENARIOS[name], 1):
        messages.append({"role": "user", "content": prompt})
        print(f"\n[{n}] > {prompt}")
        m = run_turn(messages, tools, registry, args.yes)
        messages.append({"role": "assistant", "content": m["answer"]})
        elided = bound_context(messages, m["prompt_tokens"], CTX_BUDGET)
        for line in render_steps(m["steps"]):
            print(line)
        print(f"    < {m['answer'][:220]}")
        row = _fmt_row(m, n, expect, elided)
        print("   ", row)
        if _expect_ok(expect, m["called"]) is False:
            fails += 1
        rows.append(row)
    print("\n" + "=" * 78 + f"\nSUMMARY ({name}) — {len(rows)} turns, {fails} tool-routing miss(es)\n")
    for r in rows:
        print(r)
    sys.exit(1 if fails else 0)


def run_prompts(prompts, args):
    tools, registry, messages = build(args.no_system)
    for p in prompts:
        messages.append({"role": "user", "content": p})
        print(f"\n> {p}")
        m = run_turn(messages, tools, registry, args.yes)
        messages.append({"role": "assistant", "content": m["answer"]})
        elided = bound_context(messages, m["prompt_tokens"], CTX_BUDGET)
        for line in render_steps(m["steps"]):
            print(line)
        print(f"< {m['answer']}")
        print("   ", _fmt_row(m, elided=elided))


def run_repl(args):
    tools, registry, messages = build(args.no_system)
    print("interactive agent — Ctrl-D or /quit to exit, /reset to clear history\n")
    while True:
        try:
            p = input("› ").strip()
        except EOFError:
            print()
            break
        if not p:
            continue
        if p in ("/quit", "/exit"):
            break
        if p == "/reset":
            del messages[1 if messages and messages[0]["role"] == "system" else 0:]
            print("  (history cleared)\n")
            continue
        messages.append({"role": "user", "content": p})
        m = run_turn(messages, tools, registry, args.yes)
        messages.append({"role": "assistant", "content": m["answer"]})
        elided = bound_context(messages, m["prompt_tokens"], CTX_BUDGET)
        print()
        for line in render_steps(m["steps"]):
            print(line)
        print(f"\n{m['answer']}\n")
        print("   ", _fmt_row(m, elided=elided), "\n")


def main():
    ap = argparse.ArgumentParser(description="headless multi-turn agent harness over llama-server + mcpo")
    ap.add_argument("prompts", nargs="*", help="one-shot prompts; omit for interactive REPL")
    ap.add_argument("--scenario", help=f"run a scripted benchmark: {', '.join(SCENARIOS)}")
    ap.add_argument("--yes", action="store_true", help="auto-approve reboot confirmations (for scripted runs)")
    ap.add_argument("--no-system", action="store_true", help="send no grounding system prompt")
    ap.add_argument("--show-tools", action="store_true", help="print loaded tool names + exit")
    ap.add_argument("--debug", action="store_true", help="dump raw llama responses (finish_reason, tool_calls, content)")
    args = ap.parse_args()

    global DEBUG
    DEBUG = args.debug

    if args.show_tools:
        tools, _ = load_tools()
        for t in tools:
            print(t["function"]["name"], "->", t["function"]["description"][:70])
        return
    if args.scenario:
        run_scenario(args.scenario, args)
    elif args.prompts:
        run_prompts(args.prompts, args)
    else:
        run_repl(args)


if __name__ == "__main__":
    main()
