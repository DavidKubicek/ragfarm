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
