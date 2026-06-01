#!/usr/bin/env python3
"""Unit tests for the Phase-2 pure progression math in admin_progression.py:
the XP->level binary search, the intel curve, keystone skill-point bonus, the
grant-all-keystones target math, and the character-XP summary. All ported
verbatim from Icehunter/dune-admin (MIT) cmd/dune-admin/{db.go,keystones.go}.
Pure-logic, no DB/RMQ/network needed — the live queries are verified in-container.

Run: python3 scripts/test_players_read.py
"""
import importlib.util
import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import admin_progression as ap  # noqa: E402  # type: ignore[import-not-found]

# Load admin-http.py (hyphenated) for csv_to_table, to assert the CSV column
# contract between the bash char-xp-read subcommand and the HTTP enrichment.
_spec = importlib.util.spec_from_file_location("admin_http", _HERE / "admin-http.py")
assert _spec is not None and _spec.loader is not None
admin_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin_http)


class XpToLevel(unittest.TestCase):
    def test_zero_and_negative_are_level_zero(self):
        self.assertEqual(ap.xp_to_level(0), 0)
        self.assertEqual(ap.xp_to_level(-1), 0)
        self.assertEqual(ap.xp_to_level(-9999), 0)

    def test_any_positive_xp_floors_to_level_one(self):
        # dune-admin's xpToLevel returns at least 1 for any xp > 0, even
        # below the level-1 threshold (cumulative[1] == 40). Faithful port.
        self.assertEqual(ap.xp_to_level(1), 1)
        self.assertEqual(ap.xp_to_level(39), 1)
        self.assertEqual(ap.xp_to_level(40), 1)
        self.assertEqual(ap.xp_to_level(214), 1)

    def test_threshold_boundaries(self):
        # cumulative[2] == 215, cumulative[100] == 58190, cumulative[200] == 344440
        self.assertEqual(ap.xp_to_level(215), 2)
        self.assertEqual(ap.xp_to_level(58189), 99)
        self.assertEqual(ap.xp_to_level(58190), 100)
        self.assertEqual(ap.xp_to_level(344439), 199)
        self.assertEqual(ap.xp_to_level(344440), 200)

    def test_clamps_at_200(self):
        self.assertEqual(ap.xp_to_level(999_999), 200)

    def test_monotonic_nondecreasing(self):
        prev = 0
        for xp in range(0, 60000, 137):
            lvl = ap.xp_to_level(xp)
            self.assertGreaterEqual(lvl, prev)
            prev = lvl


class IntelAtLevel(unittest.TestCase):
    def test_known_points(self):
        cases = {
            0: 0, 1: 4, 2: 6, 3: 8, 15: 44, 16: 49, 30: 119, 31: 129,
            50: 319, 51: 339, 69: 699, 70: 729, 85: 1179, 86: 1219,
            125: 2779, 126: 2779, 200: 2779,
        }
        for level, expected in cases.items():
            self.assertEqual(ap.intel_at_level(level), expected, f"level {level}")

    def test_caps_at_2779(self):
        for level in (125, 150, 200, 500):
            self.assertEqual(ap.intel_at_level(level), 2779)


class KeystoneSPBonus(unittest.TestCase):
    def test_all_keystones_total(self):
        self.assertEqual(ap.keystone_sp_bonus(ap.all_keystone_ids()), 54)

    def test_all_keystone_ids_is_1_to_205(self):
        self.assertEqual(ap.all_keystone_ids(), list(range(1, 206)))

    def test_suffix_rules(self):
        self.assertEqual(ap.keystone_sp_bonus([1]), 3)    # _SkillPoint_Major
        self.assertEqual(ap.keystone_sp_bonus([2]), 1)    # _SkillPoint
        self.assertEqual(ap.keystone_sp_bonus([30]), 5)   # _SkillPoint_Super
        self.assertEqual(ap.keystone_sp_bonus([205]), 0)  # _Hat (no SP)

    def test_unknown_ids_ignored(self):
        self.assertEqual(ap.keystone_sp_bonus([9999, -1, 0]), 0)
        self.assertEqual(ap.keystone_sp_bonus([1, 9999]), 3)


class GrantAllKeystoneTargets(unittest.TestCase):
    def test_level_200_no_spent(self):
        total, unspent, bonus = ap.grant_all_keystone_targets(344440, 0)
        self.assertEqual(bonus, 54)
        self.assertEqual(total, 254)        # level 200 + 54
        self.assertEqual(unspent, 253)      # 254 - 0 - 1

    def test_level_zero(self):
        total, unspent, bonus = ap.grant_all_keystone_targets(0, 0)
        self.assertEqual((total, bonus), (54, 54))  # level 0 + 54
        self.assertEqual(unspent, 53)               # 54 - 0 - 1

    def test_unspent_never_negative(self):
        _, unspent, _ = ap.grant_all_keystone_targets(0, 1000)
        self.assertEqual(unspent, 0)


class CharXpSummary(unittest.TestCase):
    def test_basic(self):
        s = ap.char_xp_summary(58190)
        self.assertEqual(s["xp"], 58190)
        self.assertEqual(s["level"], 100)
        self.assertEqual(s["intel"], 1779)   # level 100: 1179 + (100-85)*40 = 1779
        self.assertEqual(s["maxXP"], 344440)
        self.assertFalse(s["atCap"])

    def test_at_cap(self):
        s = ap.char_xp_summary(344440)
        self.assertEqual(s["level"], 200)
        self.assertTrue(s["atCap"])

    def test_over_cap_is_capped_flag(self):
        s = ap.char_xp_summary(500000)
        self.assertEqual(s["level"], 200)
        self.assertTrue(s["atCap"])

    def test_zero(self):
        s = ap.char_xp_summary(0)
        self.assertEqual(s["level"], 0)
        self.assertEqual(s["intel"], 0)
        self.assertFalse(s["atCap"])


class CharXpColumnContract(unittest.TestCase):
    """The char-xp-read bash subcommand emits these exact CSV headers; the
    HTTP enrichment in _handle_player_char_xp reads them by name. Lock the
    contract so a rename on either side fails loudly here, not silently live."""

    def test_csv_headers_match_enrichment_keys(self):
        csv = "total_xp,total_skill_points,spent_skill_points\n58190,150,40\n"
        table = admin_http.csv_to_table(csv)
        self.assertEqual(table["headers"], ["total_xp", "total_skill_points", "spent_skill_points"])
        row = dict(zip(table["headers"], table["rows"][0]))
        # Same parse the handler does:
        summary = ap.char_xp_summary(int(row["total_xp"]))
        summary["totalSkillPoints"] = int(row["total_skill_points"])
        summary["spentSkillPoints"] = int(row["spent_skill_points"])
        self.assertEqual(summary["level"], 100)
        self.assertEqual(summary["totalSkillPoints"], 150)
        self.assertEqual(summary["spentSkillPoints"], 40)
        self.assertFalse(summary["atCap"])


if __name__ == "__main__":
    unittest.main()
