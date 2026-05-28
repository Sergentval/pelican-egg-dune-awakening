#!/bin/bash
# Originally © 2026 CubeCoders Limited (MIT). Modified for Pelican Wings.
# See ATTRIBUTION.md.
#
# start-pg.sh BASE_DIR
#
# Run by pelican-entrypoint.sh AFTER prestart.sh has done initdb/schema.
# Launches the long-lived postgres process in the background, records its
# PID, waits for it to accept connections, and wipes any stale farm_state
# entries left behind by crashed Sietches from prior boots.

export SOURCE="postgres"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

PGDATA="$STATE/pg/data"
[ -f "$PGDATA/PG_VERSION" ] || die "Postgres data dir not initialised — did pre-start run?"

log "Starting Postgres on 127.0.0.1:$DUNE_PG_PORT..."

# Re-export so the bg process inherits cleanly
export LD_LIBRARY_PATH=$(ldlib postgres)
export ICU_DATA=$(icu_dir postgres)
export PGDATA

launch_bg postgres "$LOGS/postgres.log" -- \
  "$EXTRACTED/postgres/usr/local/bin/postgres" \
    -D "$PGDATA" \
    -c "config_file=$PGDATA/postgresql.conf"

if wait_for_port 127.0.0.1 "$DUNE_PG_PORT" 30; then
  log "Postgres ready: success (pid $(read_pid postgres))"
else
  tail -30 "$LOGS/postgres.log" >&2
  die "Postgres failed to listen within 30s"
fi

# --------------------------------------------------------------------------
# Clean stale farm_state from prior crashed boots.
#
# UE5 instances register themselves in dune.farm_state on startup but only
# clean up the row on graceful exit. A segfaulted Sietch leaves a row
# behind. After a few crash cycles the table accumulates 3-5 duplicate
# rows per map. Fresh UE5 instances reading the polluted state hit the
# DuneWorldPartitioner.cpp:1091 segfault (which then leaves another stale
# row, perpetuating the loop).
#
# At this point in the boot, no UE5 has started yet — every existing row
# is by definition orphaned. The FK from world_partition.server_id is
# ON DELETE SET NULL so this just nulls those references; fresh Sietches
# repopulate both tables on their own boot.
#
# Skip silently if the schema isn't loaded yet (first install — prestart
# would have just initdb'd PGDATA and the schema-load hasn't created
# dune.farm_state yet). On those first boots there's nothing to clean.
# --------------------------------------------------------------------------
SOCKET_DIR="$BASE/runtime/postgresql"
PGBIN="$EXTRACTED/postgres/usr/local/bin"
STALE_BEFORE=$("$PGBIN/psql" -h "$SOCKET_DIR" -p "$DUNE_PG_PORT" -U dune -d dune \
    -tAc "SELECT count(*) FROM dune.farm_state" 2>/dev/null || echo "")
if [ -z "$STALE_BEFORE" ]; then
  log "  farm_state table not present (first install) — skipping stale-row cleanup"
elif [ "$STALE_BEFORE" = "0" ]; then
  log "  farm_state already empty — no cleanup needed"
else
  "$PGBIN/psql" -h "$SOCKET_DIR" -p "$DUNE_PG_PORT" -U dune -d dune \
    -c "DELETE FROM dune.farm_state" >/dev/null 2>&1 \
    && log "  wiped $STALE_BEFORE stale farm_state row(s) from prior boots" \
    || warn "  farm_state cleanup failed (non-fatal — manual DELETE may be needed)"
fi
