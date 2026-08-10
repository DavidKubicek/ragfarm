#!/usr/bin/env python3
"""fetch_llm.py — registry-driven model downloader for ragfarm (ADR-0013).

Replaces scripts/fetch-llm.sh, which was GGUF/llama.cpp-shaped and is retired.

THE REGISTRY IS THE SOURCE OF TRUTH: models/llm/active.json.
  downloaded[]  every model this deployment should have on disk
  active[]      indexes into downloaded[]; one per vLLM SLOT (see activate_llm.py)

USAGE
  fetch_llm.py -m <hf-repo>     register + download one model, append to downloaded[]
  fetch_llm.py --sync           download everything in downloaded[] that is missing,
                                and delete any models/llm/<dir> not in the registry
  fetch_llm.py --sync --yes     actually delete (without --yes, deletions are dry-run)
  fetch_llm.py --verify         re-check on-disk files against the Hub, fix nothing

WHY curl AND NOT huggingface_hub: on this network huggingface_hub hangs
indefinitely on sockets that die mid-transfer — HF_HUB_DOWNLOAD_TIMEOUT does not
fire, the process stays alive at 0 B/s and never returns control to a retry loop.
curl with --speed-limit/--speed-time detects exactly that (socket open, delivering
nothing), aborts, and -C - resumes without losing bytes. Measured on 2026-08-04:
huggingface_hub stalled twice and lost progress; curl completed 67 GB across
several dropouts. The Hub API is still used for metadata (file list + sizes).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_DIR = REPO_ROOT / "models" / "llm"
REGISTRY = LLM_DIR / "active.json"

# curl tuning: give up on a transfer delivering <50 KB/s for 30 s, then resume.
SPEED_LIMIT, SPEED_TIME, PASSES = "50000", "30", 200


def load() -> dict:
    if not REGISTRY.exists():
        return {"active": [], "downloaded": []}
    return json.loads(REGISTRY.read_text())


def save(reg: dict) -> None:
    """Always human-readable and indented — this file is git-tracked and reviewed."""
    REGISTRY.write_text(json.dumps(reg, indent=4, ensure_ascii=False) + "\n")


def hub_files(repo: str) -> list[tuple[str, int | None]]:
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo, files_metadata=True)
    return [(s.rfilename, s.size) for s in info.siblings if not s.rfilename.startswith(".")]


def verify(repo: str, dest: Path) -> tuple[list[str], list[str]]:
    """-> (missing, size-mismatched). Empty/empty means the copy is complete."""
    missing, bad = [], []
    for fname, size in hub_files(repo):
        p = dest / fname
        if not p.exists():
            missing.append(fname)
        elif size is not None and p.stat().st_size != size:
            bad.append(fname)
    return missing, bad


def fetch_file(repo: str, dest: Path, fname: str) -> bool:
    out = dest / fname
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{repo}/resolve/main/{fname}"
    for attempt in range(1, PASSES + 1):
        before = out.stat().st_size if out.exists() else 0
        rc = subprocess.run(
            ["curl", "-4", "-sSL", "-C", "-", "-o", str(out),
             "--speed-limit", SPEED_LIMIT, "--speed-time", SPEED_TIME,
             "--retry", "5", "--retry-delay", "5", "--retry-all-errors",
             "--max-time", "3600", url]
        ).returncode
        if rc == 0:
            return True
        now = out.stat().st_size if out.exists() else 0
        print(f"    retry {attempt} {fname}: at {now // 1048576} MiB "
              f"(+{(now - before) // 1048576} MiB this pass)", flush=True)
    return False


def download(entry: dict) -> bool:
    repo, dest = entry["repo"], LLM_DIR / entry["model"]
    print(f"\n=== {entry['model']}  <-  {repo}")
    dest.mkdir(parents=True, exist_ok=True)
    files = hub_files(repo)
    total = sum(s or 0 for _, s in files)
    print(f"    {len(files)} files, {total / 1e9:.1f} GB")
    for fname, size in files:
        p = dest / fname
        if p.exists() and size is not None and p.stat().st_size == size:
            continue  # already complete
        if not fetch_file(repo, dest, fname):
            print(f"    FAILED {fname}")
            return False
    missing, bad = verify(repo, dest)
    if missing or bad:
        print(f"    INTEGRITY FAIL: {len(missing)} missing, {len(bad)} size-mismatch")
        for f in (missing + bad)[:5]:
            print(f"      {f}")
        return False
    # Record the verified size. activate_llm.py sizes the GPU budget from THIS,
    # not from du: a half-finished checkpoint on disk would otherwise under-allocate.
    entry["size_gib"] = round(total / 1073741824, 1)
    print(f"    OK — {len(files)} files verified against the Hub ({entry['size_gib']} GiB)")
    return True


def slug(repo: str) -> str:
    """Directory name from a repo id. Keeps the upstream model name, drops the org."""
    return repo.split("/")[-1]


def cmd_add(args) -> int:
    reg = load()
    repo = args.model
    if any(e["repo"] == repo for e in reg["downloaded"]):
        print(f"already registered: {repo} — nothing to do")
        return 0
    name = slug(repo)
    entry = {
        "model": name,
        "repo": repo,
        "alias": args.alias or name.lower(),
        # No shared default: 'ragfarm-vision' is already taken by an existing
        # entry, so defaulting to it silently manufactures a duplicate.
        "preset": args.preset or f"ragfarm-{name.lower()}",
        "display": args.display or name,
        "profile": args.profile,
        "comment": args.comment or "",
    }

    # PRE-FLIGHT, BEFORE THE DOWNLOAD. activate_llm.py refuses to run at all on a
    # registry with a duplicate model/alias/preset — deliberately, because only a
    # human should decide which name wins. Discovering that after a 70 GB, hours
    # long transfer would be maddening, and worse, the duplicate would already be
    # in the registry and block every other slot operation until fixed by hand.
    clashes = [f"{f}={entry[f]!r} already used by downloaded[{i}] ({e['model']})"
               for f in ("model", "alias", "preset")
               for i, e in enumerate(reg["downloaded"]) if e.get(f) == entry[f]]
    if clashes:
        print("ERROR: refusing to register — the registry must stay unique:", file=sys.stderr)
        for c in clashes:
            print("  " + c, file=sys.stderr)
        print("\nPass explicit names, e.g.:\n"
              f"  fetch_llm.py -m {repo} \\\n"
              f"      --alias <served-name> --preset <owui-preset-id> \\\n"
              f"      --display '<UI label>' --profile vision-instruct", file=sys.stderr)
        return 1

    if not download(entry):
        print("download failed — NOT registering", file=sys.stderr)
        return 1
    reg["downloaded"].append(entry)
    save(reg)
    print(f"\nregistered as index {len(reg['downloaded']) - 1}: {entry['alias']}")
    return 0


def cmd_sync(args) -> int:
    reg = load()
    known = {e["model"] for e in reg["downloaded"]}
    active_models = {reg["downloaded"][i]["model"]
                     for i in reg.get("active", []) if i < len(reg["downloaded"])}
    rc = 0

    changed = False
    for e in reg["downloaded"]:
        dest = LLM_DIR / e["model"]
        if dest.exists():
            missing, bad = verify(e["repo"], dest)
            if not missing and not bad:
                print(f"present + verified: {e['model']}")
                if "size_gib" not in e:  # backfill for models fetched before this field existed
                    e["size_gib"] = round(
                        sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
                        / 1073741824, 1)
                    changed = True
                continue
            print(f"incomplete: {e['model']} ({len(missing)} missing, {len(bad)} bad) — refetching")
        if download(e):
            changed = True
        else:
            rc = 1
    if changed:
        save(reg)

    strays = [d for d in LLM_DIR.iterdir()
              if d.is_dir() and d.name not in known]
    if strays:
        print("\n--- not in registry ---")
        for d in strays:
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1e9
            if d.name in active_models:
                print(f"  REFUSING to delete {d.name} — referenced by active[]")
                continue
            if args.yes:
                shutil.rmtree(d)
                print(f"  DELETED {d.name} ({size:.1f} GB)")
            else:
                print(f"  would delete {d.name} ({size:.1f} GB)   [--yes to confirm]")
    return rc


def cmd_verify(args) -> int:
    reg = load()
    rc = 0
    for e in reg["downloaded"]:
        dest = LLM_DIR / e["model"]
        if not dest.exists():
            print(f"ABSENT    {e['model']}")
            rc = 1
            continue
        missing, bad = verify(e["repo"], dest)
        status = "OK       " if not missing and not bad else "INCOMPLETE"
        print(f"{status} {e['model']}  (missing {len(missing)}, mismatch {len(bad)})")
        if missing or bad:
            rc = 1
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("-m", "--model", help="HF repo id to register and download")
    g.add_argument("--sync", action="store_true",
                   help="download everything missing from the registry; report strays")
    g.add_argument("--verify", action="store_true",
                   help="check on-disk models against the Hub; change nothing")
    ap.add_argument("--yes", action="store_true",
                    help="with --sync: actually delete strays (default is dry-run)")
    ap.add_argument("--alias", help="served alias / unique id (default: dirname lowercased)")
    ap.add_argument("--preset", help="OWUI preset id (default: ragfarm-<dirname>); "
                    "must be unique across the registry")
    ap.add_argument("--display", help="OWUI display name")
    ap.add_argument("--profile", default="vision-thinking",
                    help="MODEL_TUNING profile: vision-thinking | vision-instruct")
    ap.add_argument("--comment", help="free-text note stored in the registry")
    args = ap.parse_args()

    if args.model and (args.yes or args.sync):
        ap.error("-m/--model takes no --sync/--yes")
    if args.sync:
        return cmd_sync(args)
    if args.verify:
        return cmd_verify(args)
    return cmd_add(args)


if __name__ == "__main__":
    sys.exit(main())
