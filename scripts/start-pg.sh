#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-pg.sh BASE_DIR
#
# Run by AMP as a PreStartStage AFTER prestart.sh has done initdb/schema.
# Launches the long-lived postgres process in the background, records its
# PID, and waits for it to accept connections before returning.

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
