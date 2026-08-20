#!/bin/bash
# Chat-commands daemon: a small tick loop over admin_chatcmd.py. The feature
# is OFF by default (data/admin/chat-commands.json enabled=false); a disabled
# tick is a no-op that also drops the copy-queue once, so running this
# unconditionally at boot costs nothing. Supervised like the other daemons:
# pidfile under runtime/pids, console.sh reads liveness from it.
set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
PIDDIR="$BASE/runtime/pids"
LOGF="$BASE/logs/chat-commands.log"
TICK_SECS="${DUNE_CHAT_COMMANDS_TICK_SECS:-5}"
case "$TICK_SECS" in *[!0-9]*|"") TICK_SECS=5;; esac
[ "$TICK_SECS" -lt 2 ] && TICK_SECS=2

mkdir -p "$PIDDIR" "$BASE/logs"

(
    echo "[chat-commands] loop starting (tick ${TICK_SECS}s; master switch in data/admin/chat-commands.json)"
    while true; do
        python3 "$BASE/scripts/admin_chatcmd.py" tick "$BASE" 2>&1 || true
        sleep "$TICK_SECS"
    done
) >> "$LOGF" 2>&1 &

echo $! > "$PIDDIR/chat-commands.pid"
echo "[start-chat-commands] started pid $(cat "$PIDDIR/chat-commands.pid") (log: logs/chat-commands.log)"
