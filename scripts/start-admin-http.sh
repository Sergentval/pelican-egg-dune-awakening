#!/bin/bash
# Launch scripts/admin-http.py in the background. Same launch_bg +
# PID-file pattern as the other services so console.sh's shutdown
# handler picks it up.
#
# Bind: 127.0.0.1:8089 by default (loopback only). Use
# DUNE_ADMIN_HTTP_ADDR and DUNE_ADMIN_HTTP_PORT to override.
#
# Auth: optional Bearer token via DUNE_ADMIN_HTTP_AUTH. When unset,
# the listener accepts any caller — fine when bound to loopback.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
export SOURCE="admin-http"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

PY="${DUNE_PYTHON3:-python3}"
SCRIPT="$SCRIPTS/admin-http.py"

if [ ! -x "$SCRIPT" ] && [ ! -r "$SCRIPT" ]; then
    die "admin-http.py missing at $SCRIPT"
fi

log "Starting admin-http on ${DUNE_ADMIN_HTTP_ADDR:-127.0.0.1}:${DUNE_ADMIN_HTTP_PORT:-8089}..."

launch_bg admin-http "$LOGS/admin-http.log" -- \
  env DUNE_BASE_DIR="$BASE" \
      DUNE_ADMIN_HTTP_ADDR="${DUNE_ADMIN_HTTP_ADDR:-127.0.0.1}" \
      DUNE_ADMIN_HTTP_PORT="${DUNE_ADMIN_HTTP_PORT:-8089}" \
      DUNE_ADMIN_HTTP_AUTH="${DUNE_ADMIN_HTTP_AUTH:-}" \
      DUNE_ADMIN_TOKEN="${DUNE_ADMIN_TOKEN:-}" \
      DUNE_ADMIN_NODE="${DUNE_ADMIN_NODE:-rabbit-game@localhost}" \
  "$PY" "$SCRIPT"

if wait_for_port 127.0.0.1 "${DUNE_ADMIN_HTTP_PORT:-8089}" 5; then
    log "admin-http ready: success (pid $(read_pid admin-http))"
else
    tail -30 "$LOGS/admin-http.log" >&2 || true
    die "admin-http failed to bind within 5s"
fi
