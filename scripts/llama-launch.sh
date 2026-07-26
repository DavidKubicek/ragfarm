#!/usr/bin/env bash
# scripts/llama-launch.sh — ragfarm-llama.service's ExecStart target.
#
# Exists ONLY to add --mmproj conditionally. systemd's ExecStart= does its OWN
# $VAR/${VAR} substitution on the command line before any shell sees it, and does
# NOT understand bash conditional-expansion operators like ${VAR:+...} — inlining
# that in the unit risks systemd mangling it, unverifiable without a live restart.
# A plain wrapper script sidesteps that entirely: systemd just execs this file,
# this file does the one conditional in ordinary bash, which is testable offline
# (LLAMA_LAUNCH_DRY_RUN=1 below) without touching the running service.
#
# Reads LLM_GGUF_PATH (required) and LLM_GGUF_MMPROJ (optional; empty = text-only
# model, no --mmproj flag) from the environment — systemd's Environment=/
# EnvironmentFile= already populate these before exec'ing this script.
set -euo pipefail

: "${LLM_GGUF_PATH:?LLM_GGUF_PATH not set — check manifests/ragfarm-llama.service Environment= / .env}"

# alias = parent dir of the GGUF, stripped of the trailing "-gguf"/"_gguf" tag —
# e.g. .../qwen_qwen3-vl-8b-thinking-gguf/foo.gguf -> qwen_qwen3-vl-8b-thinking.
# Derived from $LLM_GGUF_PATH so switching models via .env / activate-llm.sh
# updates the OpenAI-compat model id automatically. Note: OWUI's setup_openwebui.py
# still pins BASE_MODEL_ID=qwen2.5-7b-instruct — re-run it (or update that constant)
# after swapping to keep the OWUI preset pointing at a real base.
ALIAS=$(basename "$(dirname "$LLM_GGUF_PATH")" | sed 's/[-_]gguf$//i')

ARGS=(
  -m "$LLM_GGUF_PATH"
)
if [ -n "${LLM_GGUF_MMPROJ:-}" ]; then
  ARGS+=(--mmproj "$LLM_GGUF_MMPROJ")
fi
ARGS+=(
  --host 127.0.0.1 --port 8080 -ngl 999 -c 32768 --context-shift --keep 3072 --jinja
  --seed 42 --temperature 0.6 -fa on -v --mlock --mmap --alias "$ALIAS"
)

if [ -n "${LLAMA_LAUNCH_DRY_RUN:-}" ]; then
  printf '%q ' /home/dave/llama.cpp/build/bin/llama-server "${ARGS[@]}"
  printf '\n'
  exit 0
fi

exec /home/dave/llama.cpp/build/bin/llama-server "${ARGS[@]}"
