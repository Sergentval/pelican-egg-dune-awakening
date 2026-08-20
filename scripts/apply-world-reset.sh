#!/bin/bash
# Boot-time consumer of the world-reset / world-rollback markers (DST
# worldreset-2 port — see scripts/admin_worldreset.py for the full story).
#
# Ordered BEFORE prestart.sh in the entrypoint: prestart initdb's + loads
# the schema whenever server/state/pg is fresh, so "reset the world" here
# is just moving the datadir aside and letting the ordinary first-boot
# path build an empty world under the same battlegroup identity/config.
#
# Every failure path boots the OLD world untouched (fail closed) and
# leaves a result marker the panel surfaces — nothing here may brick the
# boot or half-wipe anything: the datadir is MOVED in one atomic rename,
# never deleted, never copied piecemeal.

set -uo pipefail  # deliberately NOT -e — marker trouble must not brick the boot

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
STATE="$BASE/server/state"
PY="$BASE/scripts/admin_worldreset.py"
TS=$(date -u +%Y%m%d-%H%M%S)

# Bound the disk cost of preserved worlds: keep the 2 newest pg.pre-reset-*
# and the 1 newest pg.rolled-back-*. Run on BOTH executing paths (reset and
# rollback) — each creates a parked dir, and the docs promise the bound.
prune_preserved() {
    python3 - "$STATE" <<'PYEOF' || true
import os, re, shutil, sys
state = sys.argv[1]
for pat, keep in ((re.compile(r"^pg\.pre-reset-\d{8}-\d{6}$"), 2),
                  (re.compile(r"^pg\.rolled-back-\d{8}-\d{6}$"), 1)):
    dirs = sorted([d for d in os.listdir(state) if pat.match(d)], reverse=True)
    for d in dirs[keep:]:
        shutil.rmtree(os.path.join(state, d), ignore_errors=True)
PYEOF
}

# Engine presence before trusting any exit code (the C3.5 review lesson:
# python3 on a missing/empty file exits non-zero/zero in ways that are
# indistinguishable from real answers).
if [ ! -s "$PY" ]; then
    if [ -e "$STATE/world-reset-pending.json" ] || [ -e "$STATE/world-rollback-pending.json" ]; then
        echo "[world-reset] [WARN] scripts/admin_worldreset.py is missing or empty but a world marker exists — NOT touching the world. Reinstall the server to resync scripts."
    fi
    exit 0
fi

# ---- rollback first: it only exists when the operator armed it, and it
# ---- must win over a stale reset marker.
rb=$(python3 "$PY" rollback-pending "$BASE" 2>/dev/null) && {
    dir=$(printf '%s' "$rb" | python3 -c 'import json,sys; print(json.load(sys.stdin)["restore_dir"])' 2>/dev/null)
    if [ -z "$dir" ] || [ ! -f "$STATE/$dir/data/PG_VERSION" ]; then
        echo "[world-reset] [WARN] rollback target '${dir:-?}' is missing or not a datadir — NOT touching the world"
        python3 "$PY" write-result "$BASE" rollback fail "target ${dir:-?} missing at boot" || true
        python3 "$PY" clear-rollback "$BASE" || true
        exit 0
    fi
    moved=""
    if [ -d "$STATE/pg" ]; then
        moved="pg.rolled-back-$TS"
        if ! mv "$STATE/pg" "$STATE/$moved"; then
            echo "[world-reset] [WARN] could not set the current datadir aside — aborting rollback, booting unchanged"
            # Clear the marker like every other refusal path: a persistent mv
            # obstacle would otherwise silently re-attempt on every boot with
            # only last_result changing. The operator re-arms consciously.
            python3 "$PY" write-result "$BASE" rollback fail "could not move current pg aside — marker cleared, re-arm to retry" || true
            python3 "$PY" clear-rollback "$BASE" || true
            exit 0
        fi
    fi
    if ! mv "$STATE/$dir" "$STATE/pg"; then
        echo "[world-reset] [WARN] could not move $dir into place — restoring the previous datadir and booting unchanged"
        if [ -n "$moved" ]; then mv "$STATE/$moved" "$STATE/pg" || true; fi
        python3 "$PY" write-result "$BASE" rollback fail "could not move $dir into place — marker cleared, re-arm to retry" || true
        python3 "$PY" clear-rollback "$BASE" || true
        exit 0
    fi
    # The schema-loaded sentinel travels WITH its world: park the fresh
    # world's copy, and the restored world provably has a schema — recreate
    # its sentinel unconditionally so prestart never re-runs resetdb over
    # live data.
    if [ -n "$moved" ] && [ -f "$STATE/schema-loaded" ]; then
        mv "$STATE/schema-loaded" "$STATE/$moved/schema-loaded.sentinel" || true
    fi
    touch "$STATE/schema-loaded" || true
    prune_preserved
    python3 "$PY" clear-rollback "$BASE" || true
    python3 "$PY" clear-pending "$BASE" || true
    python3 "$PY" write-result "$BASE" rollback ok "world restored from $dir" "$moved" || true
    echo "[world-reset] [INFO] ROLLBACK — preserved world restored from $dir (the fresh world was kept as ${moved:-<none>})"
    exit 0
}

# ---- reset
python3 "$PY" pending "$BASE" >/dev/null 2>&1 || exit 0
if ! python3 "$PY" verify-pending "$BASE"; then
    echo "[world-reset] [WARN] reset marker failed verification (see line above) — NOT touching the world; marker cleared"
    python3 "$PY" write-result "$BASE" reset fail "marker verification failed at boot" || true
    python3 "$PY" clear-pending "$BASE" || true
    exit 0
fi
if [ ! -d "$STATE/pg" ]; then
    python3 "$PY" clear-pending "$BASE" || true
    python3 "$PY" write-result "$BASE" reset ok "no previous datadir existed" || true
    exit 0
fi
keep="pg.pre-reset-$TS"
if ! mv "$STATE/pg" "$STATE/$keep"; then
    echo "[world-reset] [WARN] could not set the datadir aside — NOT resetting; booting the old world"
    # Same marker discipline as the rollback mv failures: clear, don't loop.
    python3 "$PY" write-result "$BASE" reset fail "could not move pg aside — marker cleared, re-arm to retry" || true
    python3 "$PY" clear-pending "$BASE" || true
    exit 0
fi
# prestart gates its schema load (resetdb, role + dune database creation)
# on $STATE/schema-loaded, which lives OUTSIDE the datadir. It must travel
# with the preserved world, or the fresh boot skips schema creation and
# migrate-db dies on "database dune does not exist" (found the hard way on
# the live e2e). Rollback restores it from the preserved dir. If the park
# itself fails, force the safe default — sentinel ABSENT (a stale present
# sentinel reproduces the migrate-db dead end; absent merely re-runs the
# schema load on the fresh, empty datadir).
if [ -f "$STATE/schema-loaded" ]; then
    mv "$STATE/schema-loaded" "$STATE/$keep/schema-loaded.sentinel" \
        || rm -f "$STATE/schema-loaded" || true
fi
prune_preserved
python3 "$PY" clear-pending "$BASE" || true
python3 "$PY" write-result "$BASE" reset ok "fresh world; previous datadir preserved" "$keep" || true
echo "[world-reset] [INFO] WORLD RESET — previous datadir preserved as $keep; prestart will now build a fresh world"
exit 0
