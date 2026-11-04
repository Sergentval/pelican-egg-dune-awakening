#!/bin/bash
# Regression harness for the `db-sql` guard in scripts/admin-publish.sh.
#
#   bash scripts/tests/test-db-sql-guard.sh
#
# db-sql pins the psql session read-only, then runs the query with -c. psql
# sends a -c string as ONE simple query, so every statement in it runs: a
# COMMIT inside the query ends the read-only transaction and what follows can
# write. The guard must refuse a stacked query before psql is ever reached.
#
# The base dir has no Postgres in it on purpose: a query the guard lets
# through fails on the missing psql binary, which is how the "accepted" cases
# are told apart from the refused ones (exit 2 plus the guard's message).

set -u

HERE="$(dirname "$(readlink -f "$0")")"
PUBLISH="$(cd "$HERE/.." && pwd)/admin-publish.sh"
BASE="$(mktemp -d)"
trap 'rm -rf "$BASE"' EXIT

pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }

run() { DUNE_BASE_DIR="$BASE" bash "$PUBLISH" db-sql "$1" 2>&1; }

refused() {
  local q=$1 out rc
  out=$(run "$q"); rc=$?
  if [ "$rc" = 2 ] && [[ "$out" == db-sql:* ]]; then
    ok "refused: $q"
  else
    bad "refused: $q" "rc=$rc out=$out"
  fi
}

accepted() {
  local q=$1 out
  out=$(run "$q")
  if [[ "$out" == db-sql:* ]]; then
    bad "accepted: $q" "the guard refused it: $out"
  else
    ok "accepted: $q"
  fi
}

refused "show x; commit; set session characteristics as transaction read write; delete from dune.actors"
refused "select 1; delete from dune.actors"
refused "select 1 -- ; a ';' in a comment is refused too"
refused ";;"
refused "   "

accepted "select 1"
accepted "select 1;"
accepted $'select 1 ;\n  '

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
