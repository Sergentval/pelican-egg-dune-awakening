#!/usr/bin/env python3
"""Operator teleport locations store (Phase 3).

A small JSON store of named map coordinates (e.g. "Hagga safe outpost") used for
one-click teleport shortcuts. Operator-created mutable data, so it lives in the
state dir (persists across restarts; not reset by an egg reinstall), mirroring
the welcome ledger. Pure file I/O + validation; the teleport itself runs through
admin-publish.sh tpsafe (admin-http resolves a location then calls it).
"""
import json
import os

from admin_map import MAP_KEYS  # reuse the supported-map whitelist

MAX_LOCATIONS = 500  # sane cap so an authenticated caller can't grow the file unbounded


def store_path(base: str) -> str:
    return os.path.join(base, "server", "state", "map-locations.json")


def load_locations(base: str) -> list:
    """Return the saved locations (empty list if the store is absent/corrupt)."""
    try:
        with open(store_path(base), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    locs = data.get("locations") if isinstance(data, dict) else data
    return [l for l in locs if isinstance(l, dict)] if isinstance(locs, list) else []


def save_locations(base: str, locations: list) -> None:
    path = store_path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"locations": locations}, f, indent=2)
    os.replace(tmp, path)  # atomic


def normalize_location(raw) -> tuple:
    """Validate + coerce one location. Returns (loc_dict, None) or (None, error)."""
    if not isinstance(raw, dict):
        return None, "location must be an object"
    name = str(raw.get("name", "")).strip()
    if not name or len(name) > 60:
        return None, "name required (1-60 chars)"
    mp = str(raw.get("map", "")).strip()
    if mp not in MAP_KEYS:
        return None, "map must be one of " + "|".join(MAP_KEYS)
    try:
        x, y, z = float(raw["x"]), float(raw["y"]), float(raw["z"])
    except (KeyError, TypeError, ValueError):
        return None, "x, y, z must be numbers"
    return {"name": name, "map": mp, "x": x, "y": y, "z": z}, None


def upsert_location(base: str, raw) -> tuple:
    """Add or replace a location (keyed on name, case-insensitive). Returns
    (locations, None) or (None, error)."""
    loc, err = normalize_location(raw)
    if err:
        return None, err
    key = loc["name"].lower()
    locs = [l for l in load_locations(base) if str(l.get("name", "")).strip().lower() != key]
    if len(locs) >= MAX_LOCATIONS:
        return None, f"too many locations (max {MAX_LOCATIONS}); remove some first"
    locs.append(loc)
    locs.sort(key=lambda l: (l.get("map", ""), l.get("name", "")))
    save_locations(base, locs)
    return locs, None


def remove_location(base: str, name: str) -> list:
    key = str(name or "").strip().lower()
    locs = [l for l in load_locations(base) if str(l.get("name", "")).strip().lower() != key]
    save_locations(base, locs)
    return locs


def find_location(base: str, name: str):
    key = str(name or "").strip().lower()
    for l in load_locations(base):
        if str(l.get("name", "")).strip().lower() == key:
            return l
    return None
