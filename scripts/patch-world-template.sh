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
# Each row: map|dim_count|dim_base|label|warm_partition_id
DESTS_SPEC=""
while IFS='|' read -r map count base label warm; do
  [ -z "$map" ] && continue
  [ "$count" -le 0 ] && continue
  DESTS_SPEC+="${map}|${count}|${base}|${label}|${warm}"$'\n'
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

# Parse destinations spec into a {map_name: {"warm": int, "dims": [(pid, dim), ...]}} dict.
extras: dict[str, dict] = {}
for line in spec.strip().splitlines():
    parts = line.split("|")
    if len(parts) < 5:
        continue
    map_name = parts[0]
    count = int(parts[1])
    base = int(parts[2])
    warm = int(parts[4])
    extras[map_name] = {
        "warm": warm,
        "dims": [(base + i, i + 1) for i in range(count)],
    }

with open(path) as f:
    lines = f.readlines()

def fmt_entry(indent: str, pid: int, dim: int) -> list[str]:
    """One partition entry. `indent` is the spaces before the leading dash."""
    field = indent + "  "
    return [
        f"{indent}- dimension: {dim}\n",
        f"{field}disable: false\n",
        f"{field}id: {pid}\n",
        f"{field}maxX: 1\n",
        f"{field}maxY: 1\n",
        f"{field}minX: 0\n",
        f"{field}minY: 0\n",
    ]

# Default indents used when synthesising a new map block — match the
# 12/14/16-space pattern Funcom uses in their existing world-template.
DEFAULT_MAP_INDENT = " " * 12       # before `- map:`
DEFAULT_FIELD_INDENT = " " * 14     # before `partitions:` and `- dimension:`

out: list[str] = []
i = 0
inserted: set[str] = set()
# Track indent of the last `- map:` line we encountered so we can re-use
# it when synthesising a missing block (and so the appended block lands
# at the right depth even if Funcom shifts the indentation in a future
# depot update).
observed_map_indent: str | None = None
# Index in `out` where the last successfully-injected destination block
# ends. Missing-destination synthesis appends right after this point so
# every injection stays inside the worldPartitions list.
last_dest_end_in_out: int | None = None

while i < len(lines):
    line = lines[i]
    out.append(line)
    stripped = line.lstrip()

    matched_map: str | None = None
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

    # Capture the worldPartitions indent from the matched line. This
    # only fires on lines that are confirmed worldPartitions entries
    # (the same destination map name often appears in messageQueues
    # at a different indent — we must not pick that one up).
    observed_map_indent = line[: len(line) - len(stripped)]

    # Walk through: partitions: + 7 fields of existing dim:0 block.
    j = i + 1
    if j < len(lines) and "partitions:" in lines[j]:
        out.append(lines[j])
        j += 1
    # The dim entry indent is whatever Funcom's YAML uses. Detect from
    # the line after `partitions:` (the `- dimension: 0` line).
    dim_indent = DEFAULT_FIELD_INDENT
    if j < len(lines):
        peek_stripped = lines[j].lstrip()
        if peek_stripped.startswith("- dimension"):
            dim_indent = lines[j][: len(lines[j]) - len(peek_stripped)]
    for _ in range(7):
        if j < len(lines):
            out.append(lines[j])
            j += 1
    # Inject our dimensional entries
    if extras[matched_map]["dims"]:
        for pid, dim in extras[matched_map]["dims"]:
            out.extend(fmt_entry(dim_indent, pid, dim))
    inserted.add(matched_map)
    last_dest_end_in_out = len(out)
    i = j

# Synthesise complete map blocks for destinations Funcom's YAML doesn't
# ship (SH_FallenLight, primarily). The block needs to land inside the
# worldPartitions list at the same indent as the existing `- map:`
# siblings — NOT inside one of them.
missing = [m for m in extras if m not in inserted]
synth_blocks: list[str] = []
if missing and observed_map_indent is not None:
    map_indent = observed_map_indent
    field_indent = map_indent + "  "    # `partitions:` indent (one deeper)
    dim_indent = field_indent             # `- dimension:` sits at the same depth as `partitions:`

    for map_name in missing:
        spec_for_map = extras[map_name]
        synth_blocks.append(f"{map_indent}- map: {map_name}\n")
        synth_blocks.append(f"{field_indent}partitions:\n")
        synth_blocks.extend(fmt_entry(dim_indent, spec_for_map["warm"], 0))
        for pid, dim in spec_for_map["dims"]:
            synth_blocks.extend(fmt_entry(dim_indent, pid, dim))
elif missing:
    # No reference indent because Funcom's YAML had no `- map:` lines at
    # all — that means our assumptions are wrong about this depot version.
    # Skip synthesis and warn loudly instead of corrupting the file.
    sys.stderr.write(
        f"patch-world-template: no `- map:` reference indent in YAML — "
        f"cannot synthesise missing maps: {', '.join(missing)}\n"
    )

if synth_blocks:
    # Find the END of the worldPartitions list — the first line AFTER
    # our last injected destination that pops to indent < map_indent
    # (= we exited the list) AND isn't whitespace/comment. Walking
    # forward from `last_dest_end_in_out` instead of backwards from EOF
    # because anchoring on a known-in-list position is more reliable
    # than guessing from EOF (the YAML may have siblings of
    # worldPartitions at the same indent — messageQueues lists maps
    # too).
    list_indent_len = len(map_indent)
    insert_at = len(out)
    anchor = last_dest_end_in_out if last_dest_end_in_out is not None else 0
    for idx in range(anchor, len(out)):
        ln = out[idx]
        s = ln.lstrip()
        if not s or s.startswith("#"):
            continue
        cur_indent = len(ln) - len(s)
        if cur_indent < list_indent_len:
            insert_at = idx
            break
        if cur_indent == list_indent_len and not s.startswith("- "):
            # Same indent but not a list item dash — we've exited the
            # list into a sibling key (rare for YAML, but valid).
            insert_at = idx
            break

    insert_at = min(insert_at, len(out))
    out[insert_at:insert_at] = synth_blocks

# Marker so subsequent runs skip the patch.
out.append("# dim-injected: marker line — do not remove\n")

with open(path, "w") as f:
    f.writelines(out)

injected_dims = sum(len(extras[m]["dims"]) for m in inserted)
synth_dims = sum(len(extras[m]["dims"]) for m in missing)
print(f"injected {injected_dims} dim entries into existing map blocks: {', '.join(sorted(inserted)) or '(none)'}")
if missing:
    print(f"synthesised {len(missing)} complete map block(s) ({synth_dims} dims + {len(missing)} warm rows): {', '.join(missing)}")
PYEOF

log "world-template.yaml patched: success"
