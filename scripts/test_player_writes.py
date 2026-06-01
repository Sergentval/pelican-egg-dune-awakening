#!/usr/bin/env python3
"""Unit tests for the Phase-3 write compute logic: award_char_xp_outcome in
admin_progression.py (XP cap, level recompute, skill-point + intel math),
ported from Icehunter/dune-admin computeAwardCharXPOutcome. Pure logic — the
jsonb_set write itself is verified live in-container.

Run: python3 scripts/test_player_writes.py
"""
import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import admin_progression as ap  # noqa: E402  # type: ignore[import-not-found]


class AwardCharXpOutcome(unittest.TestCase):
    def test_basic_additive(self):
        # level-1 threshold is 40 cumulative XP; starter job occupies 1 SP.
        o = ap.award_char_xp_outcome(current_xp=0, spent_sp=0, keystone_bonus=0, amount=40)
        self.assertEqual(o["new_xp"], 40)
        self.assertEqual(o["new_level"], 1)
        self.assertEqual(o["new_total_sp"], 1)
        self.assertEqual(o["new_unspent_sp"], 0)   # max(0, 1 - 0 - 1)
        self.assertEqual(o["new_intel"], 4)
        self.assertFalse(o["capped"])

    def test_midgame_value(self):
        # cumulative[164]=113862 <= 114772 < cumulative[165]=116046 -> level 164
        o = ap.award_char_xp_outcome(current_xp=114672, spent_sp=29, keystone_bonus=0, amount=100)
        self.assertEqual(o["new_xp"], 114772)
        self.assertEqual(o["new_level"], 164)
        self.assertEqual(o["new_total_sp"], 164)
        self.assertEqual(o["new_unspent_sp"], 134)   # 164 - 29 - 1
        self.assertEqual(o["new_intel"], 2779)        # past the level-125 cap
        self.assertFalse(o["capped"])

    def test_caps_at_max_xp(self):
        o = ap.award_char_xp_outcome(current_xp=344440, spent_sp=0, keystone_bonus=0, amount=1000)
        self.assertEqual(o["new_xp"], 344440)
        self.assertEqual(o["new_level"], 200)
        self.assertEqual(o["new_total_sp"], 200)
        self.assertEqual(o["new_unspent_sp"], 199)
        self.assertTrue(o["capped"])

    def test_negative_amount_clamps_to_zero(self):
        o = ap.award_char_xp_outcome(current_xp=100, spent_sp=0, keystone_bonus=0, amount=-1000)
        self.assertEqual(o["new_xp"], 0)
        self.assertEqual(o["new_level"], 0)
        self.assertEqual(o["new_total_sp"], 0)
        self.assertEqual(o["new_unspent_sp"], 0)
        self.assertEqual(o["new_intel"], 0)
        self.assertFalse(o["capped"])

    def test_keystone_bonus_added_to_total_sp(self):
        # All 205 keystones grant +54 SP; total = level + bonus.
        o = ap.award_char_xp_outcome(current_xp=0, spent_sp=0, keystone_bonus=54, amount=0)
        self.assertEqual(o["new_xp"], 0)
        self.assertEqual(o["new_level"], 0)
        self.assertEqual(o["new_total_sp"], 54)         # 0 + 54
        self.assertEqual(o["new_unspent_sp"], 53)       # max(0, 54 - 0 - 1)

    def test_unspent_never_negative(self):
        o = ap.award_char_xp_outcome(current_xp=40, spent_sp=999, keystone_bonus=0, amount=0)
        self.assertEqual(o["new_unspent_sp"], 0)

    def test_exact_threshold_boundary(self):
        # cumulative[2] = 215
        o = ap.award_char_xp_outcome(current_xp=214, spent_sp=0, keystone_bonus=0, amount=1)
        self.assertEqual(o["new_xp"], 215)
        self.assertEqual(o["new_level"], 2)


if __name__ == "__main__":
    unittest.main()
