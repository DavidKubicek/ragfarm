#!/usr/bin/env bash
# scripts/lib-models.sh — shared helpers for fetch-llm.sh / fetch-encoder.sh / deploy.sh.
#
# SOURCE this (do not execute). Provides:
#   - tiny logging helpers (info/ok/warn/die) — guarded so sourcing into deploy.sh,
#     which defines its own, never clobbers deploy.sh's versions.
#   - env_upsert FILE KEY VALUE   — idempotently set KEY=VALUE in an .env-style file
#   - hf_snapshot REPO DEST [REVISION]  — download/reuse a full HF repo snapshot into
#     DEST as REAL FILES (no symlink farm). LATEST revision by default. Auto-selects
#     the fastest weight format (safetensors > pytorch_model.bin) and skips the
#     redundant format + unused bulk (onnx exports, images/docs) for size/speed —
#     NOT for security (see the model-format policy). Reuses shared HF-cache blobs
#     when present (no re-download).
#   - hf_snapshot_allow REPO DEST ALLOW_GLOB  — INCLUDE-filtered (for picking one
#     GGUF file/shard-set out of a repo that hosts many quantizations).
#   - restart_units UNIT... — restart the given systemd unit(s) via passwordless
#     sudo; --no-restart on the caller (or NO_RESTART=1 env) skips it. Fetch/
#     activate scripts call this at the end so a model swap is immediate — no
#     "now run this restart command yourself" step the user forgets.
#
# All HF calls go through the huggingface_hub PYTHON API (not the `hf`/
# `huggingface-cli` binaries) so behavior is one code path regardless of which CLI
# version (if any) happens to be on PATH.
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_PY="${VENV_PY:-$REPO_ROOT/.venv/bin/python}"

# ---- logging (guarded — don't clobber a caller's own definitions) -----------
if ! declare -F die >/dev/null 2>&1; then
	_c()  { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
	info(){ printf '  %s\n' "$*"; }
	ok()  { printf '  %sOK%s   %s\n' "$(_c 32)" "$(_c 0)" "$*"; }
	warn(){ printf '  %sWARN%s %s\n' "$(_c 33)" "$(_c 0)" "$*" >&2; }
	die() { printf '\n%sFAIL%s %s\n' "$(_c 31)" "$(_c 0)" "$*" >&2; exit 1; }
fi

[ -x "$VENV_PY" ] || die "venv python not found at $VENV_PY — run deploy.sh's venv phase first"

# env_upsert FILE KEY VALUE — set KEY=VALUE in FILE, creating FILE / the line if
# absent, replacing it in place if present. Preserves every other line untouched.
env_upsert() {
	local file="$1" key="$2" value="$3"
	touch "$file"
	if grep -q "^${key}=" "$file" 2>/dev/null; then
		local tmp; tmp="$(mktemp)"
		awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' "$file" > "$tmp"
		mv "$tmp" "$file"
	else
		printf '%s=%s\n' "$key" "$value" >> "$file"
	fi
}

# hf_snapshot REPO DEST [REVISION] — snapshot as real files, LATEST by default.
# Auto-picks the fastest weight format present (safetensors > pytorch_model.bin) and
# excludes the redundant format + unused bulk (onnx/openvino exports, images/docs)
# for download size / load speed — NOT for security. Small companion weights (e.g.
# bge-m3's load-bearing sparse_linear.pt head) are KEPT. Prints the local path.
hf_snapshot() {
	local repo="$1" dest="$2" revision="${3:-}"
	"$VENV_PY" - "$repo" "$dest" "$revision" <<'PY'
import sys, os, shutil
from huggingface_hub import snapshot_download, list_repo_files
repo, dest, revision = sys.argv[1], sys.argv[2], (sys.argv[3] or None)
have_safetensors = any(f.endswith(".safetensors") for f in list_repo_files(repo, revision=revision))
# Keep only what the model LOADS (weights + config + tokenizer). Drop repo bloat:
# ONNX/OpenVINO exports, images, docs/readmes, git metadata. NOT for security.
ignore = ["onnx/*", "openvino/*", "*.onnx", "*.onnx_data",
          "imgs/*", "*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.pdf",
          "*.md", ".gitattributes", ".gitignore", "*.DS_Store"]
if have_safetensors:                                   # fast format available -> skip the slow/redundant one
    ignore += ["*.bin", "pytorch_model*", "tf_model*", "flax_model*"]
path = snapshot_download(repo, revision=revision, local_dir=dest, ignore_patterns=ignore)
shutil.rmtree(os.path.join(dest, ".cache"), ignore_errors=True)   # HF local_dir download-metadata
print(path, end="")
PY
}

# hf_pick_mmproj REPO MAIN_FILE_GLOB — auto-detect a vision model's projector
# GGUF. Lists REPO's files; if none contain "mmproj", prints nothing (text-only
# model, not an error). Otherwise picks the SMALLEST reasonable one, cheapest VRAM
# first — a projector's own quant barely affects output quality (it's a tiny
# adapter, not the reasoning model), so there's no reason to default to the
# biggest file: prefers a name sharing MAIN_FILE_GLOB's quant token (rare — most
# repos don't offer a matching mmproj quant), else Q8_0 (smallest that most repos
# actually ship; confirmed on ggml-org/Qwen2.5-VL-7B-Instruct-GGUF: Q8_0 853MB vs
# f16 1354MB, same architecture), else f16, else bf16, else alphabetically first.
# Prints the chosen filename (repo-relative), or nothing.
hf_pick_mmproj() {
	local repo="$1" main_glob="$2"
	"$VENV_PY" - "$repo" "$main_glob" <<'PY'
import sys, re
from huggingface_hub import list_repo_files
repo, main_glob = sys.argv[1], sys.argv[2]
files = list_repo_files(repo)
cands = sorted(f for f in files if "mmproj" in f.lower() and f.lower().endswith(".gguf"))
if not cands:
    sys.exit(0)

def has_token(fname, token):
    # word-boundary match so "f16" doesn't false-match inside "bf16"
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", fname.lower()) is not None

m = re.search(r"(q\d_k_[ms]|q\d_\d|f16|f32|bf16)", main_glob, re.I)
quant = m.group(1).lower() if m else None
pick = None
if quant:
    pick = next((f for f in cands if has_token(f, quant)), None)
for fallback in ("q8_0", "f16", "bf16"):
    if pick:
        break
    pick = next((f for f in cands if has_token(f, fallback)), None)
print(pick or cands[0], end="")
PY
}

# hf_snapshot_allow REPO DEST ALLOW_GLOB — include-filtered snapshot (e.g. one
# GGUF quant out of a repo hosting several). Prints the resolved local path.
hf_snapshot_allow() {
	local repo="$1" dest="$2" allow="$3"
	"$VENV_PY" - "$repo" "$dest" "$allow" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, dest, allow = sys.argv[1], sys.argv[2], sys.argv[3]
print(snapshot_download(repo, local_dir=dest, allow_patterns=[allow]))
PY
}

# slugify REPO — "BAAI/bge-m3" -> "bge-m3" (the models/<kind>/<slug>/ dir name)
slugify() { basename "$1" | tr '[:upper:]' '[:lower:]'; }

# restart_units UNIT... — restart the given systemd unit(s) via sudo. Honors
# NO_RESTART=1 (used by callers with --no-restart). Uses sudo -n first so
# non-interactive contexts don't hang on a password prompt; falls back to a
# regular sudo (which will prompt on tty) if the passwordless attempt fails.
# On failure, prints the manual command instead of erroring out — the .env
# update already succeeded and the user can re-apply on their own.
restart_units() {
	if [ -n "${NO_RESTART:-}" ]; then
		info "NO_RESTART set — skipping systemctl; restart manually to apply:"
		info "    sudo systemctl restart $*"
		return 0
	fi
	info "restarting $* (sudo systemctl restart)"
	if sudo -n systemctl restart "$@" 2>/dev/null; then
		ok "restarted: $*"
		return 0
	fi
	# passwordless didn't work — try interactively (this will prompt on a tty)
	if sudo systemctl restart "$@"; then
		ok "restarted: $*"
		return 0
	fi
	warn "restart failed; apply manually:  sudo systemctl restart $*"
	return 1
}
