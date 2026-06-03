#!/usr/bin/env python3
"""Live-map helpers (Phase 2): map-key validation + marker CSV parsing.

The position query itself lives in admin-publish.sh (the only layer with DB
access); this pure module is shared with admin-http for input validation and
for parsing the CSV that the map-markers subcommand emits. No DB / network.
"""
import csv
import io

# Maps the live map supports. These are the canonical dune.actors.map values
# AND the keys the SPA has background images + projection bounds for.
MAP_KEYS = ("HaggaBasin", "DeepDesert", "Arrakeen", "HarkoVillage")


def validate_map_key(key) -> bool:
    """True only for a supported map key (rejects empty / unknown / non-str)."""
    return isinstance(key, str) and key in MAP_KEYS


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key, "") or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key, "") or 0))
    except (TypeError, ValueError):
        return 0


def parse_markers(csv_text: str) -> list:
    """Parse the map-markers CSV (header + rows) into a list of marker dicts.

    Expected header: id,name,online,partition,fls,x,y,z. x/y/z coerce to float,
    partition to int (0 on garbage), online to bool from the Funcom status text.
    Uses csv.reader so a quoted name containing a comma round-trips correctly.
    """
    rows = list(csv.reader(io.StringIO(csv_text or "")))
    if len(rows) < 2:
        return []
    headers = rows[0]
    out = []
    for cols in rows[1:]:
        if not cols or not any(c.strip() for c in cols):
            continue
        row = dict(zip(headers, cols))
        # A null transform.location yields blank x/y cells; skip so we never
        # plot a phantom marker at world origin. A real 0 coord arrives as "0".
        if not (row.get("x", "").strip() and row.get("y", "").strip()):
            continue
        out.append({
            "id": row.get("id", ""),
            "name": (row.get("name") or "").strip() or "Unknown",
            "online": (row.get("online", "") or "").strip().lower() == "online",
            "partition": _i(row, "partition"),
            "fls": row.get("fls", ""),
            "x": _f(row, "x"),
            "y": _f(row, "y"),
            "z": _f(row, "z"),
            "kind": "player",
        })
    return out
