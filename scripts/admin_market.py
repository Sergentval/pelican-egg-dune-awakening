#!/usr/bin/env python3
"""Market-bot pricing engine + read-only market view (Phase 7a).

Port of Icehunter/dune-admin internal/marketbot pricing (MIT) with the Coastal
"sane pricing" model: per-quality grade multipliers [1, 1.25, 1.55, 2, 2.6, 3.3]
and a hard 100k cap. The pure pricing functions are unit-tested against
dune-admin's pricing_test.go golden values (round-half-up matches Go math.Round).

This phase does NOT touch the exchange — it only computes prices and reads the
current orders. Posting NPC orders + the d12-gamble buy + the bot loop are 7b
(deferred until we verify how the game consumes dune_exchange_orders). The item
catalogue is lifted from dune-admin item-data.json (MIT — see ATTRIBUTION.md).
"""
import json
import math
import os
import subprocess
import sys

# Coastal sane-pricing defaults (0001-sane-pricing-100k-cap.patch).
GRADE_MULTIPLIERS = [1.0, 1.25, 1.55, 2.0, 2.6, 3.3]
RARITY_MULTIPLIERS = {"common": 1.0, "rare": 5.0, "unique": 5.0, "memento": 2.0}
VENDOR_MULTIPLIERS = {"common": 1.0, "rare": 5.0, "unique": 5.0, "memento": 2.0}
PRICE_CAP = 100_000
MIN_MEANINGFUL_VENDOR = 10


def _round_half_up(x: float) -> int:
    """Match Go's math.Round (half away from zero); prices are non-negative."""
    return int(math.floor(x + 0.5))


def _mult(rarity, table) -> float:
    return table.get((rarity or "").strip().lower(), 1.0)


def equipment_price(tier: int) -> int:
    return {0: 500, 1: 2000, 2: 8000, 3: 30000, 4: 100000, 5: 300000, 6: 750000}.get(tier, 500)


def schematic_equipment_price(tier: int) -> int:
    return {1: 500, 2: 1500, 3: 4000, 4: 12000, 5: 30000, 6: 75000}.get(tier, 500)


def material_unit_price(tier: int) -> int:
    return {0: 5, 1: 20, 2: 80, 3: 200, 4: 600, 5: 1500, 6: 4000}.get(tier, 5)


def round_price(v: int) -> int:
    """Step-round to a clean magnitude (Go roundPrice parity)."""
    v = int(v)
    if v >= 1_000_000:
        step = 100_000
    elif v >= 100_000:
        step = 10_000
    elif v >= 10_000:
        step = 1_000
    elif v >= 1_000:
        step = 100
    else:
        step = 10
    return _round_half_up(v / step) * step


def cap_price(v: int) -> int:
    return min(int(v), PRICE_CAP)


def base_price(item: dict) -> int:
    """Unrounded-to-step base price (Go basePrice). Vendor path when a real
    vendor price exists; otherwise tier/stack fallback. The unique-crafting-cost
    path is omitted: item-data.json carries no material_cost, so it never fires."""
    vp = int(item.get("vendor_price") or 0)
    rarity = item.get("rarity") or ""
    if vp >= MIN_MEANINGFUL_VENDOR:
        return _round_half_up(vp * _mult(rarity, VENDOR_MULTIPLIERS))
    mult = _mult(rarity, RARITY_MULTIPLIERS)
    tier = int(item.get("tier") or 0)
    if int(item.get("stack_max") or 1) <= 1:
        base = schematic_equipment_price(tier) if item.get("is_schematic") else equipment_price(tier)
        return _round_half_up(base * mult)
    return max(_round_half_up(material_unit_price(tier) * mult), 1)


def compute_price(item: dict, grade: int = 0) -> int:
    """Final listing price for an item at a quality grade: base x grade
    multiplier, step-rounded, then clamped at the 100k hard cap."""
    gm = GRADE_MULTIPLIERS[grade] if 0 <= grade < len(GRADE_MULTIPLIERS) else 1.0
    return cap_price(round_price(_round_half_up(base_price(item) * gm)))


# --------------------------------------------------------------------------
# Category mask encoder — ported from dune-admin internal/marketbot/pricing.go
# (MIT). Static lookup tables from in-game UI category ordering; the live
# category index (the Go `idx` param) is unused, so this is fully pure. The
# game packs the category as bytes: depth1->bits 24-31, depth2->16-23,
# depth3->8-15. mask=0 for an unknown segment so callers skip the listing
# rather than insert a conflicting guessed mask.
# --------------------------------------------------------------------------
_KNOWN_CODES = {
    1: {"garment": 0, "weapons": 1, "vehicles": 2, "utility": 3, "augment": 4, "misc": 5},
    2: {
        "lightarmor": 0, "heavyarmor": 1, "stillsuits": 2, "utilitywearables": 3, "socialwearables": 4,
        "pistol": 2, "heavypistol": 3, "heavyrifle": 4, "smg": 5, "spitdart": 6, "shotgun": 7,
        "battlerifle": 8, "heavyshotgun": 9, "missilelauncher": 10, "flamethrower": 11,
        "fireballer": 12, "lasgun": 13, "ammunition": 14,
        "sandbike": 0, "buggy": 1, "lightornithopter": 2, "mediumornithopter": 3,
        "transportornithopter": 4, "sandcrawler": 5,
        "buildingtools": 0, "hydrationtools": 2, "gatheringtools": 3, "cartographytools": 4,
        "utilitytools": 5, "consumables": 6,
        "armor": 0, "melee": 1, "ranged": 2, "misc": 3,
        "fuel": 0, "refinedresources": 1, "components": 2, "rawresources": 3,
    },
    3: {"cutteray": 0, "compactor": 1},
}
_DEPTH3_PARENT = {
    ("lightarmor", "head"): 0, ("lightarmor", "chest"): 1, ("lightarmor", "legs"): 2,
    ("lightarmor", "hands"): 3, ("lightarmor", "feet"): 4,
    ("heavyarmor", "head"): 0, ("heavyarmor", "chest"): 1, ("heavyarmor", "legs"): 2,
    ("heavyarmor", "hands"): 3, ("heavyarmor", "feet"): 4,
    ("stillsuits", "head"): 0, ("stillsuits", "chest"): 1, ("stillsuits", "hands"): 2, ("stillsuits", "feet"): 3,
    ("socialwearables", "chest"): 0, ("socialwearables", "legs"): 1, ("socialwearables", "hands"): 2, ("socialwearables", "feet"): 3,
    ("sandbike", "chassis"): 0, ("sandbike", "hull"): 1, ("sandbike", "engine"): 2, ("sandbike", "psu"): 3, ("sandbike", "locomotion"): 4, ("sandbike", "utility"): 5,
    ("buggy", "chassis"): 0, ("buggy", "hull"): 1, ("buggy", "rear"): 2, ("buggy", "engine"): 3, ("buggy", "psu"): 4, ("buggy", "locomotion"): 5, ("buggy", "turret"): 6, ("buggy", "utility"): 7,
    ("lightornithopter", "chassis"): 0, ("lightornithopter", "cockpit"): 1, ("lightornithopter", "hull"): 2, ("lightornithopter", "engine"): 3, ("lightornithopter", "psu"): 4, ("lightornithopter", "locomotion"): 5, ("lightornithopter", "utility"): 6,
    ("mediumornithopter", "chassis"): 0, ("mediumornithopter", "cabin"): 1, ("mediumornithopter", "cockpit"): 2, ("mediumornithopter", "tail"): 3, ("mediumornithopter", "engine"): 4, ("mediumornithopter", "psu"): 5, ("mediumornithopter", "locomotion"): 6, ("mediumornithopter", "utility"): 7,
    ("transportornithopter", "chassis"): 0, ("transportornithopter", "hull"): 1, ("transportornithopter", "engine"): 2, ("transportornithopter", "psu"): 3, ("transportornithopter", "locomotion"): 4, ("transportornithopter", "utility"): 5,
    ("sandcrawler", "chassis"): 0, ("sandcrawler", "cabin"): 1, ("sandcrawler", "engine"): 2, ("sandcrawler", "psu"): 3, ("sandcrawler", "locomotion"): 4, ("sandcrawler", "utility"): 5,
    ("hydrationtools", "watertools"): 0, ("hydrationtools", "bloodtools"): 1,
    ("utilitytools", "powerpack"): 0, ("utilitytools", "suspensor"): 1, ("utilitytools", "utility"): 3,
    ("consumables", "utility"): 2,
}
_WEAPON_PATH_REMAP = {"shortblades": (0, 0), "longblades": (0, 1)}
_UNIQUE_SCHEM_D2 = {"garment": 5, "weapons": 3, "vehicles": 6, "utility": 7, "augment": 4}
_UNIQUE_SCHEM_D3 = {
    "lightarmor": 0, "heavyarmor": 1, "stillsuits": 2, "utilitywearables": 3, "socialwearables": 4,
    "shortblades": 0, "longblades": 1, "pistol": 2, "heavypistol": 3, "heavyrifle": 4, "smg": 5,
    "spitdart": 6, "shotgun": 7, "battlerifle": 8, "heavyshotgun": 9, "missilelauncher": 10,
    "flamethrower": 11, "fireballer": 12, "lasgun": 13,
    "sandbike": 0, "buggy": 1, "lightornithopter": 2, "mediumornithopter": 3, "transportornithopter": 4, "sandcrawler": 5,
    "deployables": 0, "watertools": 1, "bloodtools": 2, "cutteray": 3, "staticcompactor": 4,
    "cartographytools": 5, "shield": 6, "suspensor": 7, "powerpack": 8,
    "armor": 0, "melee": 1, "ranged": 2, "misc": 3,
}


def category_mask(category: str) -> tuple[int, int]:
    """(mask, depth) for a standard item category path. mask=0 if any segment
    is unknown (caller should skip the listing)."""
    if not category:
        return 0, 0
    parts = category.split("/")
    n = min(len(parts), 4)
    depth = max(n - 1, 0)
    # Melee remap: item-data has items/weapons/shortblades (depth-2) but the game
    # nests it under melee at depth-3 (items/weapons/melee/shortblades).
    if n >= 3 and parts[1] == "weapons" and parts[2] in _WEAPON_PATH_REMAP:
        d2, d3 = _WEAPON_PATH_REMAP[parts[2]]
        return (_KNOWN_CODES[1]["weapons"] << 24) | (d2 << 16) | (d3 << 8), 3
    mask = 0
    for i in range(1, n):  # skip i=0 (root "items")
        seg = parts[i]
        code = 0
        if i == 3 and len(parts) >= 3 and (parts[2], seg) in _DEPTH3_PARENT:
            code = _DEPTH3_PARENT[(parts[2], seg)]
        elif seg in _KNOWN_CODES.get(i, {}):
            code = _KNOWN_CODES[i][seg]
        mask |= code << ((4 - i) * 8)
    return mask, depth


def unique_schematics_mask(category: str) -> tuple[int, int, bool]:
    """(mask, depth, ok) routing unique/memento items into the UNIQUE SCHEMATICS
    subcategory. ok=False when the depth-1 category has no such section."""
    parts = category.split("/")
    if len(parts) < 3:
        return 0, 0, False
    d1seg = parts[1]
    if d1seg not in _UNIQUE_SCHEM_D2:
        return 0, 0, False
    d3seg = parts[2]
    if d3seg not in _UNIQUE_SCHEM_D3 and len(parts) >= 4:
        d3seg = parts[3]
    if d3seg not in _UNIQUE_SCHEM_D3:
        return 0, 0, False
    mask = (_KNOWN_CODES[1].get(d1seg, 0) << 24) | (_UNIQUE_SCHEM_D2[d1seg] << 16) | (_UNIQUE_SCHEM_D3[d3seg] << 8)
    return mask, 3, True


def category_for(item: dict) -> tuple[int, int, bool]:
    """(mask, depth, ok) for a catalog item: schematic items reroute into UNIQUE
    SCHEMATICS, everything else uses the standard mask. ok=False -> skip."""
    cat = item.get("category", "") or ""
    if item.get("is_schematic") and cat:
        m, d, ok = unique_schematics_mask(cat)
        if ok:
            return m, d, True
    if cat:
        m, d = category_mask(cat)
        if m != 0:
            return m, d, True
    return 0, 0, False


def build_listings(items: dict, limit: int = 0) -> list[dict]:
    """Select tradeable catalog items with a known category + a meaningful vendor
    price, and compute (price, mask, depth, qty) for each NPC sell order to post.
    limit<=0 = all. Pure — the caller does the DB inserts. Skips junk so the bot
    never lists no-price/uncategorised items."""
    out = []
    for tmpl in sorted(items):
        it = items[tmpl]
        if not it.get("tradeable", True):
            continue
        if (it.get("vendor_price") or 0) < MIN_MEANINGFUL_VENDOR:
            continue
        mask, depth, ok = category_for(it)
        if not ok:
            continue
        out.append({
            "template": tmpl,
            "qty": max(1, int(it.get("stack_max", 1) or 1)),
            "quality": 0,
            "price": compute_price(it, 0),
            "mask": mask,
            "depth": depth,
        })
        if limit and len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Catalogue + read-only market view (I/O; not unit-tested).
# --------------------------------------------------------------------------
def catalog_path(base):
    return os.path.join(base, "data", "admin", "item-data.json")


def load_catalog(base):
    """Return (items_dict, names_dict) from item-data.json."""
    with open(catalog_path(base), encoding="utf-8") as f:
        d = json.load(f)
    return d.get("items", {}), d.get("names", {})


def _publish(base):
    return os.path.join(base, "scripts", "admin-publish.sh")


def market_summary(base):
    """Read-only snapshot of the live exchange (order counts). No writes."""
    sql = ("SELECT count(*) AS total, "
           "count(*) FILTER (WHERE is_npc_order) AS npc, "
           "count(*) FILTER (WHERE NOT is_npc_order) AS player "
           "FROM dune.dune_exchange_orders")
    res = subprocess.run(["bash", _publish(base), "db-sql", sql],
                         capture_output=True, text=True, timeout=20)
    return {"ok": res.returncode == 0, "csv": res.stdout.strip(), "err": res.stderr.strip()[:300]}


def _main(argv):
    if len(argv) < 2:
        print("usage: admin_market.py <price|catalog-stats|orders> [args] [BASE]", file=sys.stderr)
        return 2
    cmd = argv[1]
    base = os.environ.get("DUNE_BASE_DIR", "/home/container")
    # trailing arg may be BASE
    if len(argv) > 2 and os.path.isdir(argv[-1]):
        base = argv[-1]
    if cmd == "price":
        if len(argv) < 3:
            print("usage: admin_market.py price <template> [grade]", file=sys.stderr)
            return 2
        template = argv[2]
        grade = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 0
        items, names = load_catalog(base)
        it = items.get(template)
        if it is None:
            print(json.dumps({"ok": False, "error": f"unknown template {template!r}"}))
            return 1
        print(json.dumps({"ok": True, "template": template, "name": names.get(template, it.get("name", "")),
                          "grade": grade, "price": compute_price(it, grade),
                          "base": base_price(it), "vendor_price": it.get("vendor_price"),
                          "rarity": it.get("rarity"), "tier": it.get("tier")}))
        return 0
    if cmd == "catalog-stats":
        items, _ = load_catalog(base)
        vp = sum(1 for v in items.values() if (v.get("vendor_price") or 0) >= MIN_MEANINGFUL_VENDOR)
        print(json.dumps({"ok": True, "items": len(items), "vendor_priced": vp,
                          "fallback_priced": len(items) - vp}))
        return 0
    if cmd == "listings":
        limit = 0
        for i, a in enumerate(argv):
            if a == "--limit" and i + 1 < len(argv) and argv[i + 1].isdigit():
                limit = int(argv[i + 1])
        items, _ = load_catalog(base)
        # pipe-delimited so the bash market-bot-post loop can read it:
        # template|qty|quality|price|mask|depth
        for li in build_listings(items, limit):
            print(f"{li['template']}|{li['qty']}|{li['quality']}|{li['price']}|{li['mask']}|{li['depth']}")
        return 0
    if cmd == "orders":
        print(json.dumps(market_summary(base)))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
