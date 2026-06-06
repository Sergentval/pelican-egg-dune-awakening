#!/bin/bash
# © 2026 Sergent_val. MIT — see ATTRIBUTION.md.
#
# start-ue5-dimensions.sh BASE_DIR
#
# Spawns a UE5 partition instance for every world_partition row with
# dimension_index > 0 that doesn't have a live server_id, then updates
# the row's server_id to point at the newly-spawned UE5's farm_state
# entry.
#
# Why this exists:
#   The Funcom Battlegroup Director's DimensionServerGroup allocator
#   (BattlegroupDirector.Travel.DimensionServerGroup) picks a destination
#   partition by `dimension_index` when handling cross-Sietch travel
#   (Overland_to_DeepDesert1_S{1,2,3}, Overland_to_Arrakeen, etc.).
#   Without world_partition rows for dimension_index>0, the picker has
#   nothing to pick — TravelResponse.DestinationPartitionId stays null
#   and the client sees Code 17 (DestinationTemporarilyUnavailable).
#
#   prestart.sh seeds the dimensional rows in the DB. This script picks
#   up those rows once the supervisor has the warm partitions running
#   and the s2s mesh is stable, then materializes them as real UE5
#   processes.
#
# Design:
#   - Idempotent. Re-running is a no-op for already-mapped rows.
#   - Picks ports above the warm-map range (warm uses 7777-7781 + igw
#     7950-7954). Dimensional instances start at 7790/7960 to leave room.
#   - Same `start-ue5.sh` machinery as the warm spawns; the only
#     difference is the SUFFIX (`dim1`, `dim2`, ...) and the inline
#     DUNE_PARTITION/PORT env vars.
#   - The post-spawn DB UPDATE has to wait for farm_state to populate
#     (UE5 takes 30-90s to register). We poll the farm_state row for
#     the chosen game_port before issuing the UPDATE.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-}}"
export SOURCE="start-ue5-dimensions"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

# Port allocation strategy:
#   Game ports 7790-7820 reserved for dimensional partitions (room for
#   ~30 dim instances total across all warm maps). IGW ports mirror at
#   7960-7990. The pool.go inside mock-k8s allocates the warm range
#   starting at K8S_POOL_GAME_PORT_BASE (default 7900) — we deliberately
#   stay below that so we don't collide with future on-demand cap maps.
DIM_GAME_PORT_BASE="${DUNE_DIM_GAME_PORT_BASE:-7790}"
DIM_IGW_PORT_BASE="${DUNE_DIM_IGW_PORT_BASE:-7960}"

PG_BIN="$(rootfs postgres)/usr/local/bin"
PSQL_ENV=(
  env -i HOME=/tmp LC_ALL=C
  LD_LIBRARY_PATH="$(ldlib postgres)"
  ICU_DATA="$(icu_dir postgres)"
)

# Wait for postgres to be reachable (start-pg.sh already ran but let's
# be defensive — this script is called late in the boot sequence).
if ! wait_for_port 127.0.0.1 "$DUNE_PG_PORT" 30; then
  die "postgres not reachable on 127.0.0.1:$DUNE_PG_PORT"
fi

# Get the list of (partition_id, map, dimension_index) rows that have
# dimension_index>0 and no server_id assigned. These are what we need
# to spawn.
mapfile -t ROWS < <(
  "${PSQL_ENV[@]}" "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune -tA -F'|' <<'SQL'
SELECT partition_id, map, dimension_index, COALESCE(label, '')
FROM dune.world_partition
WHERE dimension_index > 0
  AND server_id IS NULL
ORDER BY map, dimension_index;
SQL
)

if [ "${#ROWS[@]}" -eq 0 ]; then
  log "no dimensional partitions to spawn — done."
  exit 0
fi

log "found ${#ROWS[@]} dimensional partition(s) to spawn"

# Sequential spawn with staggering. Parallel spawning overwhelms RMQ
# (TLS handshake on 5673 chokes after ~3 simultaneous UE5 boots — observed
# May 29 with all 3 DD dim partitions starting in the same second). 20s
# between spawns is enough for the previous UE5's BGD subsystem to bind
# its RMQ queue before the next one starts negotiating. We don't need
# the previous one to be FULLY ready, just past its initial RMQ
# connection burst.
SPAWN_STAGGER_SEC="${DUNE_DIM_SPAWN_STAGGER_SEC:-20}"

# PARKED sietches (server/state/admin/parked-sietches.json) must NOT auto-respawn at boot.
# Read the set ONCE; the loop still consumes each row's port slot (so non-parked dims keep
# their canonical ports) but SKIPS the spawn for a parked id, so it stays offline + its
# data preserved until an explicit unpark. Fail-safe: a read error yields an empty set, so
# everything spawns — we never silently skip (and thus kill) a live sietch.
PARKED_CSV="$("${DUNE_PYTHON3:-python3}" "$SCRIPTS/admin_park.py" csv "$BASE" 2>/dev/null || true)"

PORT_OFFSET=0
for row in "${ROWS[@]}"; do
  [ -z "$row" ] && continue
  IFS='|' read -r PART_ID MAP DIM LABEL <<<"$row"
  GAME_PORT=$((DIM_GAME_PORT_BASE + PORT_OFFSET))
  IGW_PORT=$((DIM_IGW_PORT_BASE + PORT_OFFSET))
  PORT_OFFSET=$((PORT_OFFSET + 1))

  # Reserve the port slot above, but do NOT spawn a parked sietch (it stays offline across
  # reboot with its data intact; unpark respawns it at this same canonical slot).
  case ",$PARKED_CSV," in
    *",$PART_ID,"*) log "  skip PARKED sietch partition=$PART_ID dim=$DIM (paused, data preserved)"; continue ;;
  esac

  SUFFIX="dim${DIM}-p${PART_ID}"
  log "  spawning $MAP partition=$PART_ID dim=$DIM port=$GAME_PORT igw=$IGW_PORT suffix=$SUFFIX"

  # start-ue5.sh accepts DUNE_PARTITION/DUNE_GAME_PORT/DUNE_IGW_PORT
  # inline (it explicitly skips reading $RUNTIME/ue5-$MAP.env when
  # DUNE_PARTITION is already set in the env). It writes its own pid
  # file as ue5-$MAP-$SUFFIX.pid and starts the process under launch_bg.
  # start-ue5.sh blocks until UE5 binds UDP (180s timeout), so we
  # don't background it — the sequential gating IS the stagger.
  DUNE_PARTITION="$PART_ID" \
  DUNE_GAME_PORT="$GAME_PORT" \
  DUNE_IGW_PORT="$IGW_PORT" \
  DUNE_INSTANCE_DISPLAY_NAME="$LABEL" \
    bash "$SCRIPTS/start-ue5.sh" "$BASE" "$MAP" "$SUFFIX" || warn "  spawn failed for $SUFFIX"

  # Extra cushion between spawns. UDP-bind doesn't mean RMQ-ready.
  sleep "$SPAWN_STAGGER_SEC"
done

log "all dimensional spawns started — waiting for farm_state registration"

# Poll farm_state until every dimensional instance has registered. UE5
# takes 30-90s to bind UDP, finish persistence init, and POST its
# state. We give it 240s budget.
DEADLINE=$(( $(date +%s) + 240 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  PENDING=$(
    "${PSQL_ENV[@]}" "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune -tAc \
      "SELECT count(*) FROM dune.farm_state WHERE game_port BETWEEN $DIM_GAME_PORT_BASE AND $((DIM_GAME_PORT_BASE + ${#ROWS[@]} - 1)) AND ready = true"
  )
  if [ "$PENDING" -ge "${#ROWS[@]}" ]; then
    log "all ${#ROWS[@]} dimensional partitions ready in farm_state"
    break
  fi
  sleep 2   # tight poll: notice 'ready' ASAP (cuts wake quantization); the 240s wall-clock DEADLINE above is preserved
done

# Map farm_state.server_id back to world_partition. Match by
# (map, game_port) — each dimensional UE5 we launched got a unique
# game_port in our DIM_GAME_PORT_BASE+offset range, so we walk the
# rows in the same order we spawned them.
log "updating world_partition.server_id for dimensional partitions"
PORT_OFFSET=0
for row in "${ROWS[@]}"; do
  [ -z "$row" ] && continue
  IFS='|' read -r PART_ID MAP DIM <<<"$row"
  GAME_PORT=$((DIM_GAME_PORT_BASE + PORT_OFFSET))
  PORT_OFFSET=$((PORT_OFFSET + 1))
  "${PSQL_ENV[@]}" "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune -c \
    "UPDATE dune.world_partition
     SET server_id = (SELECT server_id FROM dune.farm_state
                      WHERE map='$MAP' AND game_port=$GAME_PORT LIMIT 1)
     WHERE partition_id=$PART_ID
       AND server_id IS NULL" 2>/dev/null || warn "  UPDATE failed for partition $PART_ID"
done

log "dimensional partition setup complete: success"
