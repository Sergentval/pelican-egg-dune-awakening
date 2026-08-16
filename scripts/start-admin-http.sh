#!/bin/bash
# Launch scripts/admin-http.py in the background. Same launch_bg +
# PID-file pattern as the other services so console.sh's shutdown
# handler picks it up.
#
# Two modes (controlled by DUNE_ADMIN_UI_ENABLED):
#
#   DUNE_ADMIN_UI_ENABLED=0 (default)
#     Internal mode. Bind 127.0.0.1:8089. Optional Bearer auth via
#     DUNE_ADMIN_HTTP_AUTH. Used by tooling that runs inside the
#     container.
#
#   DUNE_ADMIN_UI_ENABLED=1
#     Public mode. Bind $DUNE_ADMIN_UI_BIND:$DUNE_ADMIN_UI_PORT
#     (default 0.0.0.0:8090). Serves the React SPA from $WEB_ROOT
#     and requires session-token auth (issued by /api/login against
#     $DUNE_ADMIN_UI_PASSWORD). Tokens are signed with
#     $DUNE_ADMIN_UI_SESSION_SECRET (32-hex, set by prestart.sh).

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
export SOURCE="admin-http"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

PY="${DUNE_PYTHON3:-python3}"
SCRIPT="$SCRIPTS/admin-http.py"

if [ ! -x "$SCRIPT" ] && [ ! -r "$SCRIPT" ]; then
    die "admin-http.py missing at $SCRIPT"
fi

if [ "${DUNE_ADMIN_UI_ENABLED:-0}" = "1" ]; then
    # Public-facing UI mode.
    BIND="${DUNE_ADMIN_UI_BIND:-0.0.0.0}"
    PORT="${DUNE_ADMIN_UI_PORT:-8090}"
    if [ -z "${DUNE_ADMIN_UI_PASSWORD:-}" ]; then
        die "DUNE_ADMIN_UI_ENABLED=1 but no DUNE_ADMIN_UI_PASSWORD — prestart.sh should have generated one. Restart the container or supply a password in the panel."
    fi
    log "Starting admin-http (UI mode) on ${BIND}:${PORT}..."
    WEB_ROOT="${DUNE_ADMIN_UI_WEB_ROOT:-$BASE/data/web/dist}"
    [ -d "$WEB_ROOT" ] || log "  warning: WEB_ROOT $WEB_ROOT missing — UI will return 404 until you build the SPA into it"
    launch_bg admin-http "$LOGS/admin-http.log" -- \
      env DUNE_BASE_DIR="$BASE" \
          DUNE_ADMIN_HTTP_ADDR="$BIND" \
          DUNE_ADMIN_HTTP_PORT="$PORT" \
          DUNE_ADMIN_UI_ENABLED=1 \
          DUNE_ADMIN_UI_PASSWORD="$DUNE_ADMIN_UI_PASSWORD" \
          DUNE_ADMIN_UI_SESSION_SECRET="$DUNE_ADMIN_UI_SESSION_SECRET" \
          DUNE_ADMIN_UI_DOMAIN="${DUNE_ADMIN_UI_DOMAIN:-}" \
          DUNE_ADMIN_UI_TRUSTED_PROXIES="${DUNE_ADMIN_UI_TRUSTED_PROXIES:-}" \
          DUNE_ADMIN_UI_TLS="${DUNE_ADMIN_UI_TLS:-auto}" \
          DUNE_ADMIN_UI_WEB_ROOT="$WEB_ROOT" \
          DUNE_ADMIN_TOKEN="${DUNE_ADMIN_TOKEN:-}" \
          DUNE_ADMIN_NODE="${DUNE_ADMIN_NODE:-rabbit-game@localhost}" \
          STEAM_API_KEY="${STEAM_API_KEY:-}" \
      "$PY" "$SCRIPT"
    WAIT_HOST="127.0.0.1"
    [ "$BIND" = "0.0.0.0" ] || WAIT_HOST="$BIND"
    if wait_for_port "$WAIT_HOST" "$PORT" 5; then
        log "admin-http UI ready: success (pid $(read_pid admin-http))"
        # Advertise the URL that actually works, and only that one. Whether
        # the session cookies carry Secure decides which one that is: a
        # Secure cookie is discarded by the browser over plain HTTP, so the
        # login form submits, succeeds server-side, and drops the operator
        # back on the login screen with no error. The panel console
        # linkifies whatever we print, so printing the wrong one walks
        # people straight into that.
        if [ -n "${DUNE_ADMIN_UI_DOMAIN:-}" ] && [ "${DUNE_ADMIN_UI_TLS:-auto}" != "off" ]; then
            log "  URL: https://${DUNE_ADMIN_UI_DOMAIN}/"
            log "  Reverse proxy should forward that hostname to http://${DUNE_EXTERNAL_IP:-$BIND}:${PORT}/"
            log "  NOTE: log in via the https:// URL above — session cookies are"
            log "        marked Secure, so a browser will drop them on"
            log "        http://${DUNE_EXTERNAL_IP:-$BIND}:${PORT}/ and the login will not stick."
            log "        Serving this panel WITHOUT TLS? Set DUNE_ADMIN_UI_TLS=off."
        elif [ -n "${DUNE_ADMIN_UI_DOMAIN:-}" ]; then
            # Domain set, operator declared there is no TLS in front: the
            # cookies are not Secure, so plain HTTP on the domain works.
            log "  URL: http://${DUNE_ADMIN_UI_DOMAIN}:${PORT}/"
            log "  NOTE: DUNE_ADMIN_UI_TLS=off — cookies are NOT marked Secure and the"
            log "        session travels in the clear. Fine on a trusted LAN; put TLS in"
            log "        front of anything reachable from the internet."
        else
            log "  URL: http://${DUNE_EXTERNAL_IP:-$BIND}:${PORT}/"
        fi
    else
        tail -30 "$LOGS/admin-http.log" >&2 || true
        die "admin-http UI failed to bind within 5s"
    fi
else
    # Internal-only mode (loopback). Pre-existing behavior.
    BIND="${DUNE_ADMIN_HTTP_ADDR:-127.0.0.1}"
    PORT="${DUNE_ADMIN_HTTP_PORT:-8089}"
    log "Starting admin-http (internal mode) on ${BIND}:${PORT}..."
    launch_bg admin-http "$LOGS/admin-http.log" -- \
      env DUNE_BASE_DIR="$BASE" \
          DUNE_ADMIN_HTTP_ADDR="$BIND" \
          DUNE_ADMIN_HTTP_PORT="$PORT" \
          DUNE_ADMIN_HTTP_AUTH="${DUNE_ADMIN_HTTP_AUTH:-}" \
          DUNE_ADMIN_TOKEN="${DUNE_ADMIN_TOKEN:-}" \
          DUNE_ADMIN_NODE="${DUNE_ADMIN_NODE:-rabbit-game@localhost}" \
      "$PY" "$SCRIPT"
    if wait_for_port 127.0.0.1 "$PORT" 5; then
        log "admin-http ready: success (pid $(read_pid admin-http))"
    else
        tail -30 "$LOGS/admin-http.log" >&2 || true
        die "admin-http failed to bind within 5s"
    fi
fi
