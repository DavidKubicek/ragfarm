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
#   scripts/fetch-llm.sh --force                           # re-fetch even if present
#   scripts/fetch-llm.sh --no-restart                      # write .env, DON'T sudo systemctl
#
# On success this script AUTO-RESTARTS ragfarm-llama via sudo so the new model is
# immediately live — pass --no-restart if you're staging changes or batching. If
# sudo can't get privilege non-interactively AND the terminal has no tty, the
# restart is skipped with a WARN and the manual command is printed.
#
# VISION MODELS: no separate flag needed — before downloading, this script checks
# whether HF_REPO itself hosts a `*mmproj*.gguf` (vision repos do, e.g.
# ggml-org/Qwen2.5-VL-7B-Instruct-GGUF ships mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf).
# If so it fetches that too and writes LLM_GGUF_MMPROJ; if not (plain text model),
# LLM_GGUF_MMPROJ is cleared. `scripts/activate-llm.sh` does the same detection
# locally when switching among models already on disk.
#
# After fetching, restart the unit to pick it up:
#   sudo systemctl restart ragfarm-llama        # or: scripts/stack.sh restart
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=./lib-models.sh
source scripts/lib-models.sh

# Known-good tool-calling GGUF LLMs (hints for --list; --file selects the quant).
# Vision models need no separate entry field — mmproj is auto-detected at fetch time.
LLMS=(
	"Qwen/Qwen2.5-7B-Instruct-GGUF | qwen2.5-7b-instruct-q4_k_m*.gguf | ~4.7GB Q4_K_M. DEFAULT; fits the iGPU."
	"Qwen/Qwen2.5-14B-Instruct-GGUF | qwen2.5-14b-instruct-q4_k_m*.gguf | ~9GB Q4_K_M. Better tool discipline; more VRAM."
	"Qwen/Qwen2.5-32B-Instruct-GGUF | qwen2.5-32b-instruct-q4_k_m*.gguf | ~20GB Q4_K_M. Prod-class (ADR-0008 HW note)."
	"bartowski/Meta-Llama-3.1-8B-Instruct-GGUF | Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf | ~4.9GB. Llama alternative, tool-calling."
	"ggml-org/Qwen2.5-VL-7B-Instruct-GGUF | Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf | ~5GB. VISION — mmproj auto-fetched."
)

list_models() {
	printf 'Known-good tool-calling GGUF LLMs (pick by VRAM budget; --file selects the quant):\n\n'
	printf '  %-42s %-38s %s\n' "REPO (--repo)" "FILE (--file)" "notes"
	local m r f n
	for m in "${LLMS[@]}"; do
		IFS='|' read -r r f n <<< "$m"
		printf '  %-42s %-38s %s\n' "$(echo "$r" | xargs)" "$(echo "$f" | xargs)" "$(echo "$n" | xargs)"
	done
	printf '\nGGUF is downloaded directly (no conversion). Split shards auto-load from the first.\n'
}

HF_REPO="Qwen/Qwen2.5-7B-Instruct-GGUF"
FILE_GLOB="qwen2.5-7b-instruct-q4_k_m*.gguf"
FORCE=0
ENV_FILE=".env"

while [ $# -gt 0 ]; do
	case "$1" in
		--list)       list_models; exit 0 ;;
		--repo)       HF_REPO="$2"; shift 2 ;;
		--file)       FILE_GLOB="$2"; shift 2 ;;
		--force)      FORCE=1; shift ;;
		--env-file)   ENV_FILE="$2"; shift 2 ;;
		--no-restart) export NO_RESTART=1; shift ;;
		-h|--help)    sed -n '2,25p' "$0"; exit 0 ;;
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

if [ -z "$(mmproj_file)" ] || [ "$FORCE" = 1 ]; then
	mmproj_pick="$(hf_pick_mmproj "$HF_REPO" "$FILE_GLOB")"
	if [ -n "$mmproj_pick" ]; then
		info "vision model detected -> fetching mmproj ($mmproj_pick)"
		hf_snapshot_allow "$HF_REPO" "$DEST_DIR" "$mmproj_pick" >/dev/null
		[ -n "$(mmproj_file)" ] || die "mmproj download completed but no .gguf landed in $DEST_DIR"
	fi
fi

env_upsert "$ENV_FILE" LLM_GGUF_PATH "$REPO_ROOT/$existing"
ok "LLM_GGUF_PATH=$REPO_ROOT/$existing  ($ENV_FILE)"

# Reflects DEST_DIR's actual contents either way — clears LLM_GGUF_MMPROJ when this
# repo has none, so a stale --mmproj from a previous vision model can't leak in.
mmproj_now="$(mmproj_file)"
if [ -n "$mmproj_now" ]; then
	env_upsert "$ENV_FILE" LLM_GGUF_MMPROJ "$REPO_ROOT/$mmproj_now"
	ok "LLM_GGUF_MMPROJ=$REPO_ROOT/$mmproj_now  ($ENV_FILE)"
else
	env_upsert "$ENV_FILE" LLM_GGUF_MMPROJ ""
fi

# Auto-restart the LLM service to activate the new .env. Only fires against the
# real .env (not a scratch --env-file); --no-restart / NO_RESTART=1 skips it.
if [ "$ENV_FILE" = ".env" ]; then
    restart_units ragfarm-llama
else
    info "wrote $ENV_FILE (not the live .env); no service restart needed"
fi
