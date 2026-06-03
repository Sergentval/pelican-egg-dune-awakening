#!/usr/bin/env python3
"""Golden tests for admin_market pricing (Phase 7a).

Values ported from dune-admin internal/marketbot/pricing_test.go so our port is
provably faithful, plus the Coastal sane-pricing additions (grade multipliers +
100k cap) and round-half-up parity with Go's math.Round.
"""
import unittest

from admin_market import (
    PRICE_CAP,
    base_price,
    build_listings,
    category_for,
    category_mask,
    compute_price,
    round_price,
    unique_schematics_mask,
)


def item(**kw):
    base = {"vendor_price": 0, "rarity": "", "stack_max": 1, "tier": 0, "is_schematic": False}
    base.update(kw)
    return base


class TestVendorPath(unittest.TestCase):
    # vendor_price >= 10 -> vendor_price * vendorMult(rarity)
    def test_rarity_ordering(self):
        for rarity, want in [("common", 1000), ("memento", 2000), ("rare", 5000), ("Unique", 5000)]:
            got = compute_price(item(vendor_price=1000, rarity=rarity, stack_max=500))
            self.assertEqual(got, want, f"vendor rarity={rarity}")


class TestFallbackPath(unittest.TestCase):
    # vendor_price < 10, stack_max=1, tier=1 -> equipmentPrice(1)=2000 * rarityMult
    def test_rarity_ordering(self):
        for rarity, want in [("common", 2000), ("memento", 4000), ("rare", 10000), ("Unique", 10000)]:
            got = compute_price(item(vendor_price=0, rarity=rarity, stack_max=1, tier=1))
            self.assertEqual(got, want, f"fallback rarity={rarity}")


class TestEmptyRarityIsCommon(unittest.TestCase):
    def test_empty_equals_common(self):
        a = compute_price(item(vendor_price=1000, rarity="", stack_max=500))
        b = compute_price(item(vendor_price=1000, rarity="common", stack_max=500))
        self.assertEqual(a, b)


class TestRoundPrice(unittest.TestCase):
    def test_steps_round_half_up(self):
        self.assertEqual(round_price(1049), 1000)   # step 100
        self.assertEqual(round_price(1050), 1100)   # half rounds up (Go math.Round parity)
        self.assertEqual(round_price(12_140), 12_000)
        self.assertEqual(round_price(121_400), 120_000)  # step 10k


class TestGradeMultiplier(unittest.TestCase):
    def test_grade_scales(self):
        it = item(vendor_price=1000, rarity="common", stack_max=500)
        self.assertEqual(compute_price(it, 0), 1000)
        # grade 1 = x1.25 -> round_half_up(1250) -> round_price(1250)=1300
        self.assertEqual(compute_price(it, 1), 1300)


class TestCap(unittest.TestCase):
    def test_hard_100k_cap(self):
        # T4 Radiation Suit: vendor_price 121400 common -> 120000 rounded -> capped 100k
        got = compute_price(item(vendor_price=121400, rarity="common", stack_max=1, tier=4))
        self.assertEqual(got, PRICE_CAP)
        # a grade bump can't break the cap either
        self.assertLessEqual(compute_price(item(vendor_price=121400, rarity="rare", stack_max=1, tier=6), 5), PRICE_CAP)


class TestBasePriceFallbackMaterials(unittest.TestCase):
    def test_stackable_material(self):
        # vendor 0, stackable (stack_max>1), tier 1 -> material_unit_price(1)=20 * common 1.0
        self.assertEqual(base_price(item(vendor_price=0, rarity="common", stack_max=500, tier=1)), 20)


class TestCategoryMask(unittest.TestCase):
    # Golden values from dune-admin pricing_test.go TestCategoryMask.
    CASES = {
        "items/weapons/shortblades": (0x01000000, 3),   # melee remap
        "items/weapons/longblades": (0x01000100, 3),    # melee remap
        "items/weapons/pistol": (0x01020000, 2),
        "items/weapons/ammunition": (0x010E0000, 2),
        "items/augment/misc": (0x04030000, 2),
        "items/misc/refinedresources": (0x05010000, 2),
        "items/garment/lightarmor/chest": (0x00000100, 3),
        "items/garment/heavyarmor/head": (0x00010000, 3),
        "items/vehicles/mediumornithopter/chassis": (0x02030000, 3),
        "items/utility/consumables": (0x03060000, 2),
        "items/utility/gatheringtools/cutteray": (0x03030000, 3),
    }

    def test_golden(self):
        for cat, (mask, depth) in self.CASES.items():
            self.assertEqual(category_mask(cat), (mask, depth), cat)

    def test_unknown_segment_zero_mask(self):
        m, _ = category_mask("items/garment/notarealsegment")
        self.assertEqual(m, 0)  # unknown depth-2 -> 0 (caller skips)

    def test_empty(self):
        self.assertEqual(category_mask(""), (0, 0))


class TestUniqueSchematicsMask(unittest.TestCase):
    CASES = {
        "items/garment/lightarmor/chest": (0x00050000, 3, True),
        "items/garment/heavyarmor/head": (0x00050100, 3, True),
        "items/weapons/shortblades": (0x01030000, 3, True),
        "items/weapons/pistol": (0x01030200, 3, True),
        "items/vehicles/sandbike": (0x02060000, 3, True),
    }

    def test_golden(self):
        for cat, want in self.CASES.items():
            self.assertEqual(unique_schematics_mask(cat), want, cat)

    def test_no_unique_section(self):
        # misc has no UNIQUE SCHEMATICS section -> ok False
        self.assertEqual(unique_schematics_mask("items/misc/components")[2], False)


class TestBuildListings(unittest.TestCase):
    CAT = {
        "Pistol_A": {"category": "items/weapons/pistol", "vendor_price": 1000, "tradeable": True, "stack_max": 1},
        "Junk": {"category": "items/weapons/pistol", "vendor_price": 0, "tradeable": True, "stack_max": 1},
        "Untradeable": {"category": "items/weapons/pistol", "vendor_price": 1000, "tradeable": False, "stack_max": 1},
        "Unknown": {"category": "items/garment/notreal", "vendor_price": 1000, "tradeable": True, "stack_max": 1},
        "Stacky": {"category": "items/misc/components", "vendor_price": 50, "tradeable": True, "stack_max": 100},
    }

    def test_filters_and_fields(self):
        out = build_listings(self.CAT)
        self.assertEqual({li["template"] for li in out}, {"Pistol_A", "Stacky"})
        pa = next(li for li in out if li["template"] == "Pistol_A")
        self.assertEqual((pa["mask"], pa["depth"]), (0x01020000, 2))
        self.assertEqual(next(li for li in out if li["template"] == "Stacky")["qty"], 100)

    def test_limit(self):
        self.assertEqual(len(build_listings(self.CAT, limit=1)), 1)


class TestCategoryFor(unittest.TestCase):
    def test_standard_item_uses_category_mask(self):
        it = {"category": "items/weapons/pistol", "rarity": "common", "is_schematic": False}
        self.assertEqual(category_for(it), (0x01020000, 2, True))

    def test_schematic_reroutes_to_unique(self):
        it = {"category": "items/weapons/pistol", "rarity": "unique", "is_schematic": True}
        self.assertEqual(category_for(it), (0x01030200, 3, True))

    def test_unknown_category_not_ok(self):
        it = {"category": "items/garment/notreal", "is_schematic": False}
        self.assertEqual(category_for(it)[2], False)


if __name__ == "__main__":
    unittest.main()
