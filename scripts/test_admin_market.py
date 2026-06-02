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
    compute_price,
    round_price,
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


if __name__ == "__main__":
    unittest.main()
