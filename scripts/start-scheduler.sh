#!/bin/bash
# Launch the unattended scheduler loop (auto-restart + auto-backup) in the
# background via launch_bg, so console.sh's shutdown handler manages it like the
# other services. The loop (admin_schedule.py scan-loop) ticks every ~30s, no-ops
# while both tasks are disabled in data/admin/schedule.json, and survives a
# malformed config without dying. Restart needs the Pelican client-API vars
# (DUNE_PELICAN_URL/CLIENT_KEY/SERVER_ID); without them the restart task self-skips.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
export SOURCE="scheduler"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

PY="${DUNE_PYTHON3:-python3}"
SCRIPT="$SCRIPTS/admin_schedule.py"

if [ ! -r "$SCRIPT" ]; then
    log "WARN $SCRIPT missing — scheduler not started"
    exit 0
fi

launch_bg scheduler "$LOGS/scheduler.log" -- \
    env DUNE_BASE_DIR="$BASE" \
        DUNE_PELICAN_URL="${DUNE_PELICAN_URL:-}" \
        DUNE_PELICAN_CLIENT_KEY="${DUNE_PELICAN_CLIENT_KEY:-}" \
        DUNE_PELICAN_SERVER_ID="${DUNE_PELICAN_SERVER_ID:-}" \
        DUNE_BACKUP_RETENTION="${DUNE_BACKUP_RETENTION:-}" \
        "$PY" "$SCRIPT" scan-loop "$BASE"
log "scheduler started (no-op while schedule.json restart/backup both disabled)"
