#!/bin/bash
# Player-events + battlepass daemon (dune-admin port — see
# scripts/admin_events.py / admin_battlepass.py). One loop drives both
# engines; each paces itself internally (per-event poll+jitter, the
# battlepass poll_seconds), so the loop tick just sets the floor. Both
# engines are OFF by default (data/admin/events-engine.json /
# battlepass.json) and a disabled engine's tick is a no-op — always safe
# to launch. The first events tick after a (re)start runs the silent
# milestone backfill (--boot) so a restart never re-announces past deeds.
set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
PIDDIR="$BASE/runtime/pids"
LOGF="$BASE/logs/player-events.log"
TICK_SECS="${DUNE_PLAYER_EVENTS_TICK_SECS:-5}"
case "$TICK_SECS" in *[!0-9]*|"") TICK_SECS=5;; esac
[ "$TICK_SECS" -lt 2 ] && TICK_SECS=2

mkdir -p "$PIDDIR" "$BASE/logs"

(
    echo "[player-events] loop starting (tick ${TICK_SECS}s; switches in data/admin/events-engine.json + battlepass.json)"
    boot="--boot"
    while true; do
        python3 "$BASE/scripts/admin_events.py" tick "$BASE" $boot 2>&1 || true
        boot=""
        python3 "$BASE/scripts/admin_battlepass.py" tick "$BASE" 2>&1 || true
        sleep "$TICK_SECS"
    done
) >> "$LOGF" 2>&1 &

echo $! > "$PIDDIR/player-events.pid"
echo "[start-player-events] started pid $(cat "$PIDDIR/player-events.pid") (log: logs/player-events.log)"
