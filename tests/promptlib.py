#!/usr/bin/env python3
"""promptlib.py — parse docs/prompts.md into machine-usable test cases.

docs/prompts.md is the SINGLE SOURCE for both the human prompt library (it is a
chapter of the PDF) and the regression suite. That dual role is the whole design
constraint: the format has to stay pleasant to read and to extend by hand, while
being unambiguous enough that a script can lift every prompt and every
expectation out of it without guessing.

THE FORMAT
Each case is one `###` heading whose text starts with an ID, followed by an
optional metadata list and a set of labelled fenced blocks:

    ### R1 · FW rules for one host

    - **preset:** ragfarm-vision-fp8
    - **tools:** search_corpus
    - **status:** verified 2026-07-27 on the text preset

    ~~~prompt
    Jaka jsou FW pravidla pro host leadb229p.lea.piz?
    ~~~

    ~~~expect
    Calls search_corpus once, then returns the firewall rules for that host as a
    markdown table with every column present. Ends with a Source: line.
    ~~~

    ~~~must
    leadb229p
    ~~~

Fences use TILDES so a prompt or an expectation may itself contain ``` blocks —
draw.io and code cases do, and backtick fences would terminate early.

WHY `expect` IS PROSE, NOT A GOLDEN STRING
Sampling is non-greedy: temperature 0.6-0.7 with no fixed seed, so two correct
answers differ in wording every time. A literal diff would fail on every run and
teach everyone to ignore it. `expect` therefore describes what a correct answer
must ACHIEVE, and an LLM judge decides whether the observed answer achieves it —
see scripts/test_regressions.py. `must` / `must-not` carry the parts that ARE
deterministic (an identifier, a hostname, a figure) and are checked by string
match first, because a cheap check that can fail hard should never be delegated
to a model.

USAGE
    .venv/bin/python tests/promptlib.py --validate      # lint the library
    .venv/bin/python tests/promptlib.py --list          # ids + titles
    .venv/bin/python tests/promptlib.py --show R1       # one case, fully
    .venv/bin/python tests/promptlib.py --json          # all cases as JSON

    from promptlib import load_cases
    for c in load_cases(tags={"rag"}):
        ...
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_MD = REPO_ROOT / "docs" / "prompts.md"

# `### R1 · title` or `### R1 - title` or `### R1 — title`
_HEAD = re.compile(r"^###\s+(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s*[·\-—:]\s*(?P<title>.+?)\s*$")
_META = re.compile(r"^-\s+\*\*(?P<key>[a-z_-]+):\*\*\s*(?P<val>.*?)\s*$")
_FENCE = re.compile(r"^~~~(?P<label>[a-z-]*)\s*$")

BLOCKS = {"prompt", "expect", "must", "must-not"}
KNOWN_META = {"preset", "slot", "tools", "attach", "status", "tags", "skip"}


@dataclasses.dataclass
class Case:
    id: str
    title: str
    prompt: str
    expect: str
    must: list[str] = dataclasses.field(default_factory=list)
    must_not: list[str] = dataclasses.field(default_factory=list)
    preset: str = ""
    slot: int = 0
    tools: list[str] = dataclasses.field(default_factory=list)
    attach: str = ""
    status: str = ""
    tags: list[str] = dataclasses.field(default_factory=list)
    skip: str = ""
    line: int = 0

    def attachment(self) -> Path | None:
        return (REPO_ROOT / self.attach) if self.attach else None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def parse(text: str) -> tuple[list[Case], list[str]]:
    """-> (cases, problems). Never raises on a malformed file; the caller decides
    whether problems are fatal. A silent skip would let a broken case quietly
    stop being tested, which is worse than a noisy parse."""
    cases: list[Case] = []
    problems: list[str] = []
    cur: dict | None = None
    blocks: dict[str, list[str]] = {}
    fence: str | None = None
    buf: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal cur, blocks
        if cur is None:
            return
        missing = [b for b in ("prompt", "expect") if not blocks.get(b)]
        if missing:
            problems.append(f"{cur['id']} (line {cur['line']}): missing block(s): "
                            + ", ".join(f"~~~{m}" for m in missing))
        else:
            cases.append(Case(
                id=cur["id"], title=cur["title"], line=cur["line"],
                prompt="\n".join(blocks["prompt"]).strip(),
                expect="\n".join(blocks["expect"]).strip(),
                must=[l for l in blocks.get("must", []) if l.strip()],
                must_not=[l for l in blocks.get("must-not", []) if l.strip()],
                preset=cur["meta"].get("preset", ""),
                slot=int(cur["meta"].get("slot", "0") or 0),
                tools=[t.strip(" `") for t in cur["meta"].get("tools", "").split(",") if t.strip()],
                attach=cur["meta"].get("attach", ""),
                status=cur["meta"].get("status", ""),
                tags=[t.strip() for t in cur["meta"].get("tags", "").split(",") if t.strip()],
                skip=cur["meta"].get("skip", ""),
            ))
        cur, blocks = None, {}

    for n, raw in enumerate(text.splitlines(), 1):
        m = _FENCE.match(raw)
        if m:
            label = m.group("label")
            if fence is None:
                if label in BLOCKS:
                    if cur is None:
                        problems.append(f"line {n}: ~~~{label} block outside any case")
                    fence, buf = label, []
                # a bare ~~~ or unknown label opens a block we simply pass over
                elif label == "":
                    fence, buf = "", []
                else:
                    problems.append(f"line {n}: unknown block label ~~~{label}")
                    fence, buf = "", []
            else:
                if fence in BLOCKS and cur is not None:
                    blocks[fence] = buf
                fence, buf = None, []
            continue
        if fence is not None:
            buf.append(raw)
            continue

        h = _HEAD.match(raw)
        if h:
            flush(n)
            cur = {"id": h.group("id"), "title": h.group("title"), "line": n, "meta": {}}
            continue
        # A `##` section heading ends the current case: metadata must not leak
        # across a section boundary.
        if raw.startswith("## "):
            flush(n)
            continue
        if cur is not None:
            mm = _META.match(raw)
            if mm:
                key = mm.group("key")
                if key not in KNOWN_META:
                    problems.append(f"{cur['id']} (line {n}): unknown metadata key '{key}'")
                cur["meta"][key] = mm.group("val")

    flush(len(text.splitlines()) + 1)
    if fence is not None:
        problems.append("file ends inside an unclosed ~~~ block")

    seen: dict[str, int] = {}
    for c in cases:
        if c.id in seen:
            problems.append(f"duplicate id {c.id!r} (lines {seen[c.id]} and {c.line})")
        seen[c.id] = c.line
        if c.attach and not c.attachment().exists():
            problems.append(f"{c.id}: attach {c.attach!r} does not exist")
    return cases, problems


def load_cases(path: Path = PROMPTS_MD, tags: set[str] | None = None,
               ids: set[str] | None = None, include_skipped: bool = False) -> list[Case]:
    cases, problems = parse(path.read_text())
    if problems:
        raise ValueError("docs/prompts.md has problems:\n  " + "\n  ".join(problems))
    out = cases
    if not include_skipped:
        out = [c for c in out if not c.skip]
    if tags:
        out = [c for c in out if tags & set(c.tags)]
    if ids:
        out = [c for c in out if c.id in ids]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", action="store_true", help="lint and exit non-zero on problems")
    ap.add_argument("--list", action="store_true", help="one line per case")
    ap.add_argument("--show", metavar="ID", help="print one case in full")
    ap.add_argument("--json", action="store_true", help="all cases as JSON")
    ap.add_argument("--tag", action="append", default=[], help="filter by tag (repeatable)")
    a = ap.parse_args()

    cases, problems = parse(PROMPTS_MD.read_text())
    if a.validate or problems:
        for p in problems:
            print(f"  PROBLEM  {p}", file=sys.stderr)
        print(f"{len(cases)} cases parsed, {len(problems)} problems")
        if problems:
            return 1
        if a.validate:
            skipped = sum(1 for c in cases if c.skip)
            tags: dict[str, int] = {}
            for c in cases:
                for t in c.tags:
                    tags[t] = tags.get(t, 0) + 1
            print(f"  runnable: {len(cases) - skipped}   skipped: {skipped}")
            print("  tags: " + ", ".join(f"{k}={v}" for k, v in sorted(tags.items())))
            return 0

    sel = [c for c in cases if not a.tag or set(a.tag) & set(c.tags)]
    if a.json:
        print(json.dumps([c.as_dict() for c in sel], indent=2, ensure_ascii=False))
    elif a.show:
        for c in cases:
            if c.id == a.show:
                print(f"=== {c.id} · {c.title}")
                for k, v in c.as_dict().items():
                    if k in ("prompt", "expect"):
                        print(f"--- {k}:\n{v}")
                    elif v:
                        print(f"    {k}: {v}")
                return 0
        print(f"no case {a.show!r}", file=sys.stderr)
        return 1
    else:
        for c in sel:
            mark = "SKIP " if c.skip else "     "
            print(f"{mark}{c.id:<6} {c.title[:52]:<52} tags={','.join(c.tags) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
