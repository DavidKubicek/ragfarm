#!/usr/bin/env python3
"""probe_k.py — does the model parameterise search_corpus sensibly?

Measures the ONE failure mode that dominated the first A/B: the model narrowing
`k` to 1, starving its own retrieval, then answering confidently from the single
wrong record it got back.

Uses the REAL production system prompt from setup_openwebui.py — not a
hand-written stub. The first A/B used a 3-line stub, which is precisely why its
conclusions about tool discipline were weaker than they looked.

Prints the query, k, record count AND the answer, because a sensible-looking
query that still produces a wrong answer is a different problem from a bad query.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra" / "openwebui"))
import setup_openwebui as owui  # noqa: E402

RAG = "http://127.0.0.1:8000/rag/search_corpus"
_ALL = [(8080, "MoE-FP8"), (8082, "dense-FP8")]
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
# optional 2nd arg: port filter, so the slow dense slot can be skipped when only
# the MoE is under investigation
SLOTS = [s for s in _ALL if str(s[0]) == sys.argv[2]] if len(sys.argv) > 2 else _ALL

QUESTIONS = [
    ("pm", "Kdo je projektovy manazer (PM) za firmu EPC? Uved jen jeho, ne cely projektovy tym.",
     lambda a: ("esal" in a) and not any(x in a for x in ("ážný", "yzsko", "áňa"))),
    ("vcpu", "Kolik vCPU a RAM ma VM hsmbvxip001ts?",
     lambda a: ("8" in a) and ("64" in a)),
]


def main() -> int:
    spec = requests.get("http://127.0.0.1:8000/rag/openapi.json", timeout=20).json()
    sch = spec["components"]["schemas"]["search_corpus_form_model"]
    tool = {"type": "function", "function": {
        "name": "search_corpus",
        "description": spec["paths"]["/search_corpus"]["post"].get("description", "Search corpus."),
        "parameters": {"type": "object", "properties": sch.get("properties", {}),
                       "required": sch.get("required", [])}}}
    SYS = owui.PROMPT_BODIES["vision"]
    print(f"system prompt: {len(SYS)} znaku (produkcni)\n")

    out = []
    for port, name in SLOTS:
        try:
            model = requests.get(f"http://127.0.0.1:{port}/v1/models",
                                 timeout=10).json()["data"][0]["id"]
        except Exception as e:
            print(f"{name} :{port} nedostupny: {e}")
            continue
        print(f"{'='*72}\n=== {name}  {model}")
        for qid, q, check in QUESTIONS:
            for run in range(RUNS):
                msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": q}]
                t0 = time.time()
                r = requests.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=1800,
                                  json={"model": model, "messages": msgs, "tools": [tool],
                                        "tool_choice": "auto", "max_tokens": 8192}).json()
                m = r["choices"][0]["message"]
                tcs = m.get("tool_calls") or []
                if not tcs:
                    print(f"  [{qid} run{run}] BEZ VOLANI NASTROJE ({time.time()-t0:.0f}s)")
                    out.append({"slot": name, "q": qid, "run": run, "k": None,
                                "records": None, "ok": False, "answer": m.get("content") or ""})
                    continue
                tc = tcs[0]
                args = json.loads(tc["function"]["arguments"])
                res = requests.post(RAG, json=args, timeout=600).json()
                msgs += [{"role": "assistant", "content": None, "tool_calls": [tc]},
                         {"role": "tool", "tool_call_id": tc["id"], "name": "search_corpus",
                          "content": json.dumps(res)}]
                r2 = requests.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=1800,
                                   json={"model": model, "messages": msgs, "tools": [tool],
                                         "max_tokens": 8192}).json()
                ans = r2["choices"][0]["message"].get("content") or ""
                ok = check(ans)
                el = time.time() - t0
                out.append({"slot": name, "q": qid, "run": run,
                            "k": args.get("k"), "query": args.get("query"),
                            "records": res.get("count"), "ok": ok,
                            "elapsed": round(el, 1), "answer": ans})
                print(f"  [{qid} run{run}] k={args.get('k')} zaznamu={res.get('count')} "
                      f"{'OK' if ok else 'CHYBA'} {el:.0f}s")
                print(f"      dotaz: {args.get('query')!r}")
                print(f"      odpoved: {' '.join(ans.split())[:260]}")
    Path(f"/tmp/probe_k_{SLOTS[0][1] if len(SLOTS)==1 else 'all'}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print("\n=== SOUHRN ===")
    for port, name in SLOTS:
        rows = [x for x in out if x["slot"] == name]
        if not rows:
            continue
        ks = [x["k"] for x in rows if x["k"] is not None]
        bad = sum(1 for k in ks if k is not None and k < 8)
        print(f"  {name}: spravne {sum(x['ok'] for x in rows)}/{len(rows)} | "
              f"k hodnoty {sorted(set(ks))} | k<8 v {bad}/{len(ks)} volanich")
    return 0


if __name__ == "__main__":
    sys.exit(main())
