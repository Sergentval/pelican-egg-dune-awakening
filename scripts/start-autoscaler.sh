#!/bin/bash
# Launch the demand-based autoscaler loop in the background via launch_bg, so
# console.sh's shutdown handler manages it like the other services. The loop
# (admin_autoscaler.py scan-loop) ticks every scan_interval_secs, NO-OPS while
# data/admin/autoscaler.json enabled:false, and survives a malformed config
# without dying. Each armed tick reads new director.log travel-demand + the live
# per-map online count and scales the 3 on-demand maps via mock-k8s ServerSetScale
# (drain idle maps to their floor, wake/load-scale up). No extra env needed — the
# mock-k8s + server-status + DB paths are all in-container.
set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
export SOURCE="autoscaler"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

PY="${DUNE_PYTHON3:-python3}"
SCRIPT="$SCRIPTS/admin_autoscaler.py"

if [ ! -r "$SCRIPT" ]; then
    log "WARN $SCRIPT missing — autoscaler not started"
    exit 0
fi

launch_bg autoscaler "$LOGS/autoscaler.log" -- \
    env DUNE_BASE_DIR="$BASE" \
        "$PY" "$SCRIPT" scan-loop "$BASE"
log "autoscaler started (no-op while autoscaler.json enabled:false)"
