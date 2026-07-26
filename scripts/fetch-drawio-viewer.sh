#!/usr/bin/env bash
# scripts/fetch-drawio-viewer.sh — populate infra/drawio-viewer/ with jgraph/drawio's
# webapp mirror (js, styles, shapes, stencils, math4, templates, images, ...) so the
# ragfarm-vision preset's draw.io RULE 5 template renders in-chat air-gapped.
#
# WHY: the vision system prompt tells the model to output a fenced <html> block
# that loads viewer-static.min.js from http://127.0.0.1/js/… and its runtime
# STYLE_PATH / SHAPES_PATH / STENCIL_PATH also point at 127.0.0.1. All of those
# need to be served locally — a stub with only viewer-static.min.js renders
# blank canvases the moment a shape lookup happens. This script fetches the
# whole webapp (~151 MB) from upstream and drops it into infra/drawio-viewer/
# where the drawio-viewer nginx container mounts it read-only.
#
# The two files we keep in git (index.html landing + drawio-editor.html — the
# renamed upstream editor) are PRESERVED across a re-fetch: they are written
# after the copy, or restored from git if absent.
#
# USAGE
#   scripts/fetch-drawio-viewer.sh              # fetch if missing; no-op if present
#   scripts/fetch-drawio-viewer.sh --force      # re-fetch even if present
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DEST="infra/drawio-viewer"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# Presence heuristic: the runtime STYLE_PATH points at /styles/*, so if styles/default.xml
# exists we already have a real webapp on disk. Bare fresh clones only have our two custom
# HTML files + a .gitkeep or similar — that's the "needs fetch" state.
if [ "$FORCE" = 0 ] && [ -d "$DEST/styles" ] && [ -f "$DEST/js/viewer-static.min.js" ]; then
    echo "  OK   drawio webapp present at $DEST ($(du -sh "$DEST" | cut -f1))"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "  fetching jgraph/drawio (shallow, sparse: src/main/webapp only) -> $TMP"
mkdir -p "$TMP/src"
cd "$TMP/src"
git init -q
git remote add origin https://github.com/jgraph/drawio.git
git config core.sparseCheckout true
echo "src/main/webapp/" > .git/info/sparse-checkout
git fetch --depth 1 origin dev -q
git checkout -q FETCH_HEAD

WEBAPP="$TMP/src/src/main/webapp"
[ -d "$WEBAPP" ] || { echo "FAIL: expected $WEBAPP not present"; exit 1; }

cd "$REPO_ROOT"
# Preserve our two tracked files across the copy: index.html (ragfarm landing)
# and drawio-editor.html (the renamed upstream editor entrypoint). Restore
# from git after the copy so a --force never silently loses them.
echo "  installing $(du -sh "$WEBAPP" | cut -f1) into $DEST"
# copy everything except the upstream index.html (would clobber our landing)
find "$WEBAPP" -maxdepth 1 -mindepth 1 -not -name index.html \
    -exec cp -a {} "$DEST/" \;
# Upstream's index.html becomes our drawio-editor.html (already committed name)
cp -a "$WEBAPP/index.html" "$DEST/drawio-editor.html"

# Guarantee our landing survives — restore if the copy tree wiped it.
if [ ! -s "$DEST/index.html" ]; then
    git checkout -- "$DEST/index.html" 2>/dev/null || {
        echo "WARN: index.html missing and not in git — landing page will 404 until re-authored"
    }
fi

echo "  OK   drawio webapp installed ($(du -sh "$DEST" | cut -f1))"
echo "  restart the nginx to serve fresh files (idempotent):"
echo "    docker compose -f infra/compose.yaml restart drawio-viewer"
