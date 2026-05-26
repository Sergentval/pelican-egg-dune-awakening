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

set +e
(
  cd "$DBU/root/PSQL" && \
  PYTHONPATH=. LD_LIBRARY_PATH="$LD" \
    "$DBU/usr/local/bin/python3" updatedb.py \
      --host "127.0.0.1:${DUNE_PG_PORT:-15432}" \
      --project-database dune --project-user dune --project-password dune \
      --admin-user postgres --admin-password postgres \
      --unattended --local-as-remote --no-backup \
      --postgres-installation "$PG/usr/local" \
      --schema-path "$DBU/root/DuneSandbox/Database" \
      2>&1 | sed 's/^/[migrate] /'
)
RC=${PIPESTATUS[0]}
set -e

case $RC in
  0)
    log "DB migrations complete: success"
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
