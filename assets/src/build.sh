#!/usr/bin/env bash
# assets/src/build.sh — render every diagram source to a PNG beside the others.
#
# The diagrams used to be source-less PNGs. When generation moved from llama.cpp
# to vLLM they became wrong and stayed wrong, because nobody could edit them.
# Now the .typ file is the source of truth and this regenerates the asset.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
command -v typst >/dev/null || { echo "typst not on PATH (try ~/.local/bin)"; exit 1; }
for f in *.typ; do
    out="../ragfarm_${f%.typ}.png"
    typst compile --format png --ppi 170 "$f" "$out"
    echo "  OK  $out ($(du -h "$out" | cut -f1))"
done
