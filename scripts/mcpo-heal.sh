#!/usr/bin/env bash
# ragfarm: mcpo boot-race healer (ADR-0003 gateway).
#
# On reboot the Docker daemon restarts containers (restart: unless-stopped) in an
# arbitrary order, BEFORE ragfarm-stack.service runs `docker compose up` — so
# compose depends_on/health conditions are not honored. mcpo then connects to MCP
# backends that aren't listening yet, trips the mcp streamable-http anyio
# cancel-scope bug, and comes up DEGRADED: host-control unmounted (reboot_host ->
# 404), empty aggregate spec, and a 100% CPU openapi-refetch spin.
#
# Fix: after the stack is up, wait until all three MCP backends accept a TCP
# connection, then restart mcpo ONCE so it mounts every tool cleanly. Best-effort;
# never fails the stack unit.
set -u

backends="infra-rag-retrieval:8104 infra-mcp-placement:8101 infra-mcp-host-control:8102"

for _ in $(seq 1 45); do            # up to ~90s
  ok=1
  for cp in $backends; do
    docker exec "${cp%%:*}" python -c \
      "import socket; socket.create_connection(('localhost', ${cp##*:}), 2)" \
      >/dev/null 2>&1 || ok=0
  done
  [ "$ok" = 1 ] && break
  sleep 2
done

docker restart infra-mcpo >/dev/null 2>&1 || true
exit 0
