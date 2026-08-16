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
SERVICES=(postgres mq-admin mq-game text-router fls-stub mock-k8s director gateway admin-http ue5-Survival_1 ue5-Overmap ue5-DeepDesert_1)

# Services whose death makes the battlegroup non-functional even though the
# rest of the tree is still alive. The supervisor loop below only used to act
# when EVERYTHING died or every UE5 died — so losing exactly one of these
# left a server that boots, registers with FLS, and then quietly fails every
# player action. That is the failure mode behind issue #82: with text-router
# (the RMQ auth backend) gone, RabbitMQ refuses every login and the Director
# just logs "RMQ unreachable" forever while the container stays "healthy".
# Bailing out lets Wings recreate the container, which actually fixes it.
#
# fls-stub, admin-http and mock-k8s are deliberately NOT here: the first two
# are optional conveniences, and mock-k8s only matters while spawning (a dead
# one is caught by the zero-UE5 rule).
CRITICAL_SERVICES=(postgres mq-admin mq-game text-router director gateway)

# Per-service hint printed when one of them dies, so the operator is pointed
# at the right log instead of at the symptom.
critical_hint() {
  case "$1" in
    text-router) echo "RMQ auth backend — every broker login is authorised here; the Director will report 'RMQ unreachable' (issue #82)" ;;
    mq-admin|mq-game) echo "RabbitMQ broker — Director/gateway/UE5 traffic all flows through it" ;;
    postgres)    echo "database — character and world state cannot be read or written" ;;
    director)    echo "Battlegroup Director — no instance scaling, travel or login routing" ;;
    gateway)     echo "FLS gateway — the server stops being advertised and drops out of the browser" ;;
    *)           echo "critical service" ;;
  esac
}

is_critical() {
  local svc=$1 c
  for c in "${CRITICAL_SERVICES[@]}"; do
    [ "$c" = "$svc" ] && return 0
  done
  return 1
}

# The non-critical services are worth a line when they die, but never worth
# recreating the container for — the battlegroup keeps running without them.
# Losing admin-http costs the operator remote control, not the session their
# players are in, so bouncing everyone to recover a web UI would be a worse
# outage than the one being recovered from. The supervisor below reports them
# once and leaves the decision to the operator (`panel restart`).
noncritical_hint() {
  case "$1" in
    admin-http) echo "the admin panel is unreachable; the game is unaffected — type 'panel restart' in this console to bring it back" ;;
    fls-stub)   echo "FLS token broker used by the gateway" ;;
    mock-k8s)   echo "instance-scaling backend the Director calls" ;;
    *)          echo "background service" ;;
  esac
}

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
    "admin-http"
    "ue5-Survival_1 ue5-Overmap ue5-DeepDesert_1"
    "gateway"
    "director"
    "mock-k8s"
    "text-router"
    "mq-game mq-admin"
    "postgres"
  )
  declare -A GRACE=(
    ["admin-http"]=3
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
  # Stop the admin stdin listener too (it'd block on read otherwise)
  if [ -n "${ADMIN_LISTENER_PID:-}" ]; then
    kill -TERM "$ADMIN_LISTENER_PID" 2>/dev/null || true
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
  # read_pid returns "" for a missing pid file AND for a pid that is no
  # longer alive, so the two cases can't be told apart here — check the
  # file directly to keep the message honest about which one happened.
  pid=$(read_pid "$svc")
  if [ -n "$pid" ]; then
    log "  $svc: pid $pid OK"
  elif [ -f "$(pid_file "$svc")" ]; then
    warn "  $svc: pid $(cat "$(pid_file "$svc")" 2>/dev/null) not running — it started and then exited"
    all_ok=0
  else
    warn "  $svc: no PID file — service did not start"
    all_ok=0
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
# `panel` console commands — recover the admin panel without restarting the
# server. Deliberately operator-triggered rather than automatic: an
# unattended restart loop on a service that is crashing for a reason would
# hide the reason and spam the log, and nothing else here self-heals.
#
# Every fallible command is guarded: lib.sh sets `set -e`, and this runs
# inside the listener's background subshell, so an unguarded non-zero
# status would kill the stdin listener and silently take every console
# command with it.
# --------------------------------------------------------------------------
panel_stop() {
  local pid
  pid=$(read_pid admin-http) || true
  if [ -z "$pid" ]; then
    printf '[panel] [INFO] admin-http was not running\n'
    return 0
  fi
  printf '[panel] [INFO] stopping admin-http (pid %s)\n' "$pid"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in 1 2 3; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    printf '[panel] [WARN] pid %s ignored SIGTERM after 3s — SIGKILL\n' "$pid"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi
  return 0
}

panel_command() {
  local sub=$1 pid
  case "$sub" in
    status)
      pid=$(read_pid admin-http) || true
      if [ -n "$pid" ]; then
        printf '[panel] [INFO] admin-http is running (pid %s), mode=%s\n' \
          "$pid" "$([ "${DUNE_ADMIN_UI_ENABLED:-0}" = "1" ] && echo UI || echo internal)"
      else
        printf '[panel] [INFO] admin-http is NOT running — type "panel restart" to start it\n'
      fi
      ;;
    restart)
      panel_stop
      # start-admin-http.sh re-reads the password and the session secret
      # from $STATE via lib.sh, so the restarted panel signs tokens with the
      # same key: operators who were logged in stay logged in.
      #
      # Output goes to a file rather than down a pipe, and the run is
      # timeout-bounded. Piping it would hand the launcher's stdout to
      # whatever it backgrounds; one descendant holding that pipe open
      # blocks the reader forever and takes the console's stdin listener
      # with it. launch_bg redirects to the service log today, but this
      # command must not be one refactor away from wedging the console.
      local out; out=$(mktemp)
      timeout 60 bash "$SCRIPTS/start-admin-http.sh" "$BASE" </dev/null >"$out" 2>&1 || true
      sed 's/^/[panel] /' "$out" || true
      rm -f "$out"
      ;;
    stop)
      panel_stop
      ;;
    *)
      printf '[panel] [INFO] usage: panel <status|restart|stop>\n'
      ;;
  esac
  return 0
}

# --------------------------------------------------------------------------
# Admin stdin listener: Pelican panel's "Console" tab forwards typed input
# to the container's stdin. We watch for lines starting with `admin ` (or
# `/admin `) and pipe them into scripts/admin-publish.sh — that's the
# AMQP-publish helper that reaches the seabass server-command handler.
# `panel ...` is handled locally instead (see panel_command above).
# Anything else is just echoed back with [admin] [INFO] so operators see
# their input in the panel log.
#
# Quoting: shlex.split via python3 so `admin broadcast "My Title" "My Body"`
# parses correctly. The operator is effectively root in the container, but
# we still avoid eval'ing arbitrary input.
# --------------------------------------------------------------------------
admin_listener() {
  local line trimmed rest
  while IFS= read -r line; do
    # Strip CR (panel may send \r\n)
    line="${line%$'\r'}"
    trimmed="${line#"${line%%[![:space:]]*}"}"   # ltrim
    case "$trimmed" in
      "")
        continue ;;
      /admin|admin)
        printf '[admin] [INFO] usage: admin <broadcast|shutdown|kick|give|xp|teleport|exec|raw> ...\n' ;;
      /panel|panel)
        panel_command "" ;;
      /panel\ *|panel\ *)
        rest="${trimmed#panel }"
        rest="${rest#/panel }"
        printf '[panel] [INFO] > %s\n' "$trimmed"
        panel_command "$rest" ;;
      /admin\ *|admin\ *)
        rest="${trimmed#admin }"
        rest="${rest#/admin }"
        printf '[admin] [INFO] > %s\n' "$trimmed"
        # shlex-parse the argv so quoted strings stay intact.
        local parsed
        if ! mapfile -t parsed < <(printf '%s' "$rest" \
              | python3 -c 'import shlex,sys; [print(t) for t in shlex.split(sys.stdin.read())]' 2>&1); then
          printf '[admin] [ERROR] could not parse argv: %s\n' "$rest"
          continue
        fi
        "$SCRIPTS/admin-publish.sh" "${parsed[@]}" 2>&1 \
          | sed 's/^/[admin] /'
        ;;
      *)
        # Unknown input — just echo so the operator sees their typing.
        printf '[stdin] [INFO] %s\n' "$trimmed"
        ;;
    esac
  done
}
admin_listener <&0 &
ADMIN_LISTENER_PID=$!

# --------------------------------------------------------------------------
# Watch-and-block: exit (and let Wings recreate the container) if any of:
#   1) every backend process is dead — total failure
#   2) zero UE5 game-server processes are alive for UE5_DEAD_GRACE secs,
#      while non-UE5 services (mq, pg, director, gateway) are still up.
#      This is the recurring DuneWorldPartitioner.cpp:1091 crash pattern:
#      Sietches all segfault, Director keeps polling mock-k8s producing
#      log spam, but the container looks "healthy" because mq/pg/etc.
#      are fine. Bail so Wings can power-cycle.
# --------------------------------------------------------------------------
UE5_DEAD_GRACE="${UE5_DEAD_GRACE:-90}"   # seconds before declaring UE5-down fatal
ue5_dead_since=0
# Shorter than UE5's: a dead broker or auth backend has no legitimate reason
# to be down for 90s, and every second of it is a player-visible failure.
CRITICAL_DEAD_GRACE="${CRITICAL_DEAD_GRACE:-20}"
critical_dead_since=0
# Consecutive misses seen per non-critical service. Reported on the second
# one (~5-10s) so an operator's own `panel restart` doesn't announce itself
# as a failure, then held until the service returns — one line per death,
# never a repeating alarm.
declare -A noncrit_misses=()
while true; do
  any_alive=0
  ue5_alive=0
  for svc in "${SERVICES[@]}"; do
    pid=$(read_pid "$svc")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      any_alive=1
      case "$svc" in ue5-*) ue5_alive=1 ;; esac
      if [ -n "${noncrit_misses[$svc]:-}" ]; then
        [ "${noncrit_misses[$svc]}" -ge 2 ] && log "$svc is back up (pid $pid)"
        unset 'noncrit_misses[$svc]'
      fi
    else
      # Non-critical and non-UE5: report, never act. UE5 and the critical
      # set have their own bail-out rules further down.
      case "$svc" in
        ue5-*) ;;
        *)
          if ! is_critical "$svc"; then
            noncrit_misses[$svc]=$(( ${noncrit_misses[$svc]:-0} + 1 ))
            if [ "${noncrit_misses[$svc]}" -eq 2 ]; then
              warn "$svc is not running — $(noncritical_hint "$svc")"
              warn "    see logs/$svc.log"
            fi
          fi
          ;;
      esac
    fi
  done

  # Total-failure exit (case 1).
  if [ "$any_alive" = 0 ]; then
    warn "All services have exited: failed — bailing out so Wings can restart"
    [ -n "${TAIL_PID:-}" ] && kill "$TAIL_PID" 2>/dev/null || true
    exit 1
  fi

  # Critical-service exit (case 1b): one of the load-bearing services died
  # while the rest of the tree survived. Left alone this produces a server
  # that looks up but cannot serve — see CRITICAL_SERVICES above. Grace is
  # one loop iteration so a service that is mid-restart isn't misreported.
  dead_critical=""
  for svc in "${CRITICAL_SERVICES[@]}"; do
    pid=$(read_pid "$svc")
    [ -z "$pid" ] && dead_critical="$dead_critical $svc"
  done
  if [ -n "$dead_critical" ]; then
    if [ "$critical_dead_since" = 0 ]; then
      critical_dead_since=$(date +%s)
      warn "Critical service(s) not running:$dead_critical — re-checking before bailing out"
    elif [ $(( $(date +%s) - critical_dead_since )) -ge "$CRITICAL_DEAD_GRACE" ]; then
      for svc in $dead_critical; do
        warn "  $svc is down — $(critical_hint "$svc")"
        warn "    see logs/$svc.log"
      done
      warn "Bailing out so Wings recreates the container"
      [ -n "${TAIL_PID:-}" ] && kill "$TAIL_PID" 2>/dev/null || true
      exit 3
    fi
  elif [ "$critical_dead_since" != 0 ]; then
    log "Critical services healthy again — cancelling restart grace"
    critical_dead_since=0
  fi

  # Zero-UE5 exit (case 2): only count when EVERY ue5-* PID is dead AND
  # any of the always-warm Sietches is in SERVICES. The OS-level survey
  # via pgrep catches lingering UE5 processes the spawner might not have
  # PID-tracked yet (defensive, prevents flapping during respawn).
  if [ "$ue5_alive" = 0 ] && \
     ! pgrep -f DuneSandboxServer-Linux-Shipping > /dev/null 2>&1; then
    if [ "$ue5_dead_since" = 0 ]; then
      ue5_dead_since=$(date +%s)
      warn "All UE5 Sietches dead — starting ${UE5_DEAD_GRACE}s grace before forcing restart"
    fi
    elapsed=$(( $(date +%s) - ue5_dead_since ))
    if [ "$elapsed" -ge "$UE5_DEAD_GRACE" ]; then
      warn "UE5 dead for ${elapsed}s — bailing out so Wings recreates the container"
      [ -n "${TAIL_PID:-}" ] && kill "$TAIL_PID" 2>/dev/null || true
      exit 2
    fi
  else
    # UE5 came back (or never went down). Reset the grace counter.
    if [ "$ue5_dead_since" != 0 ]; then
      log "UE5 Sietches alive again — cancelling restart grace"
      ue5_dead_since=0
    fi
  fi

  sleep 5 &
  wait $!   # `wait` so SIGTERM interrupts us promptly
done
