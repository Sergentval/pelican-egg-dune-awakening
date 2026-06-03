#!/usr/bin/env python3
"""Unit tests for the faction-reputation math in admin_progression.py, ported
from Icehunter/dune-admin (MIT) cmd/dune-admin/db.go: repToTier,
factionTierName, factionDisplayName, factionRepCap, factionTierThresholds. The
proc call + m_FactionDataArray jsonb rebuild are verified live in-container;
here we pin the pure arithmetic (tier boundaries, clamp, naming).

Run: python3 scripts/test_faction_rep.py
"""
import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import admin_progression as ap  # noqa: E402  # type: ignore[import-not-found]


class RepToTier(unittest.TestCase):
    def test_floor_and_boundaries(self):
        self.assertEqual(ap.rep_to_tier(0), 0)
        self.assertEqual(ap.rep_to_tier(98), 0)
        self.assertEqual(ap.rep_to_tier(99), 1)     # threshold[1]
        self.assertEqual(ap.rep_to_tier(248), 1)
        self.assertEqual(ap.rep_to_tier(249), 2)    # threshold[2]
        self.assertEqual(ap.rep_to_tier(5149), 12)  # threshold[12]
        self.assertEqual(ap.rep_to_tier(11973), 18)
        self.assertEqual(ap.rep_to_tier(11974), 19)  # threshold[19]
        self.assertEqual(ap.rep_to_tier(12473), 19)
        self.assertEqual(ap.rep_to_tier(12474), 20)  # cap == tier 20

    def test_saturates_above_cap(self):
        self.assertEqual(ap.rep_to_tier(99999), 20)


class ClampFactionRep(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(ap.clamp_faction_rep(-5), 0)
        self.assertEqual(ap.clamp_faction_rep(0), 0)
        self.assertEqual(ap.clamp_faction_rep(5000), 5000)
        self.assertEqual(ap.clamp_faction_rep(12474), 12474)
        self.assertEqual(ap.clamp_faction_rep(99999), 12474)


class FactionTierName(unittest.TestCase):
    def test_named_low_tiers(self):
        self.assertEqual(ap.faction_tier_name(1, 0), "Outsider")
        self.assertEqual(ap.faction_tier_name(2, 1), "Mercenary")
        self.assertEqual(ap.faction_tier_name(1, 5), "House Operator")

    def test_tier20_faction_specific(self):
        self.assertEqual(ap.faction_tier_name(1, 20), "Envoy")
        self.assertEqual(ap.faction_tier_name(2, 20), "Enforcer")

    def test_mid_tiers_fall_through(self):
        self.assertEqual(ap.faction_tier_name(1, 6), "Tier 6")
        self.assertEqual(ap.faction_tier_name(2, 19), "Tier 19")


class FactionDisplayName(unittest.TestCase):
    def test_known_and_unknown(self):
        self.assertEqual(ap.faction_display_name(1), "Atreides")
        self.assertEqual(ap.faction_display_name(2), "Harkonnen")
        self.assertEqual(ap.faction_display_name(3), "None")
        self.assertEqual(ap.faction_display_name(4), "Smuggler")
        self.assertEqual(ap.faction_display_name(99), "Faction99")


class FactionRepOutcome(unittest.TestCase):
    def test_basic_gain(self):
        o = ap.faction_rep_outcome(current_rep=0, delta=100, faction_id=1)
        self.assertEqual(o["new_rep"], 100)
        self.assertEqual(o["tier"], 1)
        self.assertEqual(o["tier_name"], "Mercenary")
        self.assertEqual(o["faction_name"], "Atreides")
        self.assertFalse(o["capped"])

    def test_negative_clamps_to_zero_and_flags_capped(self):
        o = ap.faction_rep_outcome(current_rep=0, delta=-50, faction_id=1)
        self.assertEqual(o["new_rep"], 0)
        self.assertEqual(o["tier"], 0)
        self.assertTrue(o["capped"])   # -50 clamped up to 0

    def test_over_cap_clamps_and_flags(self):
        o = ap.faction_rep_outcome(current_rep=12400, delta=1000, faction_id=2)
        self.assertEqual(o["new_rep"], 12474)
        self.assertEqual(o["tier"], 20)
        self.assertEqual(o["tier_name"], "Enforcer")
        self.assertTrue(o["capped"])

    def test_in_range_not_capped(self):
        o = ap.faction_rep_outcome(current_rep=200, delta=300, faction_id=1)
        self.assertEqual(o["new_rep"], 500)   # threshold[3]
        self.assertEqual(o["tier"], 3)
        self.assertEqual(o["tier_name"], "Contractor")
        self.assertFalse(o["capped"])


class TierToRep(unittest.TestCase):
    """tier -> reputation amount that lands a character ON that tier in-game.
    Mirrors dune-admin cmdSetFactionTier: rep = threshold[tier], +1 when tier>0
    (the UI floors at the threshold, so rep==threshold shows the tier below)."""

    def test_thresholds_plus_one(self):
        self.assertEqual(ap.tier_to_rep(0), 0)        # tier 0 is the legit floor, no +1
        self.assertEqual(ap.tier_to_rep(1), 100)      # 99 + 1
        self.assertEqual(ap.tier_to_rep(5), 2000)     # 1999 + 1
        self.assertEqual(ap.tier_to_rep(6), 2225)     # 2224 + 1
        self.assertEqual(ap.tier_to_rep(20), 12475)   # 12474 + 1 (intentionally over cap)

    def test_round_trips_through_rep_to_tier(self):
        for t in range(0, 21):
            self.assertEqual(ap.rep_to_tier(ap.tier_to_rep(t)), t, f"tier {t} did not round-trip")

    def test_rejects_out_of_range(self):
        for bad in (-1, 21, 100):
            with self.assertRaises(ValueError):
                ap.tier_to_rep(bad)


class FactionTierOutcome(unittest.TestCase):
    def test_basic(self):
        o = ap.faction_tier_outcome(faction_id=1, tier=5)
        self.assertEqual(o["rep"], 2000)
        self.assertEqual(o["tier"], 5)
        self.assertEqual(o["tier_name"], "House Operator")
        self.assertEqual(o["faction_name"], "Atreides")

    def test_tier20_named(self):
        o = ap.faction_tier_outcome(faction_id=2, tier=20)
        self.assertEqual(o["rep"], 12475)
        self.assertEqual(o["tier_name"], "Enforcer")

    def test_tier0(self):
        o = ap.faction_tier_outcome(faction_id=1, tier=0)
        self.assertEqual(o["rep"], 0)
        self.assertEqual(o["tier"], 0)
        self.assertEqual(o["tier_name"], "Outsider")

    def test_rejects_bad_tier(self):
        with self.assertRaises(ValueError):
            ap.faction_tier_outcome(faction_id=1, tier=21)


if __name__ == "__main__":
    unittest.main()
