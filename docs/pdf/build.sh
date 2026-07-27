#!/usr/bin/env bash
# docs/pdf/build.sh — concatenate every canonical repo doc into one PDF via pandoc+typst.
#
# Chapter order (matches 00-introduction.md's roadmap):
#   1  Introduction               (docs/pdf/00-introduction.md — the only doc unique to this bundle)
#   2  Project README             (README.md — architectural summary + 9 chat-example screenshots)
#   3  Deployment guide           (docs/deployment.md)
#   4  Prompt library             (docs/prompts.md — board demo reference)
#   5  Ingestion pipeline         (docs/ingestion-pipeline.md)
#   6  Component READMEs          (infra/embedder + infra/llama)
#   7  Configuration reference    (.env.example)
#   8  Model records              (models/llm + embeddings + reranker)
#   9  Instrumentation & tracing  (tests/tracing/*.md — seven files)
#   10 Build progress             (PROGRESS.md — appears last; is the current op-status ledger)
#
# Requires pandoc + typst on PATH (both installed to ~/.local/bin/ by
# scripts/fetch-drawio-viewer.sh's cousin — or manually per install notes).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

command -v pandoc >/dev/null || { echo "pandoc not on PATH"; exit 1; }
command -v typst  >/dev/null || { echo "typst not on PATH";  exit 1; }

OUT="${1:-docs/pdf/ragfarm.pdf}"

# Chapter list — order matters, each becomes its own top-level heading in the PDF
CHAPTERS=(
  "1  Introduction|docs/pdf/00-introduction.md"
  "2  Project README|README.md"
  "3  Qwen3-VL vision demonstrations|docs/pdf/01-vision-demonstrations.md"
  "4  Hardware requirements (dev + prod ladder)|docs/pdf/02-hardware-requirements.md"
  "5  Deployment guide|docs/deployment.md"
  "6  Prompt library|docs/prompts.md"
  "7  Ingestion pipeline|docs/ingestion-pipeline.md"
  "8a Embedder README|infra/embedder/README.md"
  "8b llama.cpp README|infra/llama/README.md"
  "9  Configuration reference (.env.example)|.env.example"
  "10a Model record — LLM|models/llm/MODEL.md"
  "10b Model record — embedder|models/embeddings/MODEL.md"
  "10c Model record — reranker|models/reranker/MODEL.md"
  "11a Instrumentation — README|tests/tracing/README_INSTRUMENTATION.md"
  "11b Instrumentation — full guide|tests/tracing/INSTRUMENTATION_GUIDE.md"
  "11c Tracer integration guide|tests/tracing/TRACER_INTEGRATION_GUIDE.md"
  "11d RAG pipeline setup notes|tests/tracing/RAG_PIPELINE_SETUP.md"
  "11e Quick reference — instrumentation|tests/tracing/QUICK_REFERENCE.md"
  "11f Quick reference — context diagnosis|tests/tracing/QUICK_REFERENCE_CONTEXT_DIAGNOSIS.md"
  "11g Context blowup diagnosis guide|tests/tracing/CONTEXT_DIAGNOSIS_GUIDE.md"
  "12 Build progress (PROGRESS.md)|PROGRESS.md"
)

# Concatenate — each chapter gets a top-level heading of the form
# "Chapter N — <title>" prepended so the TOC nests correctly. Chapter sources
# already have their own `# heading` at the top; we don't renumber those,
# pandoc handles heading levels fine as they are.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
MASTER="$TMP/master.md"

# YAML front matter — pandoc/typst pick up title, author, date, styling
cat > "$MASTER" <<EOF
---
title: "ragfarm — on-prem RAG + infra-control agent"
subtitle: "A living reference. Generated $(date -u +'%Y-%m-%d %H:%M UTC').\\newline\\newline Source of truth is the repository; this PDF is a snapshot."
author: "David Kubicek (kubicek@gmail.com)"
date: "$(date -u +'%B %-d, %Y')"
lang: en
toc: true
toc-depth: 2
numbersections: false
geometry:
  - margin=2cm
  - a4paper
mainfont: "DejaVu Sans"
sansfont: "DejaVu Sans"
monofont: "DejaVu Sans Mono"
---

EOF

for entry in "${CHAPTERS[@]}"; do
  label="${entry%%|*}"
  path="${entry##*|}"
  if [ ! -f "$path" ]; then
    echo "  MISS  $path" >&2
    continue
  fi
  echo "  +  Chapter $label  ($path, $(wc -c <"$path") bytes)"
  # Chapter divider. \newpage is LaTeX and gets passed through as literal
  # text by pandoc's GFM reader (visible as raw "\newpage" in the PDF).
  # A raw typst block with #pagebreak() is the correct portable idiom; requires
  # --from=gfm+raw_attribute on the pandoc call below.
  cat >> "$MASTER" <<EOF

\`\`\`{=typst}
#pagebreak()
\`\`\`

# Chapter $label

EOF
  # Rewrite relative image paths so they resolve from the master's temp cwd.
  # Simplest correct fix: convert any ./assets/... or docs/... reference to an
  # absolute path rooted at $REPO_ROOT.
  #
  # Non-markdown chapters (.env.example — an .env config file) are wrapped in a
  # bash code fence, because their `# ==============` section-divider lines would
  # otherwise be parsed by GFM as H1 headings and pollute the TOC.
  python3 - "$REPO_ROOT" "$path" >> "$MASTER" <<'PY'
import re, sys, pathlib
repo, path = sys.argv[1], sys.argv[2]
base = pathlib.Path(path).parent
text = pathlib.Path(path).read_text()
name = pathlib.Path(path).name

# Non-markdown files: dump inside a code fence to protect the TOC + syntax.
# .env.example is bash-flavored (VAR=value + shell-style comments).
if name == ".env.example":
    sys.stdout.write("Reproducible source-of-truth for every environment variable in the ragfarm stack. Copy to `.env` on the host and set the ones you need — everything else stays commented at safe defaults.\n\n")
    sys.stdout.write("```bash\n")
    sys.stdout.write(text)
    sys.stdout.write("\n```\n")
    sys.exit(0)

def fix(m):
    alt, url = m.group(1), m.group(2)
    if url.startswith(("http://", "https://", "/")):
        return m.group(0)
    resolved = (pathlib.Path(repo) / base / url).resolve()
    return f"![{alt}]({resolved})"
text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", fix, text)
# Also demote the file's own top-level # heading to ## so our injected
# "# Chapter N" stays the sole H1 for the chapter.
text = re.sub(r"^# ", "## ", text, count=1, flags=re.MULTILINE)
sys.stdout.write(text)
sys.stdout.write("\n")
PY
done

echo "  ==> master $(wc -l < "$MASTER") lines, $(wc -c < "$MASTER") bytes"
echo "  ==> pandoc -> typst -> pdf"
pandoc "$MASTER" \
  --from=gfm+yaml_metadata_block+raw_attribute \
  --to=pdf \
  --pdf-engine=typst \
  --toc --toc-depth=2 \
  --syntax-highlighting=tango \
  --resource-path="$REPO_ROOT:$REPO_ROOT/docs:$REPO_ROOT/assets" \
  -o "$OUT"

echo "  OK  $OUT ($(du -h "$OUT" | cut -f1))"
