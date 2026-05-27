#!/bin/bash
# Originally © 2026 CubeCoders Limited (MIT). Modified for Pelican Wings.
# See ATTRIBUTION.md.
#
# console.sh BASE_DIR
#
# Foreground process Pelican Wings monitors. Multiplexes all service logs
# into one stdout stream with [service] prefixes, and on SIGTERM stops
# each background service in reverse dependency order with phased grace
# periods (UE5 25s, gateway/director 8s, mq/pg 15s).
#
# Container lifecycle:
#   - pelican-entrypoint.sh runs every boot stage (start-pg, start-mq-*,
#     etc.) which leave services running in the background with PID files
#     in $RUNTIME/pids/
#   - pelican-entrypoint.sh then execs this script as the container's
#     main process; Wings attaches the container's stdout to its log pipe
#   - On stop, Wings sends SIGTERM
#   - We trap, send SIGTERM to each service PID per phase in reverse
#     order, wait the phase's grace window, SIGKILL stragglers, exit

BASE="${1:-${DUNE_BASE_DIR:-}}"
export SOURCE="console"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

# Dependency order — services started in this order; stopped in reverse
SERVICES=(postgres mq-admin mq-game text-router mock-k8s director gateway ue5-Survival_1 ue5-Overmap ue5-DeepDesert_1)

# --------------------------------------------------------------------------
# Shutdown handler
# --------------------------------------------------------------------------
shutdown_all() {
  echo
  log "Shutdown signal received — phased graceful stop."

  # Phased ordering: UE5 saves character/world state to Postgres on exit,
  # so DB+RMQ must outlive UE5.  Then gateway/director/text-router (the
  # FLS-bridging .NET trio), then the brokers, then Postgres last.
  local -a PHASES=(
    "ue5-Survival_1 ue5-Overmap ue5-DeepDesert_1"
    "gateway"
    "director"
    "mock-k8s"
    "text-router"
    "mq-game mq-admin"
    "postgres"
  )
  declare -A GRACE=(
    ["ue5-Survival_1 ue5-Overmap ue5-DeepDesert_1"]=25
    ["gateway"]=8
    ["director"]=8
    ["text-router"]=8
    ["mq-game mq-admin"]=15
    ["mock-k8s"]=10
    ["postgres"]=15
  )

  for phase in "${PHASES[@]}"; do
    local grace=${GRACE[$phase]:-10}
    for svc in $phase; do
      local pid; pid=$(read_pid "$svc")
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "  SIGTERM $svc (pid $pid, up to ${grace}s grace)"
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done
    # Wait this phase's grace period for all phase members to exit
    for _ in $(seq 1 "$grace"); do
      local still=0
      for svc in $phase; do
        local pid; pid=$(read_pid "$svc")
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && still=$((still+1))
      done
      [ "$still" -eq 0 ] && break
      sleep 1
    done
    # SIGKILL anything in this phase that didn't shut down in grace
    for svc in $phase; do
      local pid; pid=$(read_pid "$svc")
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        warn "  SIGKILL $svc (pid $pid) — exceeded ${grace}s grace"
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
  done

  # Stop the log-aggregator process group so no orphan tail/gawk linger
  if [ -n "${TAIL_PID:-}" ]; then
    pkill -TERM -P "$TAIL_PID" 2>/dev/null || true
    kill -TERM "$TAIL_PID" 2>/dev/null || true
    sleep 1
    pkill -KILL -P "$TAIL_PID" 2>/dev/null || true
    kill -KILL "$TAIL_PID" 2>/dev/null || true
  fi
  log "Shutdown complete: success"
  exec true   # replace ourselves so AMP sees the process tree truly gone
}
trap shutdown_all SIGTERM SIGINT SIGHUP

# --------------------------------------------------------------------------
# Verify everyone started OK (PID files exist and processes alive)
# --------------------------------------------------------------------------
log "Verifying service status..."
all_ok=1
for svc in "${SERVICES[@]}"; do
  pid=$(read_pid "$svc")
  if [ -z "$pid" ]; then
    warn "  $svc: no PID file — service did not start"
    all_ok=0
  elif ! kill -0 "$pid" 2>/dev/null; then
    warn "  $svc: pid $pid not running"
    all_ok=0
  else
    log "  $svc: pid $pid OK"
  fi
done
[ "$all_ok" = 1 ] || warn "One or more services failed to start — see logs above"

# --------------------------------------------------------------------------
# Print a single ready marker that AMP's AppReadyRegex can hook
# --------------------------------------------------------------------------
log "Dune Awakening server ready: success"
log "  World name:   ${DUNE_WORLD_NAME:-?}"
log "  Title:        ${DUNE_WORLD_TITLE:-?}"
log "  External IP:  ${DUNE_EXTERNAL_IP:-?}"
log "  Region:       ${DUNE_REGION:-?}"

# --------------------------------------------------------------------------
# Tail every service log, prefixed with [service]
# --------------------------------------------------------------------------
LOG_FILES=()
for svc in "${SERVICES[@]}"; do
  f="$LOGS/$svc.log"
  touch "$f"
  LOG_FILES+=("$f")
done

# We want one combined stream prefixed with [service].  Use one background
# tail per file with awk-prefix, all piped into our stdout.
(
  for svc in "${SERVICES[@]}"; do
    f="$LOGS/$svc.log"
    tail -n 0 -F "$f" 2>/dev/null | gawk -v svc="$svc" '
      function classify(line,    m) {
        # Our own services emit "[LEVEL] message" — promote directly.
        if (match(line, /\[(DEBUG|INFO|WARN|ERROR)\]/, m)) return m[1]
        # Director emits e.g. "[16:34:56 27 DBG IGWO] message".
        if (match(line, /\[[0-9:]+ +[0-9]+ +(DBG|INF|WRN|ERR) /, m)) {
          if (m[1] == "DBG") return "DEBUG"
          if (m[1] == "INF") return "INFO"
          if (m[1] == "WRN") return "WARN"
          if (m[1] == "ERR") return "ERROR"
        }
        # Fallback: keyword scan (legacy behaviour).
        l = tolower(line)
        if (l ~ /\<(error|fatal|failed|exception|critical|crash)\>/) return "ERROR"
        if (l ~ /\<(warn|warning)\>/) return "WARN"
        return "INFO"
      }
      {
        type = classify($0)
        printf "[%s] [%s] %s\n", svc, type, $0
        fflush()
      }' &
  done
  wait
) &
TAIL_PID=$!

# --------------------------------------------------------------------------
# Watch-and-block: if every backend process dies, exit so AMP restarts us
# --------------------------------------------------------------------------
while true; do
  any_alive=0
  for svc in "${SERVICES[@]}"; do
    pid=$(read_pid "$svc")
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && any_alive=1
  done
  if [ "$any_alive" = 0 ]; then
    warn "All services have exited: failed — bailing out so AMP can restart"
    [ -n "${TAIL_PID:-}" ] && kill "$TAIL_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 5 &
  wait $!   # `wait` so SIGTERM interrupts us promptly
done
