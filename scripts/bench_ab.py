#!/usr/bin/env python3
"""bench_ab.py — MoE vs dense A/B harness (performance + intelligence).

Two benchmarks, deliberately kept apart because they have different natures:

  PERFORMANCE  prefill / decode / TTFT — deterministic, one run is meaningful.
  INTELLIGENCE puzzles + corpus grounding — STOCHASTIC at temperature 0.6, so a
               single run proves nothing. Everything here is repeated N times and
               reported as a fraction.

Every raw answer is written to the results JSON verbatim. The auto-scorer is a
convenience, NOT the verdict: it is a substring matcher and it WILL mis-grade
paraphrases. A human reads the saved answers and confirms each score before any
number leaves this file. (Learned the hard way: a one-shot judgement on a
non-deterministic model was reported as "decisive" and was noise.)
"""
from __future__ import annotations

import json
import random
import string
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra" / "openwebui"))
import setup_openwebui as owui  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/bench_ab_results.json")
RAG = "http://127.0.0.1:8000/rag/search_corpus"
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 10

# THE PRODUCTION SYSTEM PROMPT, not a stub. The first A/B used a 3-line stub and a
# stub is a different deployment: it was missing the tool-parameter discipline and
# the targeted-vs-overview answering rule, both of which turned out to drive most
# of the measured difference between the two models.
PROD_SYS = owui.PROMPT_BODIES["vision"]


def mid(port: int) -> str:
    return requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=10).json()["data"][0]["id"]


def rnd_words(n: int) -> str:
    return " ".join("".join(random.choices(string.ascii_lowercase, k=6)) for _ in range(n))


def chat(port, model, messages, max_tokens=8192, tools=None):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    t0 = time.time()
    r = requests.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=body, timeout=1800).json()
    m = r["choices"][0]["message"]
    return {
        "elapsed": time.time() - t0,
        "content": m.get("content") or "",
        "reasoning": m.get("reasoning") or m.get("reasoning_content") or "",
        "tool_calls": m.get("tool_calls") or [],
        "usage": r.get("usage", {}),
    }


# ---------------------------------------------------------------- performance
def perf(port: int, model: str) -> dict:
    out = {"prefill": [], "decode": None, "ttft": None}
    for words in (500, 2000):
        el, pt = [], 0
        for _ in range(3):
            t0 = time.time()
            r = requests.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=900,
                              json={"model": model, "max_tokens": 1,
                                    "messages": [{"role": "user",
                                                  "content": "Shrn: " + rnd_words(words)}]}).json()
            el.append(time.time() - t0)
            pt = r["usage"]["prompt_tokens"]
        med = sorted(el)[1]
        out["prefill"].append({"prompt_tokens": pt, "seconds": round(med, 3),
                               "tok_s": round(pt / med, 1)})
    # decode: count BOTH content and reasoning deltas — a Thinking model emits
    # reasoning first, so counting only content measures nothing for 256 tokens.
    t0, first, n = time.time(), None, 0
    with requests.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=900, stream=True,
                       json={"model": model, "max_tokens": 256, "ignore_eos": True, "stream": True,
                             "messages": [{"role": "user",
                                           "content": "Popis podrobne co dela hypervisor."}]}) as r:
        for ln in r.iter_lines(decode_unicode=True):
            if not ln or not ln.startswith("data: "):
                continue
            d = ln[6:].strip()
            if d == "[DONE]":
                break
            for c in json.loads(d).get("choices", []):
                de = c.get("delta") or {}
                if de.get("content") or de.get("reasoning") or de.get("reasoning_content"):
                    if first is None:
                        first = time.time() - t0
                    n += 1
    tot = time.time() - t0
    out["ttft"] = round(first, 3) if first else None
    out["decode"] = {"tokens": n, "seconds": round(tot, 2),
                     "tok_s": round(n / (tot - first), 1) if first else None}
    return out


# --------------------------------------------------------------- intelligence
PUZZLES = [
    {
        "id": "krabice",
        "q": ("Mas tri krabice. Jedna obsahuje jen jablka, druha jen pomerance, treti smes obojiho. "
              "Vsechny tri krabice jsou popsane, ale VSECHNY tri popisky jsou spatne. "
              "Kolik nejmene kusu ovoce musis celkem vytahnout, abys spolehlive urcil obsah vsech tri "
              "krabic? Odpovez cislem a strucne zduvodni."),
        "expect": "1",
        "hit": ["jeden", "jedno", "jednu", "1 kus", "1 ovoce", "pouze jeden", "jediny", "jedin"],
        "miss": ["dva kusy", "tri kusy", "2 kusy", "3 kusy"],
    },
    {
        "id": "monty",
        "q": ("Soutezni show: jsou tri dvere, za jednimi je auto, za dvema kozy. Vyberes si dvere c.1. "
              "Moderator, ktery vi, co je za dvermi, otevre dvere c.3 a je za nimi koza. "
              "Nabidne ti zmenit volbu na dvere c.2. Mas prehodit, nebo zustat? "
              "Uved pravdepodobnost vyhry pri prehozeni."),
        "expect": "prehodit, 2/3",
        "hit": ["prehodit", "přehodit", "zmenit", "změnit", "vymenit", "vyměnit", "2/3", "66", "67"],
        "miss": [],
    },
]


def score(ans: str, p: dict) -> bool:
    low = ans.lower()
    if any(m in low for m in p["miss"]):
        return False
    return any(h in low for h in p["hit"])


def intelligence(port: int, model: str) -> list:
    rows = []
    for p in PUZZLES:
        for run in range(RUNS):
            res = chat(port, model, [{"role": "system", "content": PROD_SYS},
                                     {"role": "user", "content": p["q"]}])
            rows.append({
                "puzzle": p["id"], "run": run,
                "elapsed": round(res["elapsed"], 1),
                "reasoning_chars": len(res["reasoning"]),
                "completion_tokens": res["usage"].get("completion_tokens"),
                "auto_score": score(res["content"], p),
                "answer": res["content"],          # verbatim, for human review
                "reasoning": res["reasoning"][:4000],
            })
            print(f"    {p['id']} run{run}: auto={rows[-1]['auto_score']} "
                  f"{rows[-1]['elapsed']}s", flush=True)
    return rows


# ------------------------------------------------------- grounding/discipline
GSYS = PROD_SYS
GQ = "Kdo je projektovy manazer (PM) za firmu EPC? Uved jen jeho, ne cely projektovy tym."


def grounding(port: int, model: str, tool: dict) -> list:
    rows = []
    for run in range(RUNS):
        msgs = [{"role": "system", "content": GSYS}, {"role": "user", "content": GQ}]
        t0 = time.time()
        a = chat(port, model, msgs, tools=[tool])
        if not a["tool_calls"]:
            rows.append({"run": run, "tool_called": False, "elapsed": round(time.time() - t0, 1),
                         "answer": a["content"], "auto_score": False, "rag_seconds": None})
            print(f"    grounding run{run}: NO TOOL CALL", flush=True)
            continue
        tc = a["tool_calls"][0]
        args = json.loads(tc["function"]["arguments"])
        t1 = time.time()
        res = requests.post(RAG, json=args, timeout=600).json()
        rag_s = time.time() - t1
        msgs += [{"role": "assistant", "content": None, "tool_calls": [tc]},
                 {"role": "tool", "tool_call_id": tc["id"], "name": "search_corpus",
                  "content": json.dumps(res)}]
        b = chat(port, model, msgs, tools=[tool])
        ans = b["content"]
        rows.append({
            "run": run, "tool_called": True, "query": args,
            "rag_seconds": round(rag_s, 2), "rag_records": res.get("count"),
            "elapsed": round(time.time() - t0, 1),
            "reasoning_chars": len(a["reasoning"]) + len(b["reasoning"]),
            "auto_score": ("esal" in ans) and not any(x in ans for x in ("ážný", "yzsko", "áňa")),
            "answer": ans,
        })
        print(f"    grounding run{run}: auto={rows[-1]['auto_score']} "
              f"{rows[-1]['elapsed']}s", flush=True)
    return rows


def main() -> int:
    spec = requests.get("http://127.0.0.1:8000/rag/openapi.json", timeout=20).json()
    sch = spec["components"]["schemas"]["search_corpus_form_model"]
    tool = {"type": "function", "function": {
        "name": "search_corpus", "description": "Search the infrastructure corpus.",
        "parameters": {"type": "object", "properties": sch.get("properties", {}),
                       "required": sch.get("required", [])}}}

    slots = [(8080, "slot0"), (8082, "slot1")]
    out = {"runs_per_case": RUNS, "results": {}}
    for port, label in slots:
        try:
            model = mid(port)
        except Exception as e:
            print(f"{label} :{port} unreachable: {e}", flush=True)
            continue
        print(f"\n=== {label} :{port} {model} ===", flush=True)
        print("  performance...", flush=True)
        p = perf(port, model)
        print("  intelligence...", flush=True)
        i = intelligence(port, model)
        print("  grounding...", flush=True)
        g = grounding(port, model, tool)
        out["results"][label] = {"port": port, "model": model,
                                 "performance": p, "intelligence": i, "grounding": g}
        OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nwritten: {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
