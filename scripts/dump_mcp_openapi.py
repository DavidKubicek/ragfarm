#!/usr/bin/env python3
"""
dump_mcp_openapi.py — enumerate every MCP registered in mcpo and dump its
openapi.json, so you can see the full request/response schema AND the exact tool
name the model is shown (openapi `operationId`, e.g. tool_search_corpus_post).

WHY: OWUI presents tools to the model by their mcpo operationId, not by the MCP's
internal tool name. When you write routing hints in the system prompt, or debug why
the model calls (or fails to call) a tool, this is the ground truth of what the
model actually sees.

The mount list comes from the authoritative mcpo config
(services/mcp-gateway/mcpo-config.json); each mount is served at
MCPO_URL/<mount>/openapi.json.

USAGE
  scripts/dump_mcp_openapi.py                 # operationId summary for every mount
  scripts/dump_mcp_openapi.py --full          # + the full openapi.json per mount
  scripts/dump_mcp_openapi.py --full rag      # just one mount, full schema

ENV (defaults match the loopback deployment):
  MCPO_URL=http://127.0.0.1:8000
  MCPO_CONFIG=services/mcp-gateway/mcpo-config.json
Only needs `requests`.
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
MCPO_URL = os.environ.get("MCPO_URL", "http://127.0.0.1:8000").rstrip("/")
MCPO_CONFIG = os.environ.get("MCPO_CONFIG", str(REPO / "services/mcp-gateway/mcpo-config.json"))


def mount_names(only: list[str]) -> list[str]:
    if only:
        return only
    try:
        cfg = json.loads(pathlib.Path(MCPO_CONFIG).read_text())
        return list(cfg.get("mcpServers", {}).keys())
    except Exception as e:
        print(f"  ! can't read {MCPO_CONFIG} ({e}); falling back to a probe list", file=sys.stderr)
        return ["rag", "placement", "host-control"]


def summarize(mount: str, spec: dict):
    """Print each operation's tool name (operationId) + its arg property names."""
    print(f"\n### {mount}  ({MCPO_URL}/{mount}/openapi.json)")
    paths = spec.get("paths", {})
    if not paths:
        print("  (no paths — mount not ready? try after mcpo has mounted all backends)")
        return
    comps = spec.get("components", {}).get("schemas", {})
    for path, methods in paths.items():
        for method, op in methods.items():
            opid = op.get("operationId", "?")
            # resolve the request body schema name -> its property list
            props = []
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            ref = body.get("$ref", "")
            if ref:
                sch = comps.get(ref.split("/")[-1], {})
                req = set(sch.get("required", []))
                props = [f"{k}{'*' if k in req else ''}" for k in sch.get("properties", {})]
            desc = (op.get("description") or op.get("summary") or "").strip().splitlines()
            print(f"  TOOL NAME (operationId): {opid}")
            print(f"    {method.upper()} {path}   args: {', '.join(props) or '(none)'}   (* = required)")
            if desc:
                print(f"    desc: {desc[0][:90]}")


def main():
    ap = argparse.ArgumentParser(description="dump each registered MCP's openapi + tool names")
    ap.add_argument("mounts", nargs="*", help="mounts to dump (default: all from mcpo config)")
    ap.add_argument("--full", action="store_true", help="also print the full openapi.json per mount")
    args = ap.parse_args()

    names = mount_names(args.mounts)
    print(f"mcpo={MCPO_URL}  mounts={names}")
    for m in names:
        try:
            spec = requests.get(f"{MCPO_URL}/{m}/openapi.json", timeout=10).json()
        except Exception as e:
            print(f"\n### {m}: ERROR fetching openapi ({e})", file=sys.stderr)
            continue
        summarize(m, spec)
        if args.full:
            print(f"--- {m} openapi.json ---")
            print(json.dumps(spec, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
