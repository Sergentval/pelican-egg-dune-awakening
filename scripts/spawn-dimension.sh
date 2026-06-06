#!/bin/bash
# © 2026 Sergent_val. MIT — see ATTRIBUTION.md.
#
# spawn-dimension.sh BASE_DIR PARTITION_ID
#
# Spawn ONE dimensional UE5 partition (dimension_index>0) at its CANONICAL port
# and write world_partition.server_id back once it registers in farm_state. The
# runtime, per-partition counterpart of start-ue5-dimensions.sh (which scans all
# server_id-IS-NULL rows and assigns ports POSITIONALLY by rank-among-NULL-rows).
#
# Why a separate single-partition spawner: the bulk scanner gives the first NULL
# row offset 0 -> port 7790, so respawning one dim while others are live would
# collide on the UDP bind. Here the port is the partition's CANONICAL slot:
# offset = number of dim rows that sort before it by (map, dimension_index) —
# exactly the slot cold-boot would assign — so it never collides with the other
# (still-live) dims. Used by admin-publish 'dimension-up'. Blocks for minutes
# (UE5 UDP bind + farm_state registration), so callers background it.
set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-}}"
PART_ID="${2:-}"
export SOURCE="spawn-dimension"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

case "$PART_ID" in ''|*[!0-9]*) die "usage: spawn-dimension.sh BASE PARTITION_ID (numeric)" ;; esac

DIM_GAME_PORT_BASE="${DUNE_DIM_GAME_PORT_BASE:-7790}"
DIM_IGW_PORT_BASE="${DUNE_DIM_IGW_PORT_BASE:-7960}"
PG_BIN="$(rootfs postgres)/usr/local/bin"
PSQL_ENV=(
  env -i HOME=/tmp LC_ALL=C
  LD_LIBRARY_PATH="$(ldlib postgres)"
  ICU_DATA="$(icu_dir postgres)"
)
psql_q() { "${PSQL_ENV[@]}" "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune -tA "$@"; }

wait_for_port 127.0.0.1 "$DUNE_PG_PORT" 30 || die "postgres unreachable on 127.0.0.1:$DUNE_PG_PORT"

# Resolve the row — must be a dim that is currently OFFLINE (server_id IS NULL).
ROW="$(psql_q -F'|' -c "SELECT map, dimension_index, COALESCE(label,'') FROM dune.world_partition WHERE partition_id=$PART_ID AND dimension_index>0 AND server_id IS NULL")"
[ -n "$ROW" ] || die "partition $PART_ID is not an offline dimensional partition (need dimension_index>0 AND server_id IS NULL)"
IFS='|' read -r MAP DIM LABEL <<<"$ROW"

# Canonical port offset = rank of this row among all dim rows by (map, dimension_index).
OFFSET="$(psql_q -c "SELECT count(*) FROM dune.world_partition WHERE dimension_index>0 AND (map < '$MAP' OR (map='$MAP' AND dimension_index < $DIM))")"
GAME_PORT=$((DIM_GAME_PORT_BASE + OFFSET))
IGW_PORT=$((DIM_IGW_PORT_BASE + OFFSET))
SUFFIX="dim${DIM}-p${PART_ID}"
log "spawning $MAP partition=$PART_ID dim=$DIM offset=$OFFSET port=$GAME_PORT igw=$IGW_PORT"

DUNE_PARTITION="$PART_ID" \
DUNE_GAME_PORT="$GAME_PORT" \
DUNE_IGW_PORT="$IGW_PORT" \
DUNE_INSTANCE_DISPLAY_NAME="$LABEL" \
  bash "$SCRIPTS/start-ue5.sh" "$BASE" "$MAP" "$SUFFIX" || die "spawn failed for $SUFFIX"

# Poll farm_state for this game_port to register ready (UE5 needs 30-90s post-bind).
DEADLINE=$(( $(date +%s) + 240 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  R="$(psql_q -c "SELECT count(*) FROM dune.farm_state WHERE map='$MAP' AND game_port=$GAME_PORT AND ready=true")"
  [ "${R:-0}" -ge 1 ] && break
  sleep 2   # tight poll: notice 'ready' ASAP (cuts wake quantization); the 240s wall-clock DEADLINE above is preserved
done

# Writeback (guarded by server_id IS NULL so a concurrent run can't double-bind).
psql_q -c "UPDATE dune.world_partition
           SET server_id=(SELECT server_id FROM dune.farm_state WHERE map='$MAP' AND game_port=$GAME_PORT LIMIT 1)
           WHERE partition_id=$PART_ID AND server_id IS NULL" >/dev/null 2>&1 || warn "writeback failed for $PART_ID"
log "dimension $PART_ID spawn complete"
