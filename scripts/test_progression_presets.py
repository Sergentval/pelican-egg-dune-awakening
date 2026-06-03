#!/usr/bin/env python3
"""Unit tests for the progression-preset catalog helpers in admin_progression.py.
The presets are lifted from Icehunter/dune-admin (MIT) progression_presets.go;
here we pin the loader, lookup, and the Postgres text[] array formatter used to
hand preset roots to dune.complete_journey_story_nodes_for_player. The live proc
calls (complete/reset) are verified in-container.

Run: python3 scripts/test_progression_presets.py
"""
import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import admin_progression as ap  # noqa: E402  # type: ignore[import-not-found]


class LoadPresets(unittest.TestCase):
    def test_catalog_loads(self):
        ids = ap.progression_preset_ids()
        self.assertIn("skip_npe", ids)
        self.assertIn("unlock_all_lore", ids)
        self.assertIn("act1_complete", ids)
        self.assertEqual(len(ids), 9)

    def test_lookup_known(self):
        p = ap.progression_preset("skip_npe")
        self.assertIsNotNone(p)
        self.assertEqual(p["nodes"], ["DA_MQ_NPEAutocompleted"])
        self.assertEqual(p["name"], "Skip NPE (New Player Experience)")

    def test_lookup_multi_root(self):
        p = ap.progression_preset("unlock_all_lore")
        self.assertEqual(len(p["nodes"]), 4)
        self.assertIn("DA_Dunipedia_WarForArrakis", p["nodes"])

    def test_lookup_unknown(self):
        self.assertIsNone(ap.progression_preset("does_not_exist"))


class PgTextArray(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(ap.pg_text_array(["a", "b"]), '{"a","b"}')

    def test_dotted_and_spaced(self):
        self.assertEqual(
            ap.pg_text_array(["DA_Dunipedia_Landmarks.VermiliusGap", "Ariste Atreides"]),
            '{"DA_Dunipedia_Landmarks.VermiliusGap","Ariste Atreides"}',
        )

    def test_empty(self):
        self.assertEqual(ap.pg_text_array([]), "{}")

    def test_escapes_quotes_and_backslash(self):
        # Defensive: node ids never contain these, but the formatter must be safe.
        self.assertEqual(ap.pg_text_array(['a"b', "c\\d"]), '{"a\\"b","c\\\\d"}')


if __name__ == "__main__":
    unittest.main()
