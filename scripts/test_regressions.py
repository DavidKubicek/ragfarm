#!/usr/bin/env python3
"""test_regressions.py — replay docs/prompts.md against the live stack and judge
the answers.

WHY THIS EXISTS
The system prompt is the entire behavioural specification of this assistant, and
it is fidgety: a change that looks local to RULE 3 shifts how RULE 1 is obeyed,
and nothing tells you until a demo goes sideways. Every prompt defect found in
August was of that shape. This suite makes the blast radius of a prompt edit
visible in one command.

TWO PHASES, DELIBERATELY SEPARATE

  collect   Ask the live slot each prompt from docs/prompts.md, mimicking Open
            WebUI as closely as we can: the preset's own system prompt, the
            preset's own sampler, the same mcpo tools. The answers in prompts.md
            came from OWUI chats, so the replay has to come from the same shape
            of environment or the comparison is unfair.

  judge     Compare each collected answer against the case's `expect` text using
            a SECOND model call with a comparator prompt and NO TOOLS. The judge
            is a different job from the assistant: it reasons over two given
            texts. Giving it retrieval would let it go and check for itself and
            start grading against its own answer instead of against `expect`.

Deterministic checks run BEFORE the judge. A `must` substring that is absent is
a failure no model needs to rule on, and it costs nothing to find out.

EXIT STATUS is the worst verdict seen, so the suite composes into other scripts:
    0  EQUAL      every case achieves what it should
    1  DRIFTING   at least one case slipped, none failed outright
    2  WRONG      at least one case failed
    3  harness error (stack down, unparsable library)

USAGE
    scripts/test_regressions.py                       # collect + judge, all cases
    scripts/test_regressions.py --tag rag             # only tagged cases
    scripts/test_regressions.py --id R1 --id R7
    scripts/test_regressions.py --collect-only -o run.json
    scripts/test_regressions.py --judge-only -i run.json
    scripts/test_regressions.py --preset ragfarm-vision-instruct

Runs are written to logs/regressions-<UTC>.{json,log} — the JSON is the record to
diff between prompt versions, which is the entire point.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from promptlib import load_cases, Case  # noqa: E402

PY = REPO / ".venv" / "bin" / "python"
AGENT = REPO / "scripts" / "agent.py"
COMPARATOR = REPO / "tests" / "comparator-system.txt"
DEFAULT_PRESET = "ragfarm-vision-fp8"   # slot 0, always the primary MoE

VERDICTS = {"EQUAL": 0, "DRIFTING": 1, "WRONG": 2}


def served_alias(url: str = "http://127.0.0.1:8080") -> str:
    """Ask the slot what it is actually serving. agent.py's MODEL default is an
    env fallback from the llama.cpp era; sending it produces a 404 that looks
    like a judge failure rather than a configuration one."""
    import requests
    return requests.get(f"{url}/v1/models", timeout=10).json()["data"][0]["id"]


def tool_matched(expected: str, called: list[str]) -> bool:
    """mcpo renames tools when it mounts them: search_corpus is advertised to the
    model as tool_search_corpus_post. Compare on the logical name inside."""
    e = expected.strip().lower().strip("_")
    return any(e in c.strip().lower() for c in called)


def run_agent(argv: list[str], log: Path, timeout: int = 1800) -> list[dict]:
    """Call agent.py and parse its --json lines. Raises on a harness failure."""
    cmd = [str(PY), str(AGENT), "--json", "--log", str(log), *argv]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not out:
        raise RuntimeError(f"agent.py produced no result\n"
                           f"  cmd: {' '.join(cmd)}\n"
                           f"  rc={r.returncode}\n  {(r.stderr or r.stdout)[-600:]}")
    return out


def collect(cases: list[Case], preset: str, log: Path) -> list[dict]:
    """Phase 1 — one agent.py invocation per case, so a slow or wedged case
    cannot take the rest of the run with it."""
    rows = []
    for i, c in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {c.id:<5} collecting...", end="", flush=True)
        t0 = time.time()
        try:
            res = run_agent(["--owui-preset", preset, c.prompt], log)[0]
            rows.append({"id": c.id, "title": c.title, "prompt": c.prompt,
                         "expect": c.expect, "must": c.must, "must_not": c.must_not,
                         "tools_expected": c.tools, "attach": c.attach,
                         "answer": res["answer"], "tools_called": res["tools_called"],
                         "wall_s": res["wall_s"], "model": res["model"]})
            print(f" {time.time()-t0:5.1f}s  tools={res['tools_called'] or '-'}")
        except Exception as e:
            rows.append({"id": c.id, "title": c.title, "prompt": c.prompt,
                         "expect": c.expect, "must": c.must, "must_not": c.must_not,
                         "tools_expected": c.tools, "attach": c.attach,
                         "answer": "", "tools_called": [], "wall_s": time.time() - t0,
                         "model": "", "error": str(e)[:400]})
            print(f" ERROR {str(e)[:80]}")
    return rows


def deterministic(row: dict) -> tuple[str, str] | None:
    """Checks a model must not be asked to make. -> (verdict, reason) or None."""
    if row.get("error"):
        return "WRONG", f"harness/stack error: {row['error'][:200]}"
    a = row["answer"]
    if not a.strip():
        return "WRONG", "empty answer"
    for s in row["must"]:
        if s not in a:
            return "WRONG", f"required substring absent: {s!r}"
    for s in row["must_not"]:
        if s in a:
            return "WRONG", f"forbidden substring present: {s!r}"
    missing = [e for e in row["tools_expected"] if not tool_matched(e, row["tools_called"])]
    if missing:
        return "WRONG", (f"expected tool(s) {missing} not called "
                         f"(called: {row['tools_called'] or 'none'})")
    return None


def judge(rows: list[dict], model: str | None, log: Path) -> list[dict]:
    """Phase 2 — one comparator call per row, no tools, deterministic sampler."""
    for i, row in enumerate(rows, 1):
        early = deterministic(row)
        if early:
            row["verdict"], row["reason"], row["judged_by"] = *early, "check"
            print(f"  [{i}/{len(rows)}] {row['id']:<5} {row['verdict']:<9} {row['reason'][:70]}")
            continue
        payload = (f"PROMPT\n{row['prompt']}\n\n"
                   f"EXPECTED\n{row['expect']}\n\n"
                   f"ACTUAL\n{row['answer']}\n")
        argv = ["--model", model, "--system-file", str(COMPARATOR),
                "--no-tools", "--temp", "0", payload]
        try:
            res = run_agent(argv, log, timeout=900)[0]
            text = (res["answer"] or "").strip()
            first = text.split("\n", 1)[0].strip().upper().strip(".:*# ")
            verdict = next((v for v in VERDICTS if v in first), None)
            if verdict is None:
                # The judge failing to follow its own output contract is a
                # harness problem, not a verdict about the assistant. Say so
                # rather than silently scoring it.
                row["verdict"], row["reason"], row["judged_by"] = (
                    "WRONG", f"judge returned no verdict: {text[:150]!r}", "judge-malformed")
            else:
                rest = text.split("\n", 1)[1].strip() if "\n" in text else ""
                row["verdict"], row["reason"], row["judged_by"] = verdict, rest, "judge"
        except Exception as e:
            row["verdict"], row["reason"], row["judged_by"] = "WRONG", f"judge error: {e}"[:300], "error"
        print(f"  [{i}/{len(rows)}] {row['id']:<5} {row['verdict']:<9} {row['reason'][:70]}")
    return rows


def report(rows: list[dict]) -> int:
    print("\n" + "=" * 78)
    print(f"{'CASE':<6} {'VERDICT':<10} {'TOOLS':<22} TITLE")
    print("-" * 78)
    worst = 0
    for r in rows:
        worst = max(worst, VERDICTS.get(r.get("verdict", "WRONG"), 2))
        print(f"{r['id']:<6} {r.get('verdict','?'):<10} "
              f"{','.join(r['tools_called']) or '-':<22} {r['title'][:36]}")
    tally = {v: sum(1 for r in rows if r.get("verdict") == v) for v in VERDICTS}
    print("-" * 78)
    print(f"{len(rows)} cases: " + "  ".join(f"{k}={tally[k]}" for k in VERDICTS))
    if tally["WRONG"] or tally["DRIFTING"]:
        print("\nnot EQUAL:")
        for r in rows:
            if r.get("verdict") != "EQUAL":
                print(f"  {r['id']}  {r.get('verdict')}: {r.get('reason','')[:160]}")
    return worst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", action="append", default=[], help="only cases with this tag")
    ap.add_argument("--id", action="append", default=[], help="only this case id (repeatable)")
    ap.add_argument("--preset", default=DEFAULT_PRESET, help=f"OWUI preset to replay against (default {DEFAULT_PRESET})")
    ap.add_argument("--judge-model", help="model for the comparator (default: same slot)")
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--judge-only", action="store_true")
    ap.add_argument("-o", "--out", help="write the run JSON here (default logs/regressions-<UTC>.json)")
    ap.add_argument("-i", "--in", dest="infile", help="judge a previous run's JSON")
    ap.add_argument("--include-skipped", action="store_true")
    a = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (REPO / "logs").mkdir(exist_ok=True)
    log = REPO / "logs" / f"regressions-{stamp}.log"
    out = Path(a.out) if a.out else REPO / "logs" / f"regressions-{stamp}.json"

    if a.judge_only:
        if not a.infile:
            ap.error("--judge-only needs -i/--in")
        rows = json.loads(Path(a.infile).read_text())["cases"]
    else:
        try:
            cases = load_cases(tags=set(a.tag) or None, ids=set(a.id) or None,
                               include_skipped=a.include_skipped)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 3
        if not cases:
            print("no cases selected", file=sys.stderr)
            return 3
        # An attachment means an image the harness cannot send through agent.py's
        # text path. Skipping loudly beats pretending the case ran.
        img = [c.id for c in cases if c.attach and c.attach.endswith((".png", ".jpg", ".jpeg"))]
        if img:
            print(f"  note: {', '.join(img)} need image input — not yet supported here, skipping")
            cases = [c for c in cases if c.id not in img]
        print(f"== collect ({len(cases)} cases, preset {a.preset})")
        rows = collect(cases, a.preset, log)

    if not a.collect_only:
        jm = a.judge_model
        if not jm:
            # Prefer whatever answered during collect; fall back to asking the slot.
            jm = next((r["model"] for r in rows if r.get("model")), None) or served_alias()
        print(f"\n== judge ({len(rows)} cases, model {jm}, no tools, temp 0)")
        rows = judge(rows, jm, log)

    out.write_text(json.dumps({"stamp": stamp, "preset": a.preset, "cases": rows},
                              indent=2, ensure_ascii=False) + "\n")
    rc = report(rows) if not a.collect_only else 0
    print(f"\nrun: {out}\nlog: {log}")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(3)
