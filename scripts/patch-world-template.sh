#!/bin/bash
# Inject dimensional partition entries into world-template.yaml.
#
# Funcom ships the template with one partition per map (dimension: 0).
# For cross-Sietch travel allocation, Director needs partitions with
# dimension_index > 0 to exist in BOTH:
#   - the postgres world_partition table (prestart.sh seeds these)
#   - the BattleGroup CR's worldPartitions list (mock-k8s loads this
#     from world-template.yaml at boot)
#
# Without the YAML entries, mock-k8s's BG CR only exposes dim:0 for
# each map. Director's brown-check (Battlegroup.cs L196-225) reads the
# CR and never finds the dimensional partition IDs we seeded in the DB,
# so it never lets travel proceed to them (without our
# IGNORE_IGWO_API_SERVER_CHECK env var).
#
# We can't ship a fork of world-template.yaml because install.sh
# re-extracts the OCI tar on every fresh container — the file would
# get clobbered. Instead, patch in place every boot. Idempotent.
#
# Pulls dim counts from multi-sietch-config.sh. To change them,
# operator sets DUNE_DD_DIMENSIONS / DUNE_ARRAKEEN_DIMENSIONS / etc
# via the egg JSON panel and restarts the container.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-}}"
export SOURCE="patch-world-template"
SCRIPTS_DIR="$(dirname "$(readlink -f "$0")")"
source "$SCRIPTS_DIR/lib.sh" "$BASE"
source "$SCRIPTS_DIR/multi-sietch-config.sh"

TEMPLATE="$BASE/server/scripts/setup/templates/world-template.yaml"
[ -f "$TEMPLATE" ] || die "world-template.yaml missing at $TEMPLATE"

# Skip if our marker is already there.
if grep -q "# dim-injected" "$TEMPLATE" 2>/dev/null; then
  log "world-template.yaml already patched — skipping"
  exit 0
fi

# Serialize the destinations spec for python via env vars.
DESTS_SPEC=""
while IFS='|' read -r map count base label; do
  [ -z "$map" ] && continue
  [ "$count" -le 0 ] && continue
  DESTS_SPEC+="${map}|${count}|${base}|${label}"$'\n'
done < <(multi_sietch_destinations)

if [ -z "$DESTS_SPEC" ]; then
  log "no dimensional destinations enabled — leaving template untouched"
  exit 0
fi

log "patching $TEMPLATE with dimensional partitions for: $(awk -F'|' '{print $1}' <<<"$DESTS_SPEC" | xargs)"

DESTS_SPEC="$DESTS_SPEC" python3 - "$TEMPLATE" <<'PYEOF'
import os
import sys

path = sys.argv[1]
spec = os.environ.get("DESTS_SPEC", "")

# Parse destinations spec into a {map_name: [(pid, dim), ...]} dict.
extras: dict[str, list[tuple[int, int]]] = {}
for line in spec.strip().splitlines():
    parts = line.split("|")
    if len(parts) < 4:
        continue
    map_name, count, base, _label = parts[0], int(parts[1]), int(parts[2]), parts[3]
    extras[map_name] = [(base + i, i + 1) for i in range(count)]

with open(path) as f:
    lines = f.readlines()

def fmt_entry(pid: int, dim: int) -> list[str]:
    return [
        f"              - dimension: {dim}\n",
        f"                disable: false\n",
        f"                id: {pid}\n",
        f"                maxX: 1\n",
        f"                maxY: 1\n",
        f"                minX: 0\n",
        f"                minY: 0\n",
    ]

out: list[str] = []
i = 0
inserted: set[str] = set()
while i < len(lines):
    line = lines[i]
    out.append(line)
    matched_map: str | None = None
    stripped = line.lstrip()
    for map_name in extras:
        # The YAML lists each map like:  "            - map: <name>"
        # Only match the worldPartitions block, not the messageQueues
        # block which uses "          map: <name>" with different indent.
        if stripped.startswith(f"- map: {map_name}") and map_name not in inserted:
            matched_map = map_name
            break
    if matched_map is None:
        i += 1
        continue

    # Walk through: partitions: + 7 fields of existing dim:0 block.
    j = i + 1
    if j < len(lines) and "partitions:" in lines[j]:
        out.append(lines[j])
        j += 1
    for _ in range(7):
        if j < len(lines):
            out.append(lines[j])
            j += 1
    # Inject our dimensional entries
    for pid, dim in extras[matched_map]:
        out.extend(fmt_entry(pid, dim))
    inserted.add(matched_map)
    i = j

# Marker so subsequent runs skip the patch.
out.append("# dim-injected: marker line — do not remove\n")

with open(path, "w") as f:
    f.writelines(out)

added = sum(len(v) for v in extras.values())
print(f"injected {added} dimensional partition entries into {path}")
PYEOF

log "world-template.yaml patched: success"
