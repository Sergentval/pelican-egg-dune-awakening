#!/bin/bash
# Launch the welcome-kit scanner loop (Phase 6) in the background, using the
# same launch_bg + PID-file pattern as the other services so console.sh's
# shutdown handler picks it up.
#
# It runs admin_welcome.py scan-loop, which polls every scan_interval_secs and
# grants the active welcome package to online players not yet recorded. The loop
# no-ops while the kit is disabled (data/admin/welcome-kit.json enabled:false),
# so it is always safe to launch — nothing is granted until an operator defines
# items and flips enabled:true.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
export SOURCE="welcome-scanner"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

PY="${DUNE_PYTHON3:-python3}"
SCRIPT="$SCRIPTS/admin_welcome.py"

if [ ! -r "$SCRIPT" ]; then
    log "WARN $SCRIPT missing — welcome-scanner not started"
    exit 0
fi

launch_bg welcome-scanner "$LOGS/welcome-scanner.log" -- \
    env DUNE_BASE_DIR="$BASE" "$PY" "$SCRIPT" scan-loop "$BASE"
log "welcome-scanner started (no-op while welcome-kit.json enabled:false)"
