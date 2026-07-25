#!/usr/bin/env bash
# scripts/fetch-llm.sh — fetch/swap the generative LLM GGUF (ADR-0001).
#
# Downloads a GGUF LLM from Hugging Face into models/llm/<slug>/ and writes
# LLM_GGUF_PATH into .env, so ragfarm-llama.service (and scripts/deploy.sh) pick it
# up on the next start — no unit edits, no code changes. Idempotent: a target
# already on disk is left alone unless --force is given. This is the ONE place
# model-download logic for the LLM lives; deploy.sh calls this script rather than
# duplicating it.
#
# USAGE
#   scripts/fetch-llm.sh                                  # default: Qwen2.5-7B-Instruct Q4_K_M
#   scripts/fetch-llm.sh --list                            # show known-good LLMs, then exit
#   scripts/fetch-llm.sh --repo <hf-repo> --file <glob>    # swap to a different GGUF model
#   scripts/fetch-llm.sh --repo <hf-repo> --file <glob> --mmproj <glob>   # vision model
#                                                            # (also fetches the mmproj GGUF)
#   scripts/fetch-llm.sh --force                           # re-fetch even if present
#
# VISION MODELS (mmproj): a vision-language GGUF (e.g. Qwen2.5-VL) needs a SECOND
# small GGUF — the multimodal projector — passed to llama-server as `--mmproj`. Pass
# its glob via --mmproj to fetch it alongside the main file. Either way, after any
# fetch this script re-scans DEST_DIR and writes LLM_GGUF_MMPROJ from whatever
# `*mmproj*.gguf` file is present (HF vision repos name it that way — e.g.
# ggml-org/Qwen2.5-VL-7B-Instruct-GGUF ships mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf)
# — and CLEARS LLM_GGUF_MMPROJ when none is found, so switching back to a
# text-only model doesn't leave a stale --mmproj pointing at the wrong model.
# `scripts/activate-llm.sh` does the same detection when picking among models
# already on disk, without re-downloading.
#
# After fetching, restart the unit to pick it up:
#   sudo systemctl restart ragfarm-llama        # or: scripts/stack.sh restart
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=./lib-models.sh
source scripts/lib-models.sh

# Known-good tool-calling GGUF LLMs (hints for --list; --file selects the quant).
# mmproj column is blank for text-only models; vision models list its glob too.
LLMS=(
	"Qwen/Qwen2.5-7B-Instruct-GGUF | qwen2.5-7b-instruct-q4_k_m*.gguf | | ~4.7GB Q4_K_M. DEFAULT; fits the iGPU."
	"Qwen/Qwen2.5-14B-Instruct-GGUF | qwen2.5-14b-instruct-q4_k_m*.gguf | | ~9GB Q4_K_M. Better tool discipline; more VRAM."
	"Qwen/Qwen2.5-32B-Instruct-GGUF | qwen2.5-32b-instruct-q4_k_m*.gguf | | ~20GB Q4_K_M. Prod-class (ADR-0008 HW note)."
	"bartowski/Meta-Llama-3.1-8B-Instruct-GGUF | Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf | | ~4.9GB. Llama alternative, tool-calling."
	"ggml-org/Qwen2.5-VL-7B-Instruct-GGUF | Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf | mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf | ~5GB + ~1.3GB mmproj. VISION — pass both --file and --mmproj."
)

list_models() {
	printf 'Known-good tool-calling GGUF LLMs (pick by VRAM budget; --file selects the quant):\n\n'
	printf '  %-38s %-38s %-34s %s\n' "REPO (--repo)" "FILE (--file)" "MMPROJ (--mmproj)" "notes"
	local m r f p n
	for m in "${LLMS[@]}"; do
		IFS='|' read -r r f p n <<< "$m"
		printf '  %-38s %-38s %-34s %s\n' "$(echo "$r" | xargs)" "$(echo "$f" | xargs)" "$(echo "${p:--}" | xargs)" "$(echo "$n" | xargs)"
	done
	printf '\nGGUF is downloaded directly (no conversion). Split shards auto-load from the first.\n'
	printf 'Vision models: pass BOTH --file (main GGUF) and --mmproj (projector GGUF).\n'
}

HF_REPO="Qwen/Qwen2.5-7B-Instruct-GGUF"
FILE_GLOB="qwen2.5-7b-instruct-q4_k_m*.gguf"
MMPROJ_GLOB=""
FORCE=0
ENV_FILE=".env"

while [ $# -gt 0 ]; do
	case "$1" in
		--list)     list_models; exit 0 ;;
		--repo)     HF_REPO="$2"; shift 2 ;;
		--file)     FILE_GLOB="$2"; shift 2 ;;
		--mmproj)   MMPROJ_GLOB="$2"; shift 2 ;;
		--force)    FORCE=1; shift ;;
		--env-file) ENV_FILE="$2"; shift 2 ;;
		-h|--help)  sed -n '2,31p' "$0"; exit 0 ;;
		*) die "unknown arg: $1 (see --help / --list)" ;;
	esac
done

SLUG="$(slugify "$HF_REPO")"
DEST_DIR="models/llm/$SLUG"
mkdir -p "$DEST_DIR"

# first_shard excludes *mmproj* so a vision projector never gets picked as the main
# model (case-insensitive; HF vision repos consistently use "mmproj" in the name).
first_shard() { find "$DEST_DIR" -maxdepth 1 -iname '*.gguf' -not -iname '*mmproj*' 2>/dev/null | LC_ALL=C sort | head -1; }
mmproj_file()  { find "$DEST_DIR" -maxdepth 1 -iname '*mmproj*.gguf' 2>/dev/null | LC_ALL=C sort | head -1; }

existing="$(first_shard)"
if [ -n "$existing" ] && [ "$FORCE" != 1 ]; then
	ok "LLM GGUF already present: $existing"
else
	info "downloading $HF_REPO ($FILE_GLOB) -> $DEST_DIR"
	hf_snapshot_allow "$HF_REPO" "$DEST_DIR" "$FILE_GLOB" >/dev/null
	existing="$(first_shard)"
	[ -n "$existing" ] || die "download completed but no .gguf matching '$FILE_GLOB' landed in $DEST_DIR"
fi

if [ -n "$MMPROJ_GLOB" ]; then
	mmproj_existing="$(mmproj_file)"
	if [ -n "$mmproj_existing" ] && [ "$FORCE" != 1 ]; then
		ok "mmproj GGUF already present: $mmproj_existing"
	else
		info "downloading mmproj ($MMPROJ_GLOB) -> $DEST_DIR"
		hf_snapshot_allow "$HF_REPO" "$DEST_DIR" "$MMPROJ_GLOB" >/dev/null
		[ -n "$(mmproj_file)" ] || die "download completed but no .gguf matching '$MMPROJ_GLOB' landed in $DEST_DIR"
	fi
fi

env_upsert "$ENV_FILE" LLM_GGUF_PATH "$REPO_ROOT/$existing"
ok "LLM_GGUF_PATH=$REPO_ROOT/$existing  ($ENV_FILE)"

# Always re-detect (not just when --mmproj was passed): a *mmproj*.gguf might already
# sit in DEST_DIR from a prior fetch, or none might be present — either way the .env
# key must reflect DEST_DIR's actual contents. Clearing on "none found" matters: it
# stops a stale --mmproj from a previous vision model leaking into a text-only launch.
mmproj_now="$(mmproj_file)"
if [ -n "$mmproj_now" ]; then
	env_upsert "$ENV_FILE" LLM_GGUF_MMPROJ "$REPO_ROOT/$mmproj_now"
	ok "LLM_GGUF_MMPROJ=$REPO_ROOT/$mmproj_now  ($ENV_FILE)"
else
	env_upsert "$ENV_FILE" LLM_GGUF_MMPROJ ""
fi

info "restart to apply: sudo systemctl restart ragfarm-llama   (or scripts/stack.sh restart)"
