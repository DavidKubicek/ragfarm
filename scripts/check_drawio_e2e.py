#!/usr/bin/env python3
"""check_drawio_e2e.py — does the vision preset still produce a draw.io diagram
that actually renders?

Every failure on this path is silent. Open WebUI shows the same empty white box
whether the mirror is missing, the host URL is unreachable from the client, the
CSP blocked the script, or the model emitted invalid XML — and nothing is logged
anywhere. That makes it exactly the kind of regression that survives for days
(it did: 2026-08-09). This script turns the silence into a pass/fail line.

It drives a live slot with the REAL system prompt from setup_openwebui.py, using
the demo task that first exposed the bug: "reverse the arrows in this diagram",
with the user supplying their own XML. That task is deliberately chosen — it
catches both families of defect at once, malformed output (elision, reserved
ids, missing geometry) and lazy output (re-deriving the diagram instead of
editing the user's, which silently discards their layout and colours).

Rendering itself is NOT checked here; that needs a browser. Verify it once with
  firefox --headless --screenshot /home/dave/ragfarm/logs/x.png <url>
against tests/fixtures/drawio-wrapper-reference.html (the wrapper verbatim), or
against this script's --save output for a specific model answer. Note the snap
confinement: the screenshot path must live under /home/dave or no file appears.

USAGE
    .venv/bin/python scripts/check_drawio_e2e.py              # every live slot
    .venv/bin/python scripts/check_drawio_e2e.py --port 8080  # just this one
    .venv/bin/python scripts/check_drawio_e2e.py --save out/  # keep the HTML
"""
import argparse
import pathlib
import re
import sys

import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "infra" / "openwebui"))
import setup_openwebui as owui  # noqa: E402

# Slot N -> 8080 + 2N (8081 is the reranker). Matches scripts/activate_llm.py.
PORTS = [8080, 8082]
TASK = ("V přiloženém Draw.io grafu obrať směr šípek mezi Frontend → API → Database "
        "opačným směrem. Prezentuj výstup opět ve formátu Draw.io.\n\n")
INPUT_XML = REPO_ROOT / "tests" / "fixtures" / "dependency_input.drawio"


def checks(html: str) -> dict[str, bool]:
    """Each entry is a defect seen in real model output, not a hypothetical."""
    return {
        # The old prompt's own placeholder said "...your XML here...", and the
        # model duly wrote `... />` in place of real attributes.
        "no '...' elision": not re.search(r"\.\.\.|…", html),
        # id 0 and 1 are the root and the default layer; a vertex on either is
        # a hard conflict that draws nothing.
        "no reserved id on content": not re.search(
            r'<mxCell id="[01]"[^>]*(vertex|edge|value)=', html),
        "root+layer present": '<mxCell id="0"' in html and '<mxCell id="1" parent="0"' in html,
        # A cell with no geometry has no position and no size.
        "geometry on every cell": html.count("<mxGeometry") >= 5,
        # The next four: edit the user's XML in place, do not re-derive it.
        "kept user ids A/B/C": all(f'id="{i}"' in html for i in "ABC"),
        "e1 reversed (B->A)": re.search(r'id="e1"[^>]*source="B"[^>]*target="A"', html) is not None,
        "e2 reversed (C->B)": re.search(r'id="e2"[^>]*source="C"[^>]*target="B"', html) is not None,
        "kept fill colours": html.count("fillColor=#") == 3,
        # The viewer reads the XML from the data-mxgraph JSON, never from a
        # child <xml> element; the bootstrap in the wrapper is what bridges it.
        "uses bootstrap wrapper": "ragfarm-xml" in html and "data-mxgraph" in html,
        # 127.0.0.1 here would be the CLIENT's loopback for a remote browser.
        "correct viewer host": f"{owui.VIEWER_BASE}/js/viewer-static.min.js" in html,
    }


def run_slot(port: int, save: pathlib.Path | None) -> bool:
    base = f"http://127.0.0.1:{port}"
    try:
        alias = requests.get(f"{base}/v1/models", timeout=10).json()["data"][0]["id"]
    except requests.RequestException as e:
        print(f":{port}  SKIP — no slot here ({e.__class__.__name__})")
        return True  # a slot that is not running is not a failure

    r = requests.post(f"{base}/v1/chat/completions", timeout=900, json={
        "model": alias, "temperature": 0.6, "max_tokens": 8192,
        "messages": [{"role": "system", "content": owui.PROMPT_BODIES["vision"]},
                     {"role": "user", "content": TASK + INPUT_XML.read_text()}]})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    answer = msg.get("content") or ""
    print(f"=== {alias} :{port} | reasoning {len(msg.get('reasoning') or '')} chars"
          f" | answer {len(answer)} chars")

    block = re.search(r"```html\n(.*?)```", answer, re.S)
    if not block:
        print("  [FAIL] no ```html block in the answer")
        return False
    html = block.group(1)
    if save:
        save.mkdir(parents=True, exist_ok=True)
        out = save / f"{alias}.html"
        out.write_text(html)
        print(f"  saved {out}")

    result = checks(html)
    for name, passed in result.items():
        print(f"  [{'ok ' if passed else 'FAIL'}] {name}")
    return all(result.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, action="append", help="slot port (repeatable)")
    ap.add_argument("--save", type=pathlib.Path, help="directory to write each answer's HTML into")
    a = ap.parse_args()

    owui.check_drawio_viewer()
    ok = all(run_slot(p, a.save) for p in (a.port or PORTS))
    print("PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
