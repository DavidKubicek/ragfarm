#!/usr/bin/env python3
"""Verify the ragfarm agent toolchain end-to-end WITHOUT parsing Open WebUI source.

This is the step-07 gate check, distilled from the build. It runs in three layers,
cheapest/most-reliable first, and exits with a code the build can act on:

  exit 0  full PASS  — deterministic chain OK *and* Open WebUI executed the tool.
  exit 2  needs eyes — deterministic chain OK, but the headless Open WebUI chat
                       emulation was inconclusive; prints steps for a 30-second
                       interactive browser confirmation (the honest fallback —
                       OWUI's interactive agent loop is genuinely a UI action).
  exit 1  FAIL       — a deterministic check failed (pipeline is actually broken).

Deterministic layer (reliable — this is what a CI gate should trust):
  1. mcpo OpenAPI POST /rag/search_corpus, sparse hostname  -> record contains it.
  2. mcpo OpenAPI POST /rag/search_corpus, Czech dense query -> a .docx chunk.
  3. llama-server emits a tool_call when given the search_corpus tool.

Open WebUI layer (valuable but flaky headless — see build log 07):
  Creates a *persisted* chat then drives /api/chat/completions with the exact
  field set that makes OWUI run its agentic loop (the load-bearing one learned the
  hard way is `assistant_message_id` IN THE REQUEST BODY, alongside chat_id/id/
  session_id/tool_ids/background_tasks/stream + params.function_calling=native).
  Success is judged by the RELIABLE signal — mcpo's access log gaining a
  POST /rag/search_corpus — plus a non-empty grounded answer read back from the
  persisted chat. If that can't be confirmed, we fall back to interactive.

Env knobs (all optional, sane defaults):
  OWUI_URL=http://127.0.0.1:3000   MCPO_URL=http://127.0.0.1:8000
  LLAMA_URL=http://127.0.0.1:8080   MCPO_CONTAINER=infra-mcpo-1
  OWUI_MODEL=ragfarm  RAG_TOOL_ID=server:0
  OWUI_TOKEN=<jwt>  (or OWUI_EMAIL + OWUI_PASSWORD to sign in)
  HOSTNAME_PROBE=hsmbvxip001ts   CZECH_PROBE="Jak se přistupuje z ŠA do hostingu?"
"""
import os
import sys
import uuid
import subprocess
import requests

OWUI = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
MCPO = os.environ.get("MCPO_URL", "http://127.0.0.1:8000").rstrip("/")
LLAMA = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")
MCPO_CONTAINER = os.environ.get("MCPO_CONTAINER", "infra-mcpo-1")
OWUI_MODEL = os.environ.get("OWUI_MODEL", "ragfarm")
RAG_TOOL_ID = os.environ.get("RAG_TOOL_ID", "server:0")
HOSTNAME_PROBE = os.environ.get("HOSTNAME_PROBE", "hsmbvxip001ts")
CZECH_PROBE = os.environ.get("CZECH_PROBE", "Jak se přistupuje z ŠA do hostingu?")

ok = lambda m: print(f"  [PASS] {m}")
bad = lambda m: print(f"  [FAIL] {m}")


def deterministic() -> bool:
    passed = True
    print("== deterministic layer ==")
    # 1. sparse exact-match through mcpo
    try:
        r = requests.post(f"{MCPO}/rag/search_corpus",
                          json={"query": HOSTNAME_PROBE, "k": 3}, timeout=60).json()
        top = (r.get("results") or [{}])[0].get("text", "").lower()
        if HOSTNAME_PROBE.lower() in top:
            ok(f"mcpo sparse: {HOSTNAME_PROBE!r} -> exact record")
        else:
            bad(f"mcpo sparse: {HOSTNAME_PROBE!r} not in top result"); passed = False
    except Exception as e:
        bad(f"mcpo sparse probe errored: {e}"); passed = False
    # 2. Czech dense through mcpo
    try:
        r = requests.post(f"{MCPO}/rag/search_corpus",
                          json={"query": CZECH_PROBE, "k": 3}, timeout=60).json()
        srcs = [h.get("source_file", "") for h in r.get("results", [])]
        if any(s.lower().endswith(".docx") for s in srcs):
            ok(f"mcpo dense: Czech query -> .docx chunk ({srcs[0]})")
        else:
            bad(f"mcpo dense: no .docx in results ({srcs})"); passed = False
    except Exception as e:
        bad(f"mcpo dense probe errored: {e}"); passed = False
    # 3. llama-server emits a tool call
    try:
        body = {
            "model": "qwen2.5-7b-instruct",
            "messages": [{"role": "user", "content": f"Look up host {HOSTNAME_PROBE}."}],
            "tools": [{"type": "function", "function": {
                "name": "search_corpus",
                "description": "Search the infrastructure corpus.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}],
            "tool_choice": "auto",
        }
        d = requests.post(f"{LLAMA}/v1/chat/completions", json=body, timeout=120).json()
        if d["choices"][0].get("finish_reason") == "tool_calls":
            ok("llama-server emits tool_calls for search_corpus")
        else:
            bad("llama-server did not emit a tool_call"); passed = False
    except Exception as e:
        bad(f"llama-server tool-call probe errored: {e}"); passed = False
    return passed


def _mcpo_call_count() -> int:
    try:
        out = subprocess.run(["docker", "logs", MCPO_CONTAINER],
                             capture_output=True, text=True, timeout=30)
        return (out.stdout + out.stderr).count("POST /rag/search_corpus")
    except Exception:
        return -1


def _token():
    tok = os.environ.get("OWUI_TOKEN")
    if tok:
        return tok
    email, pw = os.environ.get("OWUI_EMAIL"), os.environ.get("OWUI_PASSWORD")
    if not (email and pw):
        return None
    try:
        r = requests.post(f"{OWUI}/api/v1/auths/signin", json={"email": email, "password": pw}, timeout=30)
        return r.json().get("token") if r.ok else None
    except Exception:
        return None


def owui_e2e() -> bool:
    """Return True iff OWUI's agentic loop provably executed search_corpus."""
    print("== Open WebUI end-to-end layer ==")
    tok = _token()
    if not tok:
        print("  [skip] no OWUI_TOKEN / OWUI_EMAIL+OWUI_PASSWORD provided")
        return False
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    before = _mcpo_call_count()
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    user_msg = {"id": uid, "parentId": None, "childrenIds": [aid], "role": "user",
                "content": HOSTNAME_PROBE, "timestamp": 0, "models": [OWUI_MODEL]}
    asst_msg = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant",
                "content": "", "model": OWUI_MODEL, "timestamp": 0}
    chat = {"models": [OWUI_MODEL], "messages": [user_msg, asst_msg],
            "history": {"messages": {uid: user_msg, aid: asst_msg}, "currentId": aid}}
    try:
        cid = requests.post(f"{OWUI}/api/v1/chats/new", headers=H, json={"chat": chat}, timeout=30).json()["id"]
        body = {
            "model": OWUI_MODEL,
            "messages": [{"role": "user", "content":
                          f"What are the group, vCPU and RAM of host {HOSTNAME_PROBE}?"}],
            "chat_id": cid, "id": aid, "assistant_message_id": aid, "parent_id": uid,
            "session_id": "check-toolchain",
            "tool_ids": [RAG_TOOL_ID],
            "background_tasks": {"title_generation": False, "tags_generation": False},
            "stream": True, "params": {"function_calling": "native"},
        }
        with requests.post(f"{OWUI}/api/chat/completions", headers=H, json=body,
                           stream=True, timeout=300) as resp:
            for _ in resp.iter_lines():
                pass
        answer = requests.get(f"{OWUI}/api/v1/chats/{cid}", headers=H, timeout=30) \
            .json()["chat"]["history"]["messages"].get(aid, {}).get("content", "")
    except Exception as e:
        print(f"  [skip] OWUI emulation errored: {e}")
        return False
    after = _mcpo_call_count()
    executed = after > before >= 0
    if executed and answer.strip():
        ok(f"OWUI executed search_corpus via mcpo (calls {before}->{after}); grounded answer len={len(answer)}")
        return True
    print(f"  [inconclusive] mcpo calls {before}->{after}, answer_len={len(answer.strip())}")
    return False


def main() -> int:
    det = deterministic()
    if not det:
        print("\nRESULT: FAIL — deterministic chain broken (fix before trusting the UI).")
        return 1
    if owui_e2e():
        print("\nRESULT: PASS — full toolchain verified (deterministic + OWUI executed the tool).")
        return 0
    print(
        "\nRESULT: NEEDS INTERACTIVE CONFIRMATION.\n"
        "The deterministic chain (embedder+Qdrant+mcpo+llama tool-calling) is GREEN, but\n"
        "the headless Open WebUI agent-loop emulation was inconclusive (this is expected —\n"
        "OWUI's interactive loop is finicky headless). Confirm in ~30s in the browser:\n"
        f"  1. open {OWUI} and log in\n"
        f"  2. select the '{OWUI_MODEL}' model (rag tool pre-attached)\n"
        f"  3. ask: \"What are the group, vCPU and RAM of host {HOSTNAME_PROBE}?\"  (English/sparse)\n"
        f"     and: \"{CZECH_PROBE}\"  (Czech/dense)\n"
        "  4. both answers must be GROUNDED in retrieved values (hostnames/IPs/steps), not generic.\n"
        "If both are grounded, the gate is met."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
