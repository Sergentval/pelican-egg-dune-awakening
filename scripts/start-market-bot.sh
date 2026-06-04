#!/bin/bash
# Launch the market-bot autonomous loop (7b-3) in the background via launch_bg,
# so console.sh's shutdown handler manages it like the other services. The loop
# (admin_market.py scan-loop) ticks every scan_interval_secs, NO-OPS while
# data/admin/market-bot.json enabled:false, and survives a malformed config
# without dying. Each armed tick tops the bot's NPC listings up toward
# max_listings and runs a gamble-buy pass (buy cheap player orders, d-die gated).
# Pure-DB: it needs no panel API vars, only DUNE_BASE_DIR.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
export SOURCE="market-bot"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

PY="${DUNE_PYTHON3:-python3}"
SCRIPT="$SCRIPTS/admin_market.py"

if [ ! -r "$SCRIPT" ]; then
    log "WARN $SCRIPT missing — market-bot not started"
    exit 0
fi

launch_bg market-bot "$LOGS/market-bot.log" -- \
    env DUNE_BASE_DIR="$BASE" \
        "$PY" "$SCRIPT" scan-loop "$BASE"
log "market-bot started (no-op while market-bot.json enabled:false)"
