#!/bin/bash
# Originally © 2026 CubeCoders Limited (MIT). Modified for Pelican Wings.
# See ATTRIBUTION.md.
#
# lib.sh - Shared helpers for the Dune Awakening Pelican egg scripts.
#
# Sourced by every other script. Establishes layout variables, logging,
# PID-file management, and wait-for-port helpers. Under Pelican Wings,
# env vars are inherited naturally from the parent shell (no equivalent
# of AMP's per-PreStartStage env-file plumbing needed).
#
# Layout under $BASE (passed as $1 to each script, or DUNE_BASE_DIR env):
#   $BASE/scripts/        — these scripts
#   $BASE/depot/          — SteamCMD download
#   $BASE/extracted/      — OCI rootfs trees (postgres/, mq/, director/, ...)
#   $BASE/state/          — persistent identity + data (pg/data, ue5-saved, certs)
#   $BASE/runtime/        — regenerated each start (pids, conf, sockets)
#   $BASE/logs/           — per-service log files

set -eu             # pipefail deliberately omitted: many pipelines `| head` or `| tail`
                    # which SIGPIPE the producer, which under pipefail aborts the script.

# --------------------------------------------------------------------------
# BASE layout
# --------------------------------------------------------------------------
BASE="${1:-${DUNE_BASE_DIR:-}}"
[ -n "$BASE" ] || { echo "lib.sh: BASE dir missing (arg1 or DUNE_BASE_DIR)"; exit 1; }
BASE="${BASE%/}"
export DUNE_BASE_DIR="$BASE"

SCRIPTS="$BASE/scripts"
DEPOT="$BASE/depot"
EXTRACTED="$BASE/extracted"
STATE="$BASE/server/state"
RUNTIME="$BASE/runtime"
LOGS="$BASE/logs"
TEMPLATES="$SCRIPTS/templates"

mkdir -p "$STATE" "$RUNTIME/pids" "$RUNTIME/postgresql" "$LOGS" 2>/dev/null || true

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# SOURCE is the structured-log [source] tag for this script.
# Each script sets SOURCE before sourcing lib.sh; fall back to 'dune'.
: "${SOURCE:=dune}"
log()  { printf '[%s] [INFO] %s\n'  "$SOURCE" "$*"; }
warn() { printf '[%s] [WARN] %s\n'  "$SOURCE" "$*" >&2; }
die()  { printf '[%s] [ERROR] %s\n' "$SOURCE" "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Per-service rootfs paths + LD_LIBRARY_PATH builder
# --------------------------------------------------------------------------
rootfs()  { echo "$EXTRACTED/$1"; }
ldlib()   { local r; r=$(rootfs "$1"); echo "$r/lib:$r/usr/lib:$r/usr/local/lib"; }
ldlib_mq() { echo "$(ldlib mq):$EXTRACTED/mq/opt/openssl/lib"; }
icu_dir() {
  # find icudt74l.dat (74.1 in some images, 74.2 in others)
  local r; r=$(rootfs "$1")
  local d; d=$(find "$r/usr/share/icu" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1)
  echo "${d:-$r/usr/share/icu/74.2}"
}

# --------------------------------------------------------------------------
# PID-file helpers (used by start scripts AND console.sh on shutdown)
# --------------------------------------------------------------------------
pid_file() { echo "$RUNTIME/pids/$1.pid"; }

write_pid() {
  local name=$1 pid=$2
  echo "$pid" > "$(pid_file "$name")"
}

read_pid() {
  local f; f=$(pid_file "$1")
  [ -f "$f" ] && cat "$f" || echo ""
}

is_running() {
  local pid; pid=$(read_pid "$1")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# --------------------------------------------------------------------------
# Launch helper — runs a command in the background, captures stdout+stderr
# to a service log, records PID for later shutdown.  Returns immediately.
# Usage: launch_bg <name> <log_file> -- <cmd...>
# --------------------------------------------------------------------------
launch_bg() {
  local name=$1 logf=$2; shift 2
  [ "$1" = "--" ] && shift
  log "starting $name"
  # setsid so we can kill the whole group on shutdown
  setsid "$@" >>"$logf" 2>&1 < /dev/null &
  local pid=$!
  write_pid "$name" "$pid"
  disown
}

# --------------------------------------------------------------------------
# wait_for_port host port timeout_sec
# --------------------------------------------------------------------------
wait_for_port() {
  local host=$1 port=$2 timeout=${3:-60}
  local i=0
  while (( i < timeout )); do
    if (echo > /dev/tcp/$host/$port) 2>/dev/null; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

# --------------------------------------------------------------------------
# wait_for_udp_port — UE5 binds UDP; we can't simply connect.  Instead poll
# /proc/net/udp6 + /proc/net/udp for the bound port owned by our PID.
# --------------------------------------------------------------------------
wait_for_udp_bind() {
  local name=$1 port=$2 timeout=${3:-120}
  local pid; pid=$(read_pid "$name")
  local hex_port; hex_port=$(printf '%04X' "$port")
  local i=0
  while (( i < timeout )); do
    if kill -0 "$pid" 2>/dev/null && grep -qE ":$hex_port " /proc/net/udp /proc/net/udp6 2>/dev/null; then
      return 0
    fi
    sleep 1; i=$((i+1))
  done
  return 1
}

# --------------------------------------------------------------------------
# psql wrapper — runs as the current user, sets ICU_DATA + LD_LIBRARY_PATH
# --------------------------------------------------------------------------
PSQL_BIN="$EXTRACTED/postgres/usr/local/bin/psql"

pg_env() {
  echo "LD_LIBRARY_PATH=$(ldlib postgres) ICU_DATA=$(icu_dir postgres)"
}

psql_super() {
  env -i HOME=/tmp LC_ALL=C $(pg_env) \
    "$PSQL_BIN" -h "${PGHOST:-127.0.0.1}" -p "${PGPORT:-15432}" -U postgres -d "${1:-postgres}" -tA "${@:2}"
}

# --------------------------------------------------------------------------
# Resolve the AMP-provided IPs into well-named variables.  All scripts
# should reference these instead of DUNE_LAN_IP / DUNE_PUBLIC_IP directly.
#
# DUNE_BIND_IP   — what services bind to (AMP's $ApplicationIPBinding;
#                  127.0.0.1 for private services, container IP for public)
# DUNE_EXTERNAL_IP — what we advertise to FLS (AMP's $ExternalIP or user override)
# --------------------------------------------------------------------------
: "${DUNE_BIND_IP:=127.0.0.1}"
: "${DUNE_EXTERNAL_IP:=$DUNE_BIND_IP}"
export DUNE_BIND_IP DUNE_EXTERNAL_IP

# Default port values — overridden by AMP-supplied env vars in production
: "${DUNE_PG_PORT:=15432}"
: "${DUNE_MQ_ADMIN_PORT:=5672}"
: "${DUNE_MQ_ADMIN_MGMT_PORT:=15672}"
: "${DUNE_MQ_GAME_PORT:=5673}"
: "${DUNE_MQ_GAME_MGMT_PORT:=15673}"
: "${DUNE_MQ_GAME_PROM_PORT:=15693}"
: "${DUNE_TEXT_ROUTER_PORT:=5059}"
: "${DUNE_DIRECTOR_PORT:=11717}"
: "${DUNE_GATEWAY_PORT:=8080}"

export DUNE_PG_PORT DUNE_MQ_ADMIN_PORT DUNE_MQ_ADMIN_MGMT_PORT \
       DUNE_MQ_GAME_PORT DUNE_MQ_GAME_MGMT_PORT DUNE_MQ_GAME_PROM_PORT \
       DUNE_TEXT_ROUTER_PORT DUNE_DIRECTOR_PORT DUNE_GATEWAY_PORT

# --------------------------------------------------------------------------
# Derive the Funcom Live Services environment from the chosen build:
# the PTC Steam app id maps to Funcom's beta FLS environment, production
# uses retail. Driven by the same panel variable that selects the Steam
# app id, so FLS env can never desync from the installed build.
# --------------------------------------------------------------------------
case "${DUNE_RELEASE_VERSION:-}" in
  3104830) DUNE_FLS_ENV=beta ;;
  *)       DUNE_FLS_ENV=retail ;;
esac
export DUNE_FLS_ENV

# --------------------------------------------------------------------------
# Persisted state — values prestart.sh generated on first install (WorldName,
# RMQ secret) live in $STATE so subsequent container restarts pick them up.
# Read them here so every stage that sources lib.sh sees them as env vars.
# --------------------------------------------------------------------------
if [ -f "$STATE/world-name" ]; then
  DUNE_WORLD_NAME=$(cat "$STATE/world-name")
  export DUNE_WORLD_NAME
fi
if [ -f "$STATE/rmq-secret" ]; then
  DUNE_RMQ_SEC=$(cat "$STATE/rmq-secret")
  export DUNE_RMQ_SEC
fi
if [ -f "$STATE/svc-cmd-token" ]; then
  # ServerCommandsAuthToken — the seabass server-command handler in each
  # UE5 instance validates inbound admin RMQ messages against this. Feed
  # it to start-ue5.sh which passes it via -ini:engine: overrides; feed
  # the same value to scripts/admin-publish.sh for outbound publishes.
  DUNE_SVC_CMD_TOKEN=$(cat "$STATE/svc-cmd-token")
  export DUNE_SVC_CMD_TOKEN
fi

# Admin web UI — if the operator left DUNE_ADMIN_UI_PASSWORD blank but
# enabled the UI, prestart.sh generates one and persists it here so the
# value survives container restarts.
if [ -z "${DUNE_ADMIN_UI_PASSWORD:-}" ] && [ -f "$STATE/admin-ui-password" ]; then
  DUNE_ADMIN_UI_PASSWORD=$(cat "$STATE/admin-ui-password")
  export DUNE_ADMIN_UI_PASSWORD
fi
# Used by admin-http.py to sign issued session tokens. Generated once
# per install, persisted across restarts. NOT user-configurable.
if [ -f "$STATE/admin-ui-session-secret" ]; then
  DUNE_ADMIN_UI_SESSION_SECRET=$(cat "$STATE/admin-ui-session-secret")
  export DUNE_ADMIN_UI_SESSION_SECRET
fi

# Mark lib loaded so children can sanity-check
export DUNE_LIB_LOADED=1
