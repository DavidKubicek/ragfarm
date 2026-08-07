#!/usr/bin/env python3
"""activate_llm.py — bind a registered model to a vLLM SLOT (ADR-0013).

Replaces scripts/activate-llm.sh, which was GGUF/llama.cpp-shaped and is retired.

WHAT A SLOT IS. vLLM serves exactly ONE base model per process — there is no
multi-model mode (several --served-model-name values are aliases for the SAME
weights; LoRA adapters need a shared base). So a "slot" is our abstraction over
one vLLM instance: slot N = systemd unit ragfarm-vllm@N on port 8080+2N, driven
by .env.slotN. Running two slots means two resident models, which is what makes
mid-chat model switching possible in Open WebUI with the conversation intact.

  slot 0 -> 127.0.0.1:8080/v1      slot 1 -> 127.0.0.1:8082/v1
  (8081 is the reranker, 8090 the embedder, 8000 mcpo, 8104 rag-retrieval)

MEMORY BUDGET — the one thing you cannot skip. vLLM's --gpu-memory-utilization is
a fraction of TOTAL device memory, and each instance computes it INDEPENDENTLY:
two slots at the single-model 0.50 will OOM, because neither knows about the
other. This script derives a per-slot fraction instead:

    util = (weights + kv_target + OVERHEAD) / TOTAL_GIB

with weights measured from the checkpoint on disk. It refuses to activate if the
sum across slots would exceed BUDGET_CEILING, leaving room for the embedder
(~1.6 GB), the reranker (~0.9 GB), the container plane and the OS.

USAGE
  activate_llm.py -s 0                       interactive picker
  activate_llm.py -s 0 -m Qwen3-VL-8B-Thinking
  activate_llm.py -s 1 -a qwen3-vl-32b-thinking-nvfp4
  activate_llm.py --status                   show slots, models, budget
  activate_llm.py -s 1 --clear               free the slot (stop + unbind)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_DIR = REPO_ROOT / "models" / "llm"
REGISTRY = LLM_DIR / "active.json"

TOTAL_GIB = 121.7          # GB10 unified memory as the driver reports it
OVERHEAD_GIB = 3.0         # CUDA context, activations, cudagraph pools
# Per slot. Lowered 12 -> 8 on 2026-08-05 to fit the official FP8 pair
# (30.1 GB MoE + 33 GB dense) under BUDGET_CEILING. Measured basis: slot 0 was
# granted 32.86 GiB and actually used 9.91 GiB of KV at 32k context, so 12 was
# generous. Override with RAGFARM_KV_GIB when a slot needs more.
KV_TARGET_GIB = float(os.environ.get("RAGFARM_KV_GIB", "8.0"))
BUDGET_CEILING = 0.72      # leave >=28% for embedder + reranker + containers + OS
BASE_PORT = 8080
PORT_STRIDE = 2            # slot 0 -> 8080, slot 1 -> 8082 (8081 is the reranker)


def load() -> dict:
    reg = json.loads(REGISTRY.read_text())
    validate(reg)
    return reg


def validate(reg: dict) -> None:
    """Fail loudly on a registry that cannot produce a working deployment.

    These are NOT auto-repaired on purpose. `alias` is what vLLM advertises and
    what OWUI keys its preset on; `preset` is the OWUI model id; `model` is a
    directory. A duplicate in any of them is a naming decision only a human
    should make — silently disambiguating would produce a UI whose entries do not
    mean what their names say.
    """
    dl = reg.get("downloaded", [])
    problems: list[str] = []
    for field in ("model", "alias", "preset"):
        seen: dict[str, int] = {}
        for i, e in enumerate(dl):
            v = e.get(field)
            if not v:
                problems.append(f"downloaded[{i}] has no '{field}'")
                continue
            if v in seen:
                problems.append(
                    f"duplicate {field} {v!r}: downloaded[{seen[v]}] ({dl[seen[v]]['model']}) "
                    f"and downloaded[{i}] ({e['model']})")
            else:
                seen[v] = i
    for slot, idx in enumerate(reg.get("active", [])):
        if idx is None:
            continue
        if not isinstance(idx, int) or idx >= len(dl) or idx < 0:
            problems.append(f"active[{slot}] = {idx!r} is not a valid downloaded[] index")
    # the same model in two slots would start two vLLM instances on the same weights
    live = [i for i in reg.get("active", []) if isinstance(i, int) and 0 <= i < len(dl)]
    if len(set(live)) != len(live):
        problems.append(f"active{live} binds the same model to more than one slot")
    if problems:
        sys.exit("models/llm/active.json is inconsistent — fix it by hand:\n  "
                 + "\n  ".join(problems))


def save(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=4, ensure_ascii=False) + "\n")


def weights_gib(entry: dict) -> float:
    """Prefer the size fetch_llm.py recorded after Hub verification. Falling back to
    du is only for a hand-placed checkpoint — and a PARTIAL download would report
    a small size and silently under-allocate the slot, so callers must check
    is_complete() before trusting a du-derived figure."""
    if entry.get("size_gib"):
        return float(entry["size_gib"])
    d = LLM_DIR / entry["model"]
    if not d.exists():
        return 0.0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1073741824


def is_complete(entry: dict) -> bool:
    """A recorded size that matches disk within 1% means fetch verified it."""
    d = LLM_DIR / entry["model"]
    if not d.exists():
        return False
    if not entry.get("size_gib"):
        return True  # unknown provenance; treat as complete but weights_gib() warns
    on_disk = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1073741824
    return on_disk >= float(entry["size_gib"]) * 0.99


def util_for(entry: dict) -> float:
    return round((weights_gib(entry) + KV_TARGET_GIB + OVERHEAD_GIB) / TOTAL_GIB, 3)


def port_for(slot: int) -> int:
    return BASE_PORT + PORT_STRIDE * slot


def slot_env_path(slot: int) -> Path:
    return REPO_ROOT / f".env.slot{slot}"


def write_slot_env(slot: int, entry: dict) -> float:
    """Per-slot config the templated unit reads. .env stays the global source of
    truth; this file holds only what differs BETWEEN slots."""
    util = util_for(entry)
    body = f"""# generated by scripts/activate_llm.py — DO NOT EDIT BY HAND
# slot {slot}: {entry['model']}
LLM_MODEL_PATH={LLM_DIR / entry['model']}
LLM_SERVED_NAME={entry['alias']}
LLM_PORT={port_for(slot)}
# derived: ({weights_gib(entry):.1f} GiB weights + {KV_TARGET_GIB} KV + {OVERHEAD_GIB} overhead) / {TOTAL_GIB}
LLM_GPU_MEM_UTIL={util}
"""
    slot_env_path(slot).write_text(body)
    return util


def resolve(reg: dict, args) -> int:
    """-> index into downloaded[]"""
    dl = reg["downloaded"]
    if args.model:
        for i, e in enumerate(dl):
            if e["model"] == args.model:
                return i
        sys.exit(f"no registered model named {args.model!r}")
    if args.alias:
        for i, e in enumerate(dl):
            if e["alias"] == args.alias:
                return i
        sys.exit(f"no registered alias {args.alias!r}")
    print("\nRegistered models:")
    for i, e in enumerate(dl):
        on_disk = (LLM_DIR / e["model"]).exists()
        mark = " " if on_disk else "  [NOT DOWNLOADED]"
        print(f"  [{i}] {e['model']:<38} {weights_gib(e):5.1f} GiB  {e['alias']}{mark}")
    try:
        return int(input("\npick index: ").strip())
    except (ValueError, EOFError, KeyboardInterrupt):
        sys.exit("aborted")


def budget_report(reg: dict) -> float:
    total = 0.0
    for slot, idx in enumerate(reg.get("active", [])):
        if idx is None or idx >= len(reg["downloaded"]):
            continue
        total += util_for(reg["downloaded"][idx])
    return round(total, 3)


def cmd_status(reg: dict) -> int:
    print(f"{'slot':<5} {'port':<6} {'util':<7} {'model'}")
    for slot, idx in enumerate(reg.get("active", [])):
        if idx is None or idx >= len(reg["downloaded"]):
            print(f"{slot:<5} {port_for(slot):<6} {'-':<7} (empty)")
            continue
        e = reg["downloaded"][idx]
        print(f"{slot:<5} {port_for(slot):<6} {util_for(e):<7} {e['model']}  [{e['alias']}]")
    tot = budget_report(reg)
    print(f"\ntotal GPU budget: {tot} of {BUDGET_CEILING} ceiling "
          f"(~{tot * TOTAL_GIB:.0f} GiB of {TOTAL_GIB:.0f})")
    return 0


def systemctl(*a: str) -> int:
    return subprocess.run(["sudo", "systemctl", *a]).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--slot", type=int, help="vLLM slot to bind (0-based)")
    ap.add_argument("-m", "--model", help="registry 'model' (directory name)")
    ap.add_argument("-a", "--alias", help="registry 'alias' (served name)")
    ap.add_argument("--clear", action="store_true", help="empty the slot and stop its unit")
    ap.add_argument("--status", action="store_true", help="show slots and budget")
    ap.add_argument("--no-restart", action="store_true", help="write config, do not touch systemd")
    args = ap.parse_args()

    reg = load()
    if args.status:
        return cmd_status(reg)
    if args.slot is None:
        ap.error("-s/--slot is required (or use --status)")

    active = reg.setdefault("active", [])
    while len(active) <= args.slot:
        active.append(None)

    if args.clear:
        active[args.slot] = None
        save(reg)
        if not args.no_restart:
            systemctl("stop", f"ragfarm-vllm@{args.slot}.service")
        print(f"slot {args.slot} cleared")
        return 0

    idx = resolve(reg, args)
    entry = reg["downloaded"][idx]
    dest = LLM_DIR / entry["model"]
    if not dest.exists():
        sys.exit(f"{entry['model']} is registered but not on disk — run:\n"
                 f"  scripts/fetch_llm.py --sync")
    if not is_complete(entry):
        sys.exit(f"{entry['model']} is only PARTIALLY downloaded "
                 f"({weights_gib(entry):.1f} GiB expected). Activating it would size the GPU "
                 f"budget from an incomplete checkpoint.\n  scripts/fetch_llm.py --sync")

    # budget check BEFORE committing: what would the total be with this change?
    proposed = list(active)
    proposed[args.slot] = idx
    total = 0.0
    for i in proposed:
        if i is not None and i < len(reg["downloaded"]):
            total += util_for(reg["downloaded"][i])
    if total > BUDGET_CEILING:
        sys.exit(f"REFUSING: slots would need {total:.3f} of total GPU memory, ceiling is "
                 f"{BUDGET_CEILING}.\nFree a slot (--clear) or lower KV_TARGET_GIB.")

    active[args.slot] = idx
    save(reg)
    util = write_slot_env(args.slot, entry)
    print(f"slot {args.slot} -> {entry['model']}  alias={entry['alias']}  "
          f"port={port_for(args.slot)}  util={util}")
    print(f"total budget now {total:.3f} of {BUDGET_CEILING}")

    if args.no_restart:
        print("(--no-restart: systemd untouched)")
        return 0

    unit = f"ragfarm-vllm@{args.slot}.service"

    # SLOTS MUST START SEQUENTIALLY. vLLM profiles free GPU memory during engine
    # init; if a second instance is allocating at the same time, the first sees
    # free memory MOVE under it and dies with either
    #   AssertionError: Error in memory profiling. Initial free memory X, current Y
    # or, for the one that loses the race,
    #   ValueError: No available memory for the cache blocks
    # Both are startup races, not a wrong budget — observed 2026-08-05 activating
    # two slots back to back. So: wait for every other slot to finish starting
    # before restarting this one.
    others = [f"ragfarm-vllm@{s}.service"
              for s, i in enumerate(active) if i is not None and s != args.slot]
    for o in others:
        state = subprocess.run(["systemctl", "is-active", o],
                               capture_output=True, text=True).stdout.strip()
        if state == "activating":
            print(f"waiting for {o} to finish starting (vLLM cannot profile GPU "
                  f"memory while another slot is allocating)...")
            for _ in range(180):  # up to 30 min; a cold JIT build is genuinely slow
                time.sleep(10)
                if subprocess.run(["systemctl", "is-active", o],
                                  capture_output=True, text=True).stdout.strip() != "activating":
                    break
            else:
                sys.exit(f"{o} is still starting after 30 min — resolve that first")

    systemctl("restart", unit)
    print(f"restarted {unit} — first start on a new checkpoint is slow "
          f"(FlashInfer JIT); watch: journalctl -u {unit} -f")
    if others:
        print("NOTE: other slots exist — if you restart them, do it ONE AT A TIME "
              "and wait for each to answer /v1/models first.")
    rebind_openwebui()
    return 0


def rebind_openwebui() -> None:
    """Re-point Open WebUI's presets at whatever is now bound to the slots.

    THIS IS NOT OPTIONAL. OWUI presets carry the system prompt, the tool list and
    the sampler; they bind to a model by ALIAS. Change the alias in a slot without
    re-running setup and the preset still names the OLD alias, which no longer
    exists — so the only entries left in the model picker are the RAW base models,
    which have NO system prompt, NO tools and NO grounding rules. The UI looks
    fine and answers are silently ungrounded.

    Observed for real on 2026-08-07 after switching both slots NVFP4 -> FP8.
    """
    setup = REPO_ROOT / "infra" / "openwebui" / "setup_openwebui.py"
    if not setup.exists():
        print("WARNING: setup_openwebui.py missing — presets NOT rebound")
        return
    env = dict(os.environ)
    envfile = REPO_ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if line.startswith(("OWUI_URL=", "OWUI_EMAIL=", "OWUI_PASSWORD=", "OWUI_TOKEN=")):
                k, _, v = line.partition("=")
                env.setdefault(k, v)
    env.setdefault("OWUI_URL", "http://127.0.0.1:3000")
    py = REPO_ROOT / ".venv" / "bin" / "python"
    print("rebinding Open WebUI presets...")
    r = subprocess.run([str(py), str(setup)], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print("WARNING: preset rebind FAILED — the model picker may only offer raw\n"
              "         base models (no system prompt, no tools). Run by hand:\n"
              f"         {py} {setup}")
        print((r.stderr or r.stdout).strip()[-500:])
    else:
        for line in r.stdout.strip().splitlines():
            if "preset" in line or "endpoints" in line:
                print("  " + line)


if __name__ == "__main__":
    sys.exit(main())
