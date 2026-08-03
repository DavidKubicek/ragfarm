#!/usr/bin/env bash
# scripts/stack.sh — ONE command to run the whole ragfarm stack.
#
# For operators: bring the entire system up or down in the correct order and confirm
# it's healthy, without memorising the per-service systemd sequence (that full manual
# sequence is still documented in docs/deployment.md → Autostart & lifecycle).
#
#   scripts/stack.sh start     # host services -> container stack -> health check
#   scripts/stack.sh stop      # container stack -> host services (reverse order)
#   scripts/stack.sh restart   # stop, then start
#   scripts/stack.sh status    # systemd unit + container status at a glance
#   scripts/stack.sh health    # probe every endpoint; exit non-zero if any is down
#
# Runs on the HOST as `dave`; uses sudo only for systemctl. Idempotent + safe to re-run.
set -uo pipefail

# Host units in START order (stop reverses this). The container stack is a separate
# unit (ragfarm-stack) that docker-compose-ups the containers + heals mcpo.
# NOTE (ADR-0013): ragfarm-llama is the retired llama.cpp GENERATION unit; step 02
# replaces it with ragfarm-vllm. Rename here AND in scripts/deploy.sh together.
HOST_UNITS=(ragfarm-llama ragfarm-reranker ragfarm-embedder ragfarm-ingester-watcher)
STACK_UNIT=ragfarm-stack
COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/compose.yaml"

c()    { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
ok()   { printf '  %sOK%s   %s\n' "$(c 32)" "$(c 0)" "$*"; }
bad()  { printf '  %sDOWN%s %s\n' "$(c 31)" "$(c 0)" "$*"; }
info() { printf '\n%s==%s %s\n' "$(c 36)" "$(c 0)" "$*"; }

probe() { # probe LABEL URL
	if curl -sf -o /dev/null --max-time 5 "$2"; then ok "$1  $2"; return 0; else bad "$1  $2"; return 1; fi
}

health() {
	info "health check"
	local rc=0
	probe "llama LLM   " http://127.0.0.1:8080/v1/models        || rc=1
	probe "reranker    " http://127.0.0.1:8081/health           || rc=1
	probe "embedder    " http://127.0.0.1:8090/health           || rc=1
	probe "qdrant      " http://127.0.0.1:6333/readyz           || rc=1
	probe "mcpo (tools)" http://127.0.0.1:8000/rag/openapi.json || rc=1
	probe "open-webui  " http://127.0.0.1:3000/health           || rc=1
	[ $rc -eq 0 ] && ok "all services healthy" || bad "one or more services are DOWN"
	return $rc
}

start() {
	info "starting host services: ${HOST_UNITS[*]}"
	sudo systemctl start "${HOST_UNITS[@]}"
	info "starting container stack: $STACK_UNIT"
	sudo systemctl start "$STACK_UNIT"
	info "waiting for endpoints to come up"
	for _ in $(seq 1 40); do health >/dev/null 2>&1 && break; sleep 3; done
	health
}

stop() {
	info "stopping container stack: $STACK_UNIT"
	sudo systemctl stop "$STACK_UNIT"
	local rev=(); local i
	for (( i=${#HOST_UNITS[@]}-1; i>=0; i-- )); do rev+=("${HOST_UNITS[i]}"); done
	info "stopping host services: ${rev[*]}"
	sudo systemctl stop "${rev[@]}"
	ok "stopped"
}

status() {
	info "systemd units"
	local u
	for u in "${HOST_UNITS[@]}" "$STACK_UNIT"; do
		printf '  %-30s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || echo unknown)"
	done
	info "containers"
	docker compose -f "$COMPOSE_FILE" ps 2>/dev/null
}

case "${1:-}" in
	start)   start ;;
	stop)    stop ;;
	restart) stop; start ;;
	status)  status ;;
	health)  health ;;
	*) echo "usage: $0 {start|stop|restart|status|health}" >&2; exit 2 ;;
esac
