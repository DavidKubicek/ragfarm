#!/usr/bin/env bash
# scripts/stack.sh — ONE command for the whole ragfarm stack, and the SINGLE
# SOURCE OF TRUTH for what "the whole stack" actually is.
#
#   scripts/stack.sh start     # host services -> container stack -> health
#   scripts/stack.sh stop      # containers -> host services (reverse order)
#   scripts/stack.sh restart   # stop, then start
#   scripts/stack.sh status    # the table: every service, endpoint, state
#   scripts/stack.sh health    # same probes, quiet-ish, non-zero exit if degraded
#   scripts/stack.sh list      # print the service inventory and exit
#
# Runs on the HOST as `dave`; sudo only for systemctl. Idempotent, safe to re-run.
#
# WHY THE TABLE BELOW IS THE POINT
# The old version probed six endpoints while the architecture had thirteen
# services, so "all services healthy" was true of less than half the system. Two
# failures found on 2026-08-12 that it could not have caught:
#   - mcpo answering 200 on /openapi.json with ZERO tools mounted. The gateway is
#     up, the model has no tools, and every grounded answer silently stops being
#     grounded. HTTP status cannot see this; only counting the mounted paths can.
#   - ragfarm-vllm@1 down while slot 1 was bound in the registry — invisible,
#     because slot 1 was never probed at all.
# So a service here declares BOTH a liveness probe and, where a 200 can lie, a
# depth check. Adding a service means adding one row, not editing three places.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/compose.yaml"
REGISTRY="$ROOT/models/llm/active.json"

# ---------------------------------------------------------------------------
# THE INVENTORY.  name | kind | unit-or-container | probe URL | ok-codes | note
# ---------------------------------------------------------------------------
# kind:  unit       host systemd service
#        container  docker compose service
#        optional   defined but not expected to run (does not fail health)
#        noport     judged by systemd alone; nothing to curl
#
# ok-codes is a space-separated allowlist. It is not always 200, and that is
# deliberate: the MCP servers speak streamable-http, so a plain GET on /mcp is
# answered 406 Not Acceptable *by the application*. A 406 therefore proves the
# service is alive and speaking MCP, where 000 means nothing is listening. Using
# 200-or-bust here would report every MCP backend as permanently down.
SERVICES=(
  "reranker|unit|ragfarm-reranker|http://127.0.0.1:8081/health|200|cross-encoder, ADR-0008"
  "embedder|unit|ragfarm-embedder|http://127.0.0.1:8090/health|200|BGE-M3 dense+sparse"
  "ingester-watcher|noport|ragfarm-ingester-watcher||-|inotify corpus watcher"
  "qdrant|container|infra-qdrant|http://127.0.0.1:6333/readyz|200|vector store"
  "rag-retrieval|container|infra-rag-retrieval|http://127.0.0.1:8104/mcp|406 405|search_corpus"
  "mcp-placement|container|infra-mcp-placement|http://127.0.0.1:8101/mcp|406 405|OpenNebula lookups"
  "mcp-host-control|container|infra-mcp-host-control|http://127.0.0.1:8102/mcp|406 405|SAFETY-GATED"
  "mcp-fs|optional|infra-mcp-fs|http://127.0.0.1:8103/mcp|406 405|stub, deliberately unwired"
  "mcpo|container|infra-mcpo|http://127.0.0.1:8000/openapi.json|200|OpenAPI gateway"
  "open-webui|container|infra-open-webui|http://127.0.0.1:3000/health|200|the only LAN-exposed service"
  "drawio-viewer|container|infra-drawio-viewer|http://127.0.0.1:80/|200|local draw.io mirror"
)

# vLLM slots are NOT hard-coded: which exist is decided by active.json's `active`
# array, so a slot bound by activate_llm.py shows up here automatically and a
# cleared one stops being reported as missing. See activate_llm(1).
slot_rows() {
    [ -r "$REGISTRY" ] || return 0
    python3 - "$REGISTRY" <<'PY'
import json, sys
reg = json.load(open(sys.argv[1]))
for slot, idx in enumerate(reg.get("active", [])):
    if isinstance(idx, int):
        name = reg["downloaded"][idx]["model"] if idx < len(reg["downloaded"]) else "?"
        port = 8080 + 2 * slot
        print(f"vllm-slot{slot}|unit|ragfarm-vllm@{slot}|"
              f"http://127.0.0.1:{port}/v1/models|200|{name}")
PY
}
mapfile -t SLOT_ROWS < <(slot_rows)
ALL=("${SLOT_ROWS[@]}" "${SERVICES[@]}")

# Host units, in START order. Stop reverses it. Slots first: everything else is
# cheap to start, and the GPU allocation is the part that can fail.
host_units() {
    local r; for r in "${SLOT_ROWS[@]}"; do echo "${r#*|unit|}" | cut -d'|' -f1; done
    echo ragfarm-reranker; echo ragfarm-embedder; echo ragfarm-ingester-watcher
}
STACK_UNIT=ragfarm-stack

c()    { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
info() { printf '\n%s==%s %s\n' "$(c 36)" "$(c 0)" "$*"; }
note() { printf '     %s\n' "$*"; }

# A templated instance that has never been started is not "cat"-able, so
# systemctl cat gives a false negative on ragfarm-vllm@N. LoadState is the
# honest question: does systemd know how to build this unit?
unit_exists() {
    [ "$(systemctl show -p LoadState --value "$1.service" 2>/dev/null)" = loaded ]
}
unit_state() { systemctl show -p ActiveState --value "$1.service" 2>/dev/null || echo unknown; }

# --- depth checks: where a 200 is not proof of a working service -------------
# Each prints a failure reason on stdout and returns 1, or returns 0 silently.
depth_mcpo() {
    local n r
    for r in rag placement; do
        n=$(curl -s --max-time 8 "http://127.0.0.1:8000/$r/openapi.json" \
            | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("paths",{})))' 2>/dev/null)
        if [ "${n:-0}" -eq 0 ]; then
            # The boot-race: mcpo mounted before its MCP backends were listening.
            # Route answers 200, zero tools behind it, model silently ungrounded.
            echo "route /$r mounted 0 tools — run scripts/mcpo-heal.sh"
            return 1
        fi
    done
    return 0
}
depth_drawio_viewer() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
           "http://127.0.0.1:80/js/viewer-static.min.js")
    if [ "$code" != "200" ]; then
        # 153 MB gitignored mirror; a fresh clone has only the landing page and
        # every in-chat diagram renders as an empty white box with no error.
        echo "webapp mirror absent ($code) — run scripts/fetch-drawio-viewer.sh"
        return 1
    fi
    return 0
}

# --- one row -> STATE string; sets ROW_BAD=1 when it counts against health ----
probe_row() {
    local name kind ref url codes note_
    IFS='|' read -r name kind ref url codes note_ <<<"$1"
    ROW_STATE=""; ROW_BAD=0; ROW_EP=""

    if [ "$kind" = noport ]; then
        ROW_EP="(no port, systemd only)"
        if ! unit_exists "$ref"; then ROW_STATE="[ABSENT] unit $ref not installed"; ROW_BAD=1
        elif [ "$(unit_state "$ref")" = active ]; then ROW_STATE="[OK]"
        else ROW_STATE="[NOT_OK: inactive] ($ref)"; ROW_BAD=1; fi
        return
    fi

    ROW_EP="${url#http://}"
    local code; code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)
    if [[ " $codes " == *" $code "* ]]; then
        local why
        if why=$(depth_check "$name"); then ROW_STATE="[OK]"
        else ROW_STATE="[DEGRADED] ($why)"; ROW_BAD=1; fi
        return
    fi

    # Down. Say WHY in the operator's terms, not curl's.
    local why="no listener"
    [ "$code" != "000" ] && why="unexpected status, wanted: $codes"
    case "$kind" in
        unit)      if unit_exists "$ref"; then why="$why; unit $(unit_state "$ref")"
                   else why="$why; unit $ref not installed"; fi ;;
        container) if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$ref"; then
                       why="$why; container $ref not running"
                   fi ;;
        optional)  ROW_STATE="[OFF] (not deployed — $note_)"; ROW_BAD=0; return ;;
    esac
    ROW_STATE="[NOT_OK: $code] ($why)"; ROW_BAD=1
}

depth_check() {
    case "$1" in
        mcpo)          depth_mcpo ;;
        drawio-viewer) depth_drawio_viewer ;;
        *)             return 0 ;;
    esac
}

table() {
    local rows=0 bad=0 r name kind
    printf '%-18s %-38s %s\n' "SERVICE" "ENDPOINT" "STATE"
    printf '%-18s %-38s %s\n' "------------------" "--------------------------------------" "-----"
    for r in "${ALL[@]}"; do
        IFS='|' read -r name kind _ _ _ _ <<<"$r"
        probe_row "$r"
        local colour=32; [ "$ROW_BAD" = 1 ] && colour=31
        [[ "$ROW_STATE" == "[OFF]"* ]] && colour=33
        printf '%-18s %-38s %s%s%s\n' "$name" "$ROW_EP" "$(c $colour)" "$ROW_STATE" "$(c 0)"
        rows=$((rows+1)); bad=$((bad+ROW_BAD))
    done
    printf '\n%d services checked, %d not OK\n' "$rows" "$bad"
    return $(( bad > 0 ))
}

health() { info "health"; table; }

status() {
    info "services"
    table; local rc=$?
    info "systemd"
    local u
    while read -r u; do
        [ -z "$u" ] && continue
        printf '  %-28s %-10s %s\n' "$u" "$(unit_state "$u")" \
            "$(systemctl is-enabled "$u.service" 2>&1 | head -1)"
    done < <(host_units; echo "$STACK_UNIT")
    note "slot 1 is intentionally NOT enabled at boot: systemd would start both"
    note "slots in parallel and vLLM cannot profile GPU memory concurrently."
    note "See activate_llm(1) -> AUTOSTART AT BOOT."
    return $rc
}

start() {
    local u units=()
    while read -r u; do [ -n "$u" ] && units+=("$u"); done < <(host_units)
    info "starting host services"
    # ONE AT A TIME, waiting for each. Two vLLM instances profiling GPU memory
    # concurrently kill each other — see activate_llm(1) -> WHY SLOTS START
    # SEQUENTIALLY. systemctl start with several units does NOT serialise them.
    for u in "${units[@]}"; do
        if ! unit_exists "$u"; then printf '  skip  %s (not installed)\n' "$u"; continue; fi
        printf '  start %s\n' "$u"
        sudo systemctl start "$u"
        case "$u" in ragfarm-vllm@*)
            local port=$(( 8080 + 2 * ${u##*@} )) i
            for i in $(seq 1 120); do
                curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$port/v1/models" && break
                [ "$(systemctl is-active "$u.service")" = failed ] && { printf '        FAILED\n'; break; }
                sleep 5
            done ;;
        esac
    done
    if unit_exists "$STACK_UNIT"; then
        info "starting container stack: $STACK_UNIT"; sudo systemctl start "$STACK_UNIT"
    else
        info "starting containers (no $STACK_UNIT unit; using compose directly)"
        # shellcheck disable=SC1091
        source "$ROOT/scripts/proxy-env.sh" >/dev/null 2>&1 || true
        docker compose -f "$COMPOSE_FILE" up -d
        # mcpo races its MCP backends on a cold start and comes up with zero
        # tools mounted. Heal unconditionally; it is a no-op when already clean.
        "$ROOT/scripts/mcpo-heal.sh" || true
    fi
    info "waiting for endpoints"
    local i; for i in $(seq 1 40); do table >/dev/null 2>&1 && break; sleep 3; done
    health
}

stop() {
    if unit_exists "$STACK_UNIT"; then
        info "stopping container stack: $STACK_UNIT"; sudo systemctl stop "$STACK_UNIT"
    else
        info "stopping containers"; docker compose -f "$COMPOSE_FILE" stop
    fi
    local u rev=()
    while read -r u; do [ -n "$u" ] && rev=("$u" "${rev[@]}"); done < <(host_units)
    info "stopping host services (reverse order)"
    for u in "${rev[@]}"; do
        unit_exists "$u" && { printf '  stop  %s\n' "$u"; sudo systemctl stop "$u"; }
    done
    printf '  done\n'
}

list() {
    local r name kind ref url codes note_
    printf '%-18s %-10s %-26s %-34s %s\n' "SERVICE" "KIND" "UNIT/CONTAINER" "PROBE" "NOTE"
    for r in "${ALL[@]}"; do
        IFS='|' read -r name kind ref url codes note_ <<<"$r"
        printf '%-18s %-10s %-26s %-34s %s\n' "$name" "$kind" "$ref" "${url:--}" "$note_"
    done
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)  status ;;
    health)  health ;;
    list)    list ;;
    *) echo "usage: $0 {start|stop|restart|status|health|list}" >&2; exit 2 ;;
esac
