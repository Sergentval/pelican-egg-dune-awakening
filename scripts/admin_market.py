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
    if cmd == "orders":
        print(json.dumps(market_summary(base)))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
