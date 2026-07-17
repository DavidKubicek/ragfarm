#!/usr/bin/env bash
# =============================================================================
# ragfarm deploy.sh — idempotent, gate-asserted deployment of the DURABLE stack.
#
# WHAT THIS IS
#   The reproducible deploy, distilled from the ADRs (the "why") and the Claude
#   Code handoffs (the ordered "what"). Each phase is deterministic, idempotent
#   (safe to re-run), and ends in a machine-checkable GATE. This replaces
#   AI-in-the-loop for repeatable deploys; AI/human stay for authoring the lock,
#   capacity/GPU decisions, and debugging a gate that fails for a reason no
#   assertion anticipated.
#
#   Runs on the HOST as `dave` (never root). Uses `sudo` ONLY for the specific
#   systemd install/enable actions — never wraps the whole script.
#
# ADDING A PHASE  (the "easy to expand" contract)
#   1. write  phase_<name>() { ... ; gate_<name>; }
#   2. add    <name>  to the PHASES=(...) array, in run order
#   3. that's it — dispatch, --list, --from, and --phase pick it up for free.
#   Keep the body idempotent and end it with an assertion block. One phase = one
#   function = one array entry: the whole deploy is readable off the array.
#
# USAGE
#   scripts/deploy.sh                 # run all phases in order
#   scripts/deploy.sh <phase>         # run one phase
#   scripts/deploy.sh --from <phase>  # run from <phase> to the end
#   scripts/deploy.sh --list          # list phases in order
#   scripts/deploy.sh --profile cu12  # select dep/torch profile (default: cpu)
#   scripts/deploy.sh --recreate-corpus   # force a corpus rebuild (alias switch)
# =============================================================================
set -euo pipefail

# ---- location: everything is relative to the repo root ----------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---- config (single source of truth; override via env or flags) -------------
PROFILE="${PROFILE:-cpu}"                       # cpu | cu12  (see profile block)
VENV="${VENV:-$HOME/ragfarm/.venv}"
PYVER="${PYVER:-python3.12}"
MODEL_DIR="${MODEL_DIR:-$HOME/ragfarm/models/embeddings/bge-m3}"
MODEL_REPO="${MODEL_REPO:-BAAI/bge-m3}"

QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
EMBED_URL="${EMBED_URL:-http://127.0.0.1:8090}"
LLM_URL="${LLM_URL:-http://127.0.0.1:8080}"
MCPO_URL="${MCPO_URL:-http://127.0.0.1:8000}"
OWUI_URL="${OWUI_URL:-http://127.0.0.1:3000}"
ALIAS="${QDRANT_COLLECTION:-corpus}"            # retrieval targets this ALIAS (ADR-0006)
CORPUS_PATH="${CORPUS_PATH:-/data/corpus}"

COMPOSE="docker compose -f infra/compose.yaml"
MANIFESTS="manifests"
SYSTEMD_DIR="/etc/systemd/system"
# host-plane units (systemd) and the stack/watcher units, in the order they matter
HOST_UNITS=(ragfarm-llama.service ragfarm-embedder.service)
STACK_UNIT="ragfarm-stack.service"
WATCH_UNIT="ragfarm-ingester-watcher.service"

RECREATE_CORPUS=0

# profile → which committed lock + torch wheel index. This is the CPU/CUDA seam
# from ADR-0006: same package set, different torch build. Extend here for prod.
profile_config() {
	case "$PROFILE" in
		cpu)  LOCK="services/requirements.lock";       TORCH_INDEX="https://download.pytorch.org/whl/cpu"  ;;
		cu12) LOCK="services/requirements.cu12.lock";   TORCH_INDEX="https://download.pytorch.org/whl/cu124" ;;
		*)    die "unknown --profile '$PROFILE' (want: cpu | cu12)" ;;
	esac
}

# ---- tiny logging + assertion helpers ---------------------------------------
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()   { printf '%s  %s\n' "$(_c 90)$(date +%H:%M:%S)$(_c 0)" "$*"; }
info()  { printf '  %s\n' "$*"; }
ok()    { printf '  %sOK%s   %s\n' "$(_c 32)" "$(_c 0)" "$*"; }
warn()  { printf '  %sWARN%s %s\n' "$(_c 33)" "$(_c 0)" "$*" >&2; }
die()   { printf '\n%sFAIL%s %s\n' "$(_c 31)" "$(_c 0)" "$*" >&2; exit 1; }
phase() { printf '\n%s== %s ==%s\n' "$(_c 36)" "$*" "$(_c 0)"; }

have() { command -v "$1" >/dev/null 2>&1; }

# wait_http URL [timeout_s] — poll until 2xx/3xx or timeout
wait_http() {
	local url="$1" timeout="${2:-60}" start; start=$(date +%s)
	while :; do
		curl -sf -o /dev/null "$url" && return 0
		(( $(date +%s) - start >= timeout )) && return 1
		sleep 2
	done
}

# resolve the huggingface CLI across versions (hf | huggingface-cli)
hf_download() {
	local repo="$1" dst="$2"
	if [ -x "$VENV/bin/hf" ]; then "$VENV/bin/hf" download "$repo" --local-dir "$dst"
	else "$VENV/bin/huggingface-cli" download "$repo" --local-dir "$dst"; fi
}

# install one systemd unit file from manifests/ and enable --now (idempotent)
install_unit() {
	local unit="$1"
	[ -f "$MANIFESTS/$unit" ] || die "missing unit file: $MANIFESTS/$unit"
	sudo cp "$MANIFESTS/$unit" "$SYSTEMD_DIR/$unit"
}

# =============================================================================
# PHASES  (run order defined by the PHASES=(...) array near the bottom)
# =============================================================================

# ---- 0. preflight: host prerequisites + repo integrity ----------------------
phase_preflight() {
	phase "preflight"
	[ "${EUID:-$(id -u)}" -ne 0 ] || die "run as dave, not root (script sudo's only for systemd)"
	have "$PYVER" || die "$PYVER not on PATH"
	have docker    || die "docker not on PATH"
	have curl      || die "curl not on PATH"
	$COMPOSE version >/dev/null 2>&1 || die "'docker compose' plugin unavailable"
	profile_config
	[ -f "$LOCK" ] || die "missing $LOCK — author it once from a known-good venv (see step 01)"
	[ -d "$CORPUS_PATH" ] || warn "corpus path $CORPUS_PATH absent (corpus phase will need it)"
	# proxy loader is a repo convention; source if present so pulls/inter-svc calls behave
	[ -f scripts/proxy-env.sh ] && source scripts/proxy-env.sh || true
	ok "host tooling present; profile=$PROFILE; lock=$LOCK"
}

# ---- 1. venv: project virtualenv + locked deps + embedder model (step 01) ---
phase_venv() {
	phase "venv (step 01 — python-env)"
	if [ ! -x "$VENV/bin/python" ]; then
		info "creating venv at $VENV ($PYVER)"
		"$PYVER" -m venv "$VENV"
	else
		info "venv exists, reusing"
	fi
	"$VENV/bin/pip" install -q -U pip wheel
	info "installing locked deps ($LOCK) with torch index for profile=$PROFILE"
	"$VENV/bin/pip" install -q --extra-index-url "$TORCH_INDEX" -r "$LOCK"

	if ls "$MODEL_DIR"/*.safetensors >/dev/null 2>&1; then
		info "embedder model already present at $MODEL_DIR"
	else
		info "fetching $MODEL_REPO -> $MODEL_DIR (safetensors)"
		hf_download "$MODEL_REPO" "$MODEL_DIR"
	fi

	# GATE
	"$VENV/bin/python" - <<'PY' || die "venv import check failed"
import torch, FlagEmbedding, fastapi, uvicorn, pydantic
import qdrant_client, requests, openpyxl, docx, pdfplumber, langdetect, watchdog
print("  torch", torch.__version__, "cuda?", torch.cuda.is_available())
PY
	ls "$MODEL_DIR"/*.safetensors >/dev/null 2>&1 || die "no safetensors in $MODEL_DIR"
	ok "venv ready; deps import; model present as safetensors"
}

# ---- 2. host services: llama + embedder on systemd (steps 02/03) -------------
phase_host_services() {
	phase "host services (llama + embedder)"
	local u
	for u in "${HOST_UNITS[@]}"; do install_unit "$u"; done
	sudo systemctl daemon-reload
	for u in "${HOST_UNITS[@]}"; do sudo systemctl enable --now "$u"; done

	# GATE
	wait_http "$LLM_URL/v1/models" 120 || die "llama endpoint not answering ($LLM_URL)"
	# embedder must return NON-EMPTY sparse (same failure class as the TEI drop bug)
	curl -s "$EMBED_URL/embed" -H 'content-type: application/json' \
		-d '{"input":["prod-kvm-03 10.20.1.43 vlan203"],"kind":"passage"}' \
	| "$VENV/bin/python" - <<'PY' || die "embedder sparse gate failed"
import sys, json
d = json.load(sys.stdin); s = d["sparse"][0]
assert len(d["dense"][0]) == 1024 and len(s) > 0, "dense!=1024 or sparse empty"
print(f"  dense_dim {len(d['dense'][0])}  sparse_terms {len(s)}")
PY
	ok "llama + embedder active; embedder returns dense(1024)+sparse"
}

# ---- 3. stack: container layer via ragfarm-stack.service (step 04/07) --------
phase_stack() {
	phase "container stack (qdrant, rag, mcpo, open-webui, mcp-*)"
	# build first-party images (renamed build contexts per ADR-0005); mock MCPs by default
	$COMPOSE build
	install_unit "$STACK_UNIT"
	sudo systemctl daemon-reload
	sudo systemctl enable --now "$STACK_UNIT"

	# GATE
	wait_http "$QDRANT_URL/healthz" 90 || wait_http "$QDRANT_URL/readyz" 90 || die "qdrant not healthy"
	wait_http "$MCPO_URL/openapi.json" 120 || wait_http "$MCPO_URL/docs" 120 || die "mcpo endpoint down"
	wait_http "$OWUI_URL/health" 120 || die "open-webui not healthy"
	ok "stack up; qdrant + mcpo + open-webui answering"
}

# ---- 4. corpus: ADR-0006 bootstrap (alias + manifest) -----------------------
# Idempotent: bootstraps via --recreate ONLY if no alias/collection exists yet
# (or when --recreate-corpus is given). Ongoing sync is the watcher's job.
phase_corpus() {
	phase "corpus bootstrap (ADR-0006)"
	local exists; exists=$(curl -s "$QDRANT_URL/aliases" | grep -c "\"$ALIAS\"" || true)
	local phys;   phys=$(curl -s "$QDRANT_URL/collections/$ALIAS" | grep -c '"status"' || true)

	if [ "$RECREATE_CORPUS" = "1" ] || { [ "$exists" = "0" ] && [ "$phys" = "0" ]; }; then
		info "running --recreate (bootstrap/migrate: builds corpus_<ts>, switches alias, seeds manifest)"
		"$VENV/bin/python" services/ingester/ingester.py --recreate --corpus "$CORPUS_PATH"
	else
		info "collection/alias '$ALIAS' already present — skipping recreate (watcher keeps it synced)"
	fi

	# GATE — point count > 0 AND stored sparse is populated (config alone can't tell you)
	curl -s "$QDRANT_URL/collections/$ALIAS/points/scroll" -H 'content-type: application/json' \
		-d '{"limit":1,"with_vector":true,"with_payload":false}' \
	| "$VENV/bin/python" - <<'PY' || die "stored-sparse gate failed — hybrid retrieval would be broken"
import sys, json
pts = json.load(sys.stdin)["result"]["points"]
assert pts, "collection empty"
sp = pts[0]["vector"]["sparse"]
assert len(sp["indices"]) > 0, "stored sparse vector is EMPTY"
print(f"  stored sparse_indices {len(sp['indices'])}")
PY
	ok "corpus present; alias '$ALIAS' resolves; stored sparse populated"
}

# ---- 5. watcher: autonomous incremental sync (ADR-0006) ---------------------
phase_watcher() {
	phase "ingester watcher (ADR-0006)"
	install_unit "$WATCH_UNIT"
	sudo systemctl daemon-reload
	sudo systemctl enable --now "$WATCH_UNIT"
	# GATE
	systemctl is-active --quiet "$WATCH_UNIT" || die "$WATCH_UNIT not active"
	ok "$WATCH_UNIT active (debounced watch on $CORPUS_PATH)"
}

# ---- 6. verify: end-to-end gates -------------------------------------------
phase_verify() {
	phase "verify (end-to-end)"
	# exact-identifier through mcpo exercises the sparse branch + RRF fusion end to end.
	# NOTE: swap this hostname for a known exact identifier in YOUR corpus.
	local probe="${RAG_PROBE:-hsmbvxip001ts}"
	curl -s "$MCPO_URL/rag/search_corpus" -H 'content-type: application/json' \
		-d "{\"query\":\"$probe\",\"k\":3}" | grep -q results \
		|| die "RAG exact-identifier probe '$probe' returned no results (sparse fusion?)"
	# no stale ADR-0005 names lingering
	if docker ps --format '{{.Names}}' | grep -qE 'mcp-infra-placement|mcp-fs-agent'; then
		die "stale container names present (mcp-infra-placement / mcp-fs-agent)"
	fi
	ok "RAG exact-identifier resolves; container names clean"
	log "deploy verified."
}

# =============================================================================
# RUN ORDER — the deploy is readable straight off this array
# =============================================================================
PHASES=(preflight venv host_services stack corpus watcher verify)

# ---- dispatch ---------------------------------------------------------------
list_phases() { printf 'phases (in order):\n'; local p; for p in "${PHASES[@]}"; do printf '  %s\n' "$p"; done; }

run_one()  { local p="$1"; declare -F "phase_$p" >/dev/null || die "no such phase: $p"; "phase_$p"; }
run_all()  { local p; for p in "${PHASES[@]}"; do run_one "$p"; done; }
run_from() {
	local start="$1" seen=0 p
	for p in "${PHASES[@]}"; do
		[ "$p" = "$start" ] && seen=1
		[ "$seen" = 1 ] && run_one "$p"
	done
	[ "$seen" = 1 ] || die "no such phase: $start"
}

main() {
	local mode=all target=""
	while [ $# -gt 0 ]; do
		case "$1" in
			--list)            list_phases; exit 0 ;;
			--from)            mode=from; target="${2:-}"; shift ;;
			--profile)         PROFILE="${2:-}"; shift ;;
			--recreate-corpus) RECREATE_CORPUS=1 ;;
			-h|--help)         sed -n '2,40p' "$0"; exit 0 ;;
			--*)               die "unknown option: $1" ;;
			*)                 mode=one; target="$1" ;;
		esac
		shift
	done
	case "$mode" in
		all)  run_all ;;
		one)  profile_config; run_one "$target" ;;   # single phase still needs profile vars
		from) profile_config; run_from "$target" ;;
	esac
}

main "$@"

#vi:set sw=8:ts=8:noexpandtab
