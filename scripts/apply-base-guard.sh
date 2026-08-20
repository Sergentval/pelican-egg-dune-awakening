#!/bin/bash
# Boot-time re-apply of the BaseBackup wipe-guard (see admin_baseguard.py
# for the full story). Runs right after migrate-db.sh in the entrypoint:
# the guarded function is Funcom-owned, and a game update can ship a boot
# migration that replaces it and silently drops our predicate — on this
# single-container stack, migrations only ever run at boot, so re-applying
# here (rather than on a timer, as upstream DST does across its VM
# boundary) covers every replacement window.
#
# No-op unless data/admin/base-guard.json says {"enabled": true} — the
# guard rewrites a Funcom function, so it is strictly opt-in. Never blocks
# the boot: a failure is a WARN and the operator can apply from the panel.

set -uo pipefail  # deliberately NOT -e — guard trouble must not brick the boot

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"

if ! python3 "$BASE/scripts/admin_baseguard.py" enabled "$BASE"; then
    echo "[base-guard] [INFO] boot re-apply disabled (data/admin/base-guard.json) — skipping"
    exit 0
fi

if out=$(bash "$BASE/scripts/admin-publish.sh" base-guard-apply 2>&1); then
    echo "[base-guard] [INFO] $(printf '%s' "$out" | grep '^\[admin-publish\] OK' | head -n1 | cut -c17-)"
else
    echo "[base-guard] [WARN] boot re-apply failed — stored base backups are NOT wipe-protected until it succeeds; apply manually from the panel. Detail: $(printf '%s' "$out" | tail -n1 | head -c 300)"
fi
exit 0
