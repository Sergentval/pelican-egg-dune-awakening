#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# migrate-db.sh BASE_DIR
#
# Apply Funcom's schema migrations against the running postgres before
# any downstream services start. Funcom ships their migration runner as
# the db-utils image (extracted/db-utils), which we invoke directly.
#
# The runner is idempotent — it tracks applied patches in dune.patches
# and skips any that are already in. Exits 0 on no-op as well as success.
#
# Exit codes from Funcom's updatedb.py (see funcomdb/app.py):
#   0  Success (or already up-to-date)
#   1  ConnectionFailure — postgres unreachable
#   6  PatchHasDifference — would-be patch differs from schema (manual review)
#   7  InvalidLegacyPatchFound — corrupt legacy patch file
#   8  InvalidPatchFilesFound — malformed patch file name
#   9  UnregisteredPatchesFound — DB has patches the current build doesn't
#                                 recognise (DOWNGRADE — refuse to proceed)
#  10  DatabaseSetupForTestFailed — internal test setup error
set -e

BASE="${1:-${DUNE_BASE_DIR:-}}"
export SOURCE="migrate"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

DBU="$EXTRACTED/db-utils"
PG="$EXTRACTED/postgres"

if [ ! -d "$DBU/root/PSQL" ]; then
  log "db-utils image not extracted — install.sh hasn't run yet; skipping migrations"
  exit 0
fi

log "Running Funcom DB migrations (Database/Upgrade/*.sql)..."

# All LD paths from both images so the musl python finds its libs AND
# psql/libpq if the migration runner shells out to psql for any reason.
LD="$DBU/lib:$DBU/usr/lib:$DBU/usr/local/lib:$PG/lib:$PG/usr/lib:$PG/usr/local/lib"

# DUNE-FUNCOM-EVENTLOG-OWNER - Funcom's event_log partition maintenance
# creates event_log_pN child tables owned by the postgres superuser, not
# dune. updatedb.py's schema-diff dumps as dune and then fails to LOCK
# them ("permission denied for table event_log_pN"), aborting the
# migration. Re-home every dune-schema table to the dune role first.
# Idempotent; remove once Funcom fixes partition ownership.
LD_LIBRARY_PATH="$LD" "$PG/usr/local/bin/psql" \
  -h 127.0.0.1 -p "${DUNE_PG_PORT:-15432}" -U postgres -d dune -tA -c \
  "DO \$\$ DECLARE r record; BEGIN \
     FOR r IN SELECT tablename FROM pg_tables \
              WHERE schemaname='dune' AND tableowner<>'dune' LOOP \
       EXECUTE format('ALTER TABLE dune.%I OWNER TO dune', r.tablename); \
       RAISE NOTICE 'reassigned ownership of % to dune', r.tablename; \
     END LOOP; END \$\$;" 2>&1 | sed 's/^/[migrate] /' \
  || warn "table-ownership reassignment failed (non-fatal) - migration may abort"

# DUNE-FUNCOM-MIGRATE-FALSEOK - capture updatedb.py output so the RC=0
# branch can scan it for failure markers (updatedb.py has been seen to
# log a fatal error yet still exit 0).
MIGRATE_OUT="$RUNTIME/migrate-last.log"

# DUNE-FUNCOM-MIGRATE-TIMEOUT - bump updatedb.py's per-psql-command
# timeout from its hard-coded 10min default. Funcom's runner reads this
# env var via funcomdb.installation.api._get_command_timeout(). Big DDL
# patches and first-run schema loads on slow disks can easily exceed
# 10 minutes; 1 hour gives plenty of headroom. Admin override possible
# via state/UserOverrides.ini-style env, but most users will never care.
export FUNCOM_DATABASE_COMMAND_TIMEOUT="${FUNCOM_DATABASE_COMMAND_TIMEOUT:-3600}"
log "DB-command timeout set to ${FUNCOM_DATABASE_COMMAND_TIMEOUT}s (Funcom default: 600s)"

# DUNE-MIGRATE-HEARTBEAT - updatedb.py logs each patch as it applies but
# can sit silent for minutes during big DDL ops. AMP's console looks
# frozen; users assume the server hung and kill it. Emit a heartbeat
# every 30s with elapsed time + most recent log line so it's visibly
# alive. Killed when the main runner returns.
_dune_migrate_heartbeat() {
  local started=$1 logfile=$2 line t mins secs
  while sleep 30; do
    if [ -f "$logfile" ]; then
      line=$(tail -n 1 "$logfile" 2>/dev/null | sed 's/^\[migrate\] *//')
    else
      line="(no output yet)"
    fi
    t=$(( $(date +%s) - started ))
    mins=$(( t / 60 )); secs=$(( t % 60 ))
    log "still working — ${mins}m ${secs}s elapsed, latest: ${line:-(idle)}"
  done
}

set +e
MIGRATE_STARTED=$(date +%s)
# Reset the captured output so a stale heartbeat doesn't see last run's
# trailing lines while updatedb.py is still warming up.
: > "$MIGRATE_OUT"
_dune_migrate_heartbeat "$MIGRATE_STARTED" "$MIGRATE_OUT" &
HEARTBEAT_PID=$!
# Make sure we never leak the heartbeat: kill it on any exit path.
trap 'kill "$HEARTBEAT_PID" 2>/dev/null; wait "$HEARTBEAT_PID" 2>/dev/null || true' EXIT
(
  cd "$DBU/root/PSQL" || exit 99
  PYTHONPATH=. LD_LIBRARY_PATH="$LD" \
    FUNCOM_DATABASE_COMMAND_TIMEOUT="$FUNCOM_DATABASE_COMMAND_TIMEOUT" \
    "$DBU/usr/local/bin/python3" updatedb.py \
      --host "127.0.0.1:${DUNE_PG_PORT:-15432}" \
      --project-database dune --project-user dune --project-password dune \
      --admin-user postgres --admin-password postgres \
      --unattended --local-as-remote --no-backup \
      --postgres-installation "$PG/usr/local" \
      --schema-path "$DBU/root/DuneSandbox/Database" \
      2>&1 | sed 's/^/[migrate] /' | tee "$MIGRATE_OUT"
  # PIPESTATUS[0] here is updatedb.py's real exit. Reading it in the
  # parent after the subshell would instead yield the subshell's own
  # exit (= tee/sed = always 0), so exit the subshell with it explicitly.
  exit ${PIPESTATUS[0]}
)
RC=$?
kill "$HEARTBEAT_PID" 2>/dev/null
wait "$HEARTBEAT_PID" 2>/dev/null || true
trap - EXIT
MIGRATE_ELAPSED=$(( $(date +%s) - MIGRATE_STARTED ))
log "updatedb.py finished after ${MIGRATE_ELAPSED}s (exit=${RC})"
set -e

case $RC in
  0)
    # DUNE-FUNCOM-MIGRATE-FALSEOK - updatedb.py has been observed to log a
    # fatal error (e.g. a failed pg_dump schema-diff) yet still exit 0.
    # Treat any failure marker in the captured output as a hard failure.
    if [ -f "$MIGRATE_OUT" ] && \
       grep -qE '\[ERROR\]\[app\]|pg_dump: error:|Contact #help-engine-and-editor' "$MIGRATE_OUT"; then
      warn "updatedb.py exited 0 but its output contains failure markers:"
      grep -E '\[ERROR\]\[app\]|pg_dump: error:|Contact #help-engine-and-editor' "$MIGRATE_OUT" | sed 's/^/  /'
      die "Aborting: migration reported success but logged errors - refusing to start against a possibly-stale schema."
    fi
    log "DB migrations complete: success"

    # Upstream inserts the DLC world_partition rows here. We do NOT: this
    # fork seeds every partition in prestart.sh step 6, from a single list
    # whose ids are checked against Funcom's world-template.yaml. Keeping a
    # second copy here would be duplicated state with two chances to drift.
    #
    # What upstream has that prestart does not is the sequence reset, which
    # is a separate concern and worth keeping: prestart inserts explicit
    # partition_ids, so the serial's high-water mark lags behind and any
    # later auto-generated id would collide. prestart runs before this, so
    # resetting here sees the final row set.
    log "Resetting world_partition id sequence high-water mark..."
    LD_LIBRARY_PATH="$LD" "$PG/usr/local/bin/psql" \
      -h 127.0.0.1 -p "${DUNE_PG_PORT:-15432}" -U postgres -d dune -tA <<'DUNE_WP_SQL' 2>&1 | sed 's/^/[migrate] /'
SELECT setval(pg_get_serial_sequence('dune.world_partition', 'partition_id'),
              GREATEST((SELECT COALESCE(max(partition_id), 0) FROM dune.world_partition), 1));
DUNE_WP_SQL
    ;;
  9)
    warn "DB has patches the current build doesn't recognise."
    warn "This is the downgrade scenario — Funcom does not support rolling back schema."
    warn "Either roll forward to a newer build, or wipe the database (state/pg/data) to start fresh."
    die "Aborting: refusing to start services against unsupported-downgrade DB state."
    ;;
  6|7|8)
    warn "DB migration completed with structural issues (exit $RC) — see [migrate] lines above"
    warn "Common causes: corrupt or hand-edited patch SQL, malformed patch filename in Database/Upgrade/"
    die "Aborting: schema differences require manual review before services may start."
    ;;
  1)
    die "Couldn't reach postgres at 127.0.0.1:${DUNE_PG_PORT:-15432} — Start Postgres stage must run first."
    ;;
  *)
    die "updatedb.py exited $RC (unrecognised) — refusing to proceed; check [migrate] lines above for context."
    ;;
esac
