#!/bin/bash
# Integration test for the console's `panel` commands and the non-critical
# service notice. Drives the REAL console.sh inside a throwaway fake
# container — the failure modes here (a wedged stdin listener, a repeating
# alarm, set -e killing the listener) only exist when the whole loop runs.
#
# Needs nothing but bash/gawk/coreutils. Takes ~40s (the supervisor loop
# ticks every 5s and the death notice waits one tick).
#
# Run: bash scripts/test_console_panel.sh
set -u
SRC=$(dirname "$(readlink -f "$0")")
BASE=$(mktemp -d)
trap 'pkill -P $$ 2>/dev/null; rm -rf "$BASE"' EXIT

mkdir -p "$BASE/scripts" "$BASE/server/state" "$BASE/runtime/pids" "$BASE/logs"
cp "$SRC"/*.sh "$SRC"/*.py "$BASE/scripts/" 2>/dev/null
chmod +x "$BASE/scripts"/*.sh

# Stub launcher: stands in for the real one (which needs postgres, a depot
# and a UE5 tree). Same contract — background a process, write its pid file.
# DELIBERATELY HOSTILE: the backgrounded process inherits stdout instead of
# being redirected to a log the way launch_bg does. If panel_command pipes
# the launcher's output anywhere, this wedges the reader for 600s and the
# console's stdin listener dies with it. The test must still pass.
cat > "$BASE/scripts/start-admin-http.sh" <<'STUB'
#!/bin/bash
set -euo pipefail
BASE="${1:?}"
setsid sleep 600 &
echo $! > "$BASE/runtime/pids/admin-http.pid"
echo "[admin-http] [INFO] admin-http ready: success (pid $!)"
STUB
chmod +x "$BASE/scripts/start-admin-http.sh"

# Keep every critical service "alive" so the supervisor doesn't bail on us.
setsid sleep 600 & KEEPALIVE=$!
for s in postgres mq-admin mq-game text-router director gateway fls-stub mock-k8s \
         ue5-Survival_1 ue5-Overmap ue5-DeepDesert_1 admin-http; do
  echo "$KEEPALIVE" > "$BASE/runtime/pids/$s.pid"
done
# admin-http gets its own pid so we can kill it without killing the rest.
setsid sleep 600 & PANEL_PID=$!
echo "$PANEL_PID" > "$BASE/runtime/pids/admin-http.pid"

FIFO="$BASE/stdin"; mkfifo "$FIFO"
OUT="$BASE/console.out"
export UE5_DEAD_GRACE=9999 CRITICAL_DEAD_GRACE=9999 DUNE_ADMIN_UI_ENABLED=1
timeout 70 bash "$BASE/scripts/console.sh" "$BASE" < "$FIFO" > "$OUT" 2>&1 &
CONSOLE=$!
exec 3>"$FIFO"   # hold the write end open

say() { printf '%s\n' "$1" >&3; }
wait_for() {  # wait_for <regex> <seconds> <label>
  local deadline=$((SECONDS + $2))
  while [ $SECONDS -lt $deadline ]; do
    grep -qE "$1" "$OUT" && { echo "PASS  $3"; return 0; }
    sleep 0.5
  done
  echo "FAIL  $3   (no match for /$1/)"
  return 1
}

fails=0
sleep 3

say "panel status"
wait_for "admin-http is running \(pid $PANEL_PID\)" 10 "panel status → reports running + pid" || fails=1

say "panel"
wait_for "usage: panel <status\|restart\|stop>" 10 "bare 'panel' → usage" || fails=1

say "panel bogus"
wait_for "usage: panel" 10 "unknown subcommand → usage" || fails=1

say "panel stop"
wait_for "stopping admin-http \(pid $PANEL_PID\)" 10 "panel stop → SIGTERMs the panel" || fails=1
sleep 1
if kill -0 "$PANEL_PID" 2>/dev/null; then echo "FAIL  panel process actually dead"; fails=1
else echo "PASS  panel process actually dead"; fi

# Supervisor should now notice — on the SECOND miss, not the first.
wait_for "admin-http is not running" 20 "supervisor reports the death" || fails=1
wait_for "type 'panel restart' in this console" 5 "…with the recovery hint" || fails=1
wait_for "see logs/admin-http.log" 5 "…and the log pointer" || fails=1

say "panel status"
wait_for "admin-http is NOT running" 10 "panel status → reports stopped" || fails=1

say "panel restart"
wait_for "admin-http ready: success" 15 "panel restart → relaunches it" || fails=1
wait_for "admin-http is back up" 20 "supervisor notices the recovery" || fails=1

# The notice must be one-shot, not a repeating alarm.
n=$(grep -c "admin-http is not running" "$OUT")
if [ "$n" = 1 ]; then echo "PASS  death reported exactly once (not a repeating alarm)"
else echo "FAIL  death reported $n times"; fails=1; fi

# The stdin listener must still be alive after all that — and must report the
# NEW pid, so a stale line from the first status can't make this pass.
NEWPID=$(grep -oP "admin-http is back up \(pid \K[0-9]+" "$OUT" | tail -1)
say "panel status"
wait_for "admin-http is running \(pid ${NEWPID:-none}\)" 10 "listener survived every command (reports new pid $NEWPID)" || fails=1

exec 3>&-
kill $CONSOLE 2>/dev/null
echo
[ "$fails" = 0 ] && echo "ALL PASS" || { echo "SOME FAILED"; echo "--- console output ---"; cat "$OUT"; }
exit "$fails"
