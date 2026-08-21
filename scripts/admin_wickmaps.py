#!/usr/bin/env python3
"""Deep Desert "Wick Maps" — active Coriolis seed → pre-collected POI layout.

The Deep Desert is not infinitely random: it cycles through exactly 12
fixed layouts selected by a Coriolis world seed (0-11) that rotates each
week. So the map never has to be re-rendered — the community approach is
to collect the point-of-interest positions for all 12 seeds ONCE, then
each week detect the active seed and show that seed's layout.

This module is the pure half: pick the Deep Desert seed out of what
`dune.debug_get_coriolis_seeds()` reports (surfaced by admin-publish.sh's
`coriolis-seed` subcommand as `map,seed` rows), and load the matching
layout from data/admin/wickmaps.json. The engine draws the 9x9 sector
grid itself and overlays these POIs + the live player dots on the same
coordinate space — no terrain image, nothing under copyright.

POI layout data ported from coastal-ms/DST-DuneServerTool (Apache-2.0);
see ATTRIBUTION.md.
"""
from __future__ import annotations

import json
import os

CATALOG_PATH = "data/admin/wickmaps.json"


def active_dd_seed(rows: list[dict]) -> int | None:
    """The Deep Desert world seed from `map,seed` rows. Prefers the exact
    'DeepDesert' map, falls back to any 'DeepDesert*'. Returns None when no
    map resolves to a real 0-11 seed (e.g. every DD partition is -1 = auto,
    or the map isn't running)."""
    def seed_of(pred) -> int | None:
        for r in rows:
            name = str(r.get("map", ""))
            raw = r.get("seed")
            if raw is None:
                continue
            try:
                seed = int(raw)
            except (TypeError, ValueError):
                continue
            if pred(name) and 0 <= seed <= 11:
                return seed
        return None

    return (seed_of(lambda n: n == "DeepDesert")
            or seed_of(lambda n: n.startswith("DeepDesert")))


def load_layout(base: str, seed: int) -> dict | None:
    """The POI layout for one seed, or None if the seed/catalog is absent."""
    if seed is None or not 0 <= seed <= 11:
        return None
    try:
        with open(os.path.join(base, CATALOG_PATH), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    seeds = doc.get("seeds")
    if not isinstance(seeds, dict):
        return None
    return seeds.get(str(seed))


def deepdesert_view(base: str, rows: list[dict]) -> dict:
    """What the HTTP layer returns to the map UI: the active seed (or null),
    and its layout when one is known. Always well-formed."""
    seed = active_dd_seed(rows)
    layout = load_layout(base, seed) if seed is not None else None
    return {
        "seed": seed,
        "layout_available": layout is not None,
        "layout": layout,
    }
