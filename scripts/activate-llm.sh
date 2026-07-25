#!/usr/bin/env bash
# scripts/activate-llm.sh — pick among LLM GGUFs already on disk and activate one.
#
# fetch-llm.sh downloads a model and points .env at it (one model per invocation).
# This script instead LISTS everything already fetched into models/llm/*/, lets you
# choose one (by number, interactively, or non-interactively via --dir/--path), and
# writes LLM_GGUF_PATH + LLM_GGUF_MMPROJ into .env — no re-download. It runs the same
# mmproj detection fetch-llm.sh does (any `*mmproj*.gguf` in the chosen model's
# directory), so flipping between a vision model and a text-only one always leaves
# LLM_GGUF_MMPROJ correct (set, or cleared — never stale).
#
# USAGE
#   scripts/activate-llm.sh              # list models on disk, prompt for a choice
#   scripts/activate-llm.sh --dir qwen2.5-32b-instruct-gguf   # activate by dir name
#   scripts/activate-llm.sh --path /abs/path/to/model.gguf    # activate by exact file
#   scripts/activate-llm.sh --list        # list only, no prompt, exit 0
#
# This script does NOT restart the service (same convention as fetch-llm.sh) — it
# only writes .env. Restart to apply:
#   sudo systemctl restart ragfarm-llama        # or: scripts/stack.sh restart
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=./lib-models.sh
source scripts/lib-models.sh

LLM_ROOT="models/llm"
ENV_FILE=".env"
ARG_DIR=""
ARG_PATH=""
LIST_ONLY=0

while [ $# -gt 0 ]; do
	case "$1" in
		--dir)      ARG_DIR="$2"; shift 2 ;;
		--path)     ARG_PATH="$2"; shift 2 ;;
		--list)     LIST_ONLY=1; shift ;;
		--env-file) ENV_FILE="$2"; shift 2 ;;
		-h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
		*) die "unknown arg: $1 (see --help)" ;;
	esac
done

# main_gguf DIR — first non-mmproj *.gguf in DIR (LC_ALL=C so multi-shard names
# like ...-00001-of-00002.gguf sort the first shard first, same as fetch-llm.sh).
main_gguf()   { find "$1" -maxdepth 1 -iname '*.gguf' -not -iname '*mmproj*' 2>/dev/null | LC_ALL=C sort | head -1; }
mmproj_gguf() { find "$1" -maxdepth 1 -iname '*mmproj*.gguf' 2>/dev/null | LC_ALL=C sort | head -1; }

# Discover every models/llm/<slug>/ that actually has a usable main GGUF (skips a
# dir left behind by an interrupted download with no complete file — see the
# 2026-07-25 incident where a dangling .env pointer broke the next restart).
mapfile -t DIRS < <(find "$LLM_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | LC_ALL=C sort)
CHOICES=()
for d in "${DIRS[@]}"; do
	g="$(main_gguf "$d")"
	[ -n "$g" ] && CHOICES+=("$d")
done
[ "${#CHOICES[@]}" -gt 0 ] || die "no complete GGUF found under $LLM_ROOT/*/ — fetch one first: scripts/fetch-llm.sh"

print_list() {
	printf 'LLM GGUFs on disk (%s):\n\n' "$LLM_ROOT"
	local i=1 d g m size mtag
	for d in "${CHOICES[@]}"; do
		g="$(main_gguf "$d")"
		m="$(mmproj_gguf "$d")"
		size="$(du -sh "$d" 2>/dev/null | cut -f1)"
		mtag=""
		[ -n "$m" ] && mtag="  [vision: $(basename "$m")]"
		printf '  %2d) %-42s %-8s %s%s\n' "$i" "$(basename "$d")" "($size)" "$(basename "$g")" "$mtag"
		i=$((i + 1))
	done
}

if [ "$LIST_ONLY" = 1 ]; then
	print_list
	exit 0
fi

# Resolve the chosen directory: --path (exact file) > --dir (by name) > interactive.
if [ -n "$ARG_PATH" ]; then
	[ -f "$ARG_PATH" ] || die "--path not found: $ARG_PATH"
	CHOSEN_DIR="$(dirname "$ARG_PATH")"
	CHOSEN_MAIN="$ARG_PATH"
elif [ -n "$ARG_DIR" ]; then
	CHOSEN_DIR="$LLM_ROOT/$ARG_DIR"
	[ -d "$CHOSEN_DIR" ] || CHOSEN_DIR="$ARG_DIR"   # allow a full/relative path too
	[ -d "$CHOSEN_DIR" ] || die "--dir not found: $ARG_DIR"
	CHOSEN_MAIN="$(main_gguf "$CHOSEN_DIR")"
	[ -n "$CHOSEN_MAIN" ] || die "no complete (non-mmproj) .gguf in $CHOSEN_DIR"
else
	print_list
	printf '\nActivate which # ? '
	read -r REPLY
	idx=$((REPLY - 1))
	[ "$idx" -ge 0 ] && [ "$idx" -lt "${#CHOICES[@]}" ] || die "invalid choice: $REPLY"
	CHOSEN_DIR="${CHOICES[$idx]}"
	CHOSEN_MAIN="$(main_gguf "$CHOSEN_DIR")"
fi

CHOSEN_MMPROJ="$(mmproj_gguf "$CHOSEN_DIR")"

env_upsert "$ENV_FILE" LLM_GGUF_PATH "$REPO_ROOT/$CHOSEN_MAIN"
ok "LLM_GGUF_PATH=$REPO_ROOT/$CHOSEN_MAIN  ($ENV_FILE)"

# Same clear-if-absent rule as fetch-llm.sh: switching to a text-only model must
# not leave a previous vision model's --mmproj pointing at the wrong projector.
if [ -n "$CHOSEN_MMPROJ" ]; then
	env_upsert "$ENV_FILE" LLM_GGUF_MMPROJ "$REPO_ROOT/$CHOSEN_MMPROJ"
	ok "LLM_GGUF_MMPROJ=$REPO_ROOT/$CHOSEN_MMPROJ  ($ENV_FILE)"
else
	env_upsert "$ENV_FILE" LLM_GGUF_MMPROJ ""
fi

info "restart to apply: sudo systemctl restart ragfarm-llama   (or scripts/stack.sh restart)"
