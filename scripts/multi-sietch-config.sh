#!/bin/bash
# Multi-Sietch configuration — single source of truth for cross-Sietch
# travel destinations.
#
# Source this from scripts that need to know:
#   - which destination maps to enable
#   - how many dimensional (per-player tunnel) partitions each one gets
#   - what partition_id range each destination owns
#
# Each Sietch destination needs:
#   - one always-warm partition (dimension_index = 0) — the shared
#     landing zone. seeded by prestart.sh, spawned by mock-k8s
#     AlwaysWarmMaps.
#   - N dimensional partitions (dimension_index = 1..N) — per-player
#     sandstorm tunnels. seeded here, spawned by
#     start-ue5-dimensions.sh.
#
# Partition_id ranges per destination (each owns 10 ids):
#   100-110 : DeepDesert_1   dimensions
#   110-120 : SH_Arrakeen    dimensions
#   120-130 : SH_HarkoVillage dimensions
#   130-140 : SH_FallenLight dimensions  (opt-in via env)
#
# Note: the warm partition keeps its original id (8, 3, 4, etc).
# We start dimensional ids at base+1 to leave room for future hot
# additions (e.g. partition 100 reserved for a hypothetical DD lobby).
#
# Three env vars control how many dimensional partitions per map.
# Setting a value to 0 disables travel to that destination entirely
# (the warm partition still runs because of AlwaysWarmMaps; players
# can only reach it via Login flow, not sandstorm tunnel).
#
#   DUNE_DD_DIMENSIONS         (default 3)
#   DUNE_ARRAKEEN_DIMENSIONS   (default 0 — Arrakeen travel untested)
#   DUNE_HARKO_DIMENSIONS      (default 0 — Harko travel untested)
#   DUNE_FALLEN_LIGHT_DIMENSIONS (default 0 — opt-in 3rd city)
#
# To enable SH_FallenLight at all (warm + dims), also set:
#   DUNE_ENABLE_FALLEN_LIGHT=true
#
# This file is sourced — don't make it executable.

# Defaults. Override via the egg JSON / Pelican panel env var settings.
: "${DUNE_DD_DIMENSIONS:=3}"
: "${DUNE_ARRAKEEN_DIMENSIONS:=0}"
: "${DUNE_HARKO_DIMENSIONS:=0}"
: "${DUNE_FALLEN_LIGHT_DIMENSIONS:=0}"
: "${DUNE_ENABLE_FALLEN_LIGHT:=false}"

# Per-destination metadata. Each entry: map_name|dim_count|partition_id_base|label_base
# Order matters — affects port allocation in start-ue5-dimensions.sh.
multi_sietch_destinations() {
  echo "DeepDesert_1|${DUNE_DD_DIMENSIONS}|101|Deep Desert"
  echo "SH_Arrakeen|${DUNE_ARRAKEEN_DIMENSIONS}|111|Arrakeen"
  echo "SH_HarkoVillage|${DUNE_HARKO_DIMENSIONS}|121|Harko Village"
  if [ "${DUNE_ENABLE_FALLEN_LIGHT}" = "true" ]; then
    echo "SH_FallenLight|${DUNE_FALLEN_LIGHT_DIMENSIONS}|131|Fallen Light"
  fi
}

# Emit a SQL INSERT row for one dimensional partition.
# Usage: multi_sietch_partition_row <map> <pid> <dim> <label>
multi_sietch_partition_row() {
  local map="$1" pid="$2" dim="$3" label="$4"
  printf '  (%s, %s, %s, %s, false, %s)' \
    "$pid" \
    "'$map'" \
    "'{\"box\":{\"max_x\":1,\"max_y\":1,\"min_x\":0,\"min_y\":0},\"type\":\"box2d_array\"}'::jsonb" \
    "$dim" \
    "'$label D${dim}'"
}

# Emit the full SQL VALUES list for all dimensional partitions. Empty
# if no destinations have dims enabled.
multi_sietch_partition_values() {
  local first=1
  while IFS='|' read -r map count base label; do
    [ -z "$map" ] && continue
    [ "$count" -le 0 ] && continue
    for d in $(seq 1 "$count"); do
      local pid=$((base + d - 1))
      [ "$first" -eq 1 ] || printf ',\n'
      multi_sietch_partition_row "$map" "$pid" "$d" "$label"
      first=0
    done
  done < <(multi_sietch_destinations)
  echo
}
