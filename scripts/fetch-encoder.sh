#!/usr/bin/env bash
# scripts/fetch-encoder.sh — fetch/swap the embedder + reranker MODEL PAIR
# (ADR-0002 embedder, ADR-0008 reranker).
#
# Downloads the dense+sparse embedder into models/embeddings/<slug>/ (FlagEmbedding
# loads it in place) and its cross-encoder reranker — converted to an f16 GGUF via
# llama.cpp's converter — into models/reranker/<slug>/. Writes EMBED_MODEL_PATH and
# RERANK_GGUF_PATH into .env, which ragfarm-embedder / ragfarm-reranker read on next
# start — no unit edits, no code changes.
#
# WHY ONE SCRIPT FOR BOTH: the embedder and reranker must be a COMPATIBLE pair (same
# family / language coverage, e.g. bge-m3 + bge-reranker-v2-m3) — swap them together,
# not independently. This is the ONE place download+convert logic for the pair lives;
# scripts/deploy.sh calls it rather than duplicating it.
#
# Fetches the LATEST revision and the fastest available weight format (safetensors >
# pytorch_model.bin). Idempotent: a target already on disk is a no-op unless --force.
#
# USAGE
#   scripts/fetch-encoder.sh                 # default pair: bge-m3 + bge-reranker-v2-m3
#   scripts/fetch-encoder.sh --list          # show known-good matching pairs, then exit
#   scripts/fetch-encoder.sh --embed-repo <repo> --rerank-repo <repo>
#   scripts/fetch-encoder.sh --force         # re-fetch / re-convert even if present
#
# After fetching, restart both units:
#   sudo systemctl restart ragfarm-embedder ragfarm-reranker   # or scripts/stack.sh restart
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=./lib-models.sh
source scripts/lib-models.sh

# Known-good, family-matched embedder+reranker pairs (hints for --list; not exhaustive).
PAIRS=(
	"BAAI/bge-m3 | BAAI/bge-reranker-v2-m3 | multilingual (100+ langs incl. Czech), 1024-d dense + SPARSE. DEFAULT."
	"BAAI/bge-large-en-v1.5 | BAAI/bge-reranker-v2-m3 | English, 1024-d dense only (no sparse)."
	"intfloat/multilingual-e5-large | BAAI/bge-reranker-v2-m3 | multilingual dense; pair with the m3 reranker."
	"Snowflake/snowflake-arctic-embed-l-v2.0 | BAAI/bge-reranker-v2-m3 | multilingual dense, strong retrieval."
)

list_pairs() {
	printf 'Known-good embedder + reranker pairs — keep the two in the same family / language coverage:\n\n'
	printf '  %-40s %-26s %s\n' "EMBEDDER (--embed-repo)" "RERANKER (--rerank-repo)" "notes"
	local p e r n
	for p in "${PAIRS[@]}"; do
		IFS='|' read -r e r n <<< "$p"
		printf '  %-40s %-26s %s\n' "$(echo "$e" | xargs)" "$(echo "$r" | xargs)" "$(echo "$n" | xargs)"
	done
	printf '\nNote: our hybrid search_corpus relies on bge-m3 dense+SPARSE — a dense-only embedder\n'
	printf 'weakens exact identifier (hostname/IP) matching. The reranker is downloaded as HF\n'
	printf 'weights and converted to an f16 GGUF here (served by ragfarm-reranker on the iGPU).\n'
}

EMBED_REPO="BAAI/bge-m3"
RERANK_REPO="BAAI/bge-reranker-v2-m3"
EMBED_REVISION=""   # empty = latest
RERANK_REVISION=""  # empty = latest
FORCE=0
ENV_FILE=".env"
LLAMA_DIR="${LLAMA_DIR:-$HOME/llama.cpp}"

while [ $# -gt 0 ]; do
	case "$1" in
		--list)            list_pairs; exit 0 ;;
		--embed-repo)      EMBED_REPO="$2"; shift 2 ;;
		--embed-revision)  EMBED_REVISION="$2"; shift 2 ;;
		--rerank-repo)     RERANK_REPO="$2"; shift 2 ;;
		--rerank-revision) RERANK_REVISION="$2"; shift 2 ;;
		--force)           FORCE=1; shift ;;
		--env-file)        ENV_FILE="$2"; shift 2 ;;
		--llama-dir)       LLAMA_DIR="$2"; shift 2 ;;
		-h|--help)         sed -n '2,28p' "$0"; exit 0 ;;
		*) die "unknown arg: $1 (see --help / --list)" ;;
	esac
done

# ---- embedder: weights loaded in place by FlagEmbedding ----------------------
EMBED_SLUG="$(slugify "$EMBED_REPO")"
EMBED_DIR="models/embeddings/$EMBED_SLUG"
# true if EITHER a safetensors OR a pytorch_model.bin weight is present. (NOT `ls a b`
# — that returns non-zero when either glob is empty, which mis-reports "absent".)
have_weights() { compgen -G "$EMBED_DIR/*.safetensors" >/dev/null || compgen -G "$EMBED_DIR/pytorch_model*.bin" >/dev/null; }
if have_weights && [ "$FORCE" != 1 ]; then
	ok "embedder already present: $EMBED_DIR"
	EMBED_FETCHED=0
else
	info "fetching $EMBED_REPO${EMBED_REVISION:+@$EMBED_REVISION} -> $EMBED_DIR (latest, fastest weight format)"
	mkdir -p "$EMBED_DIR"
	hf_snapshot "$EMBED_REPO" "$EMBED_DIR" "$EMBED_REVISION" >/dev/null
	have_weights || die "no weight file (safetensors/bin) landed in $EMBED_DIR"
	EMBED_FETCHED=1
fi
env_upsert "$ENV_FILE" EMBED_MODEL_PATH "$REPO_ROOT/$EMBED_DIR"

# ---- reranker: HF weights -> f16 GGUF via llama.cpp's converter ---------------
RERANK_SLUG="$(slugify "$RERANK_REPO")"
RERANK_DIR="models/reranker/$RERANK_SLUG"
RERANK_GGUF="$RERANK_DIR/${RERANK_SLUG}-f16.gguf"
if [ -f "$RERANK_GGUF" ] && [ "$FORCE" != 1 ]; then
	ok "reranker GGUF already present: $RERANK_GGUF"
else
	[ -f "$LLAMA_DIR/convert_hf_to_gguf.py" ] || die "llama.cpp converter missing at $LLAMA_DIR (build llama.cpp first — infra/llama/README.md)"
	SRC="$(mktemp -d)"; trap 'rm -rf "$SRC"' EXIT
	info "fetching $RERANK_REPO${RERANK_REVISION:+@$RERANK_REVISION} -> $SRC (conversion input)"
	hf_snapshot "$RERANK_REPO" "$SRC" "$RERANK_REVISION" >/dev/null
	mkdir -p "$RERANK_DIR"
	info "converting -> $RERANK_GGUF (f16)"
	"$VENV_PY" "$LLAMA_DIR/convert_hf_to_gguf.py" "$SRC" --outfile "$RERANK_GGUF" --outtype f16 \
		|| die "reranker GGUF conversion failed"
fi
env_upsert "$ENV_FILE" RERANK_GGUF_PATH "$REPO_ROOT/$RERANK_GGUF"

ok "EMBED_MODEL_PATH=$REPO_ROOT/$EMBED_DIR"
ok "RERANK_GGUF_PATH=$REPO_ROOT/$RERANK_GGUF   ($ENV_FILE)"
if [ "${EMBED_FETCHED:-0}" = 1 ]; then
	warn "the EMBEDDER changed — the corpus MUST be re-embedded or hybrid retrieval breaks"
	info "  1) restart:   sudo systemctl restart ragfarm-embedder ragfarm-reranker   (or scripts/stack.sh restart)"
	info "  2) re-ingest: .venv/bin/python services/ingester/ingester.py --recreate --corpus \"\${CORPUS_PATH:-/data/corpus}\""
else
	info "restart to apply (reranker-only change): sudo systemctl restart ragfarm-reranker   (or scripts/stack.sh restart)"
fi
