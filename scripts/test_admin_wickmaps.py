"""Tests for admin_wickmaps.py — seed selection + layout loading."""
import os
import shutil
import tempfile
import unittest

import admin_wickmaps as wm


class SeedSelectionTests(unittest.TestCase):
    def test_prefers_exact_deepdesert(self):
        rows = [{"map": "DeepDesert", "seed": "2"},
                {"map": "DeepDesert_1", "seed": "5"}]
        self.assertEqual(wm.active_dd_seed(rows), 2)

    def test_falls_back_to_prefixed_when_exact_is_auto(self):
        rows = [{"map": "DeepDesert", "seed": "-1"},
                {"map": "DeepDesert_1", "seed": "7"}]
        self.assertEqual(wm.active_dd_seed(rows), 7)

    def test_none_when_all_auto(self):
        rows = [{"map": "DeepDesert", "seed": "-1"},
                {"map": "DeepDesert_1", "seed": "-1"}]
        self.assertIsNone(wm.active_dd_seed(rows))

    def test_none_when_no_dd_rows(self):
        self.assertIsNone(wm.active_dd_seed([{"map": "HaggaBasin", "seed": "3"}]))

    def test_out_of_range_seed_ignored(self):
        self.assertIsNone(wm.active_dd_seed([{"map": "DeepDesert", "seed": "12"}]))
        self.assertEqual(
            wm.active_dd_seed([{"map": "DeepDesert", "seed": "0"}]), 0)

    def test_exact_seed_zero_wins_over_prefixed(self):
        """Seed 0 is falsy: `exact or fallback` silently drew the wrong one of
        the 12 fixed layouts for a whole week, 1 seed in 12."""
        rows = [{"map": "DeepDesert_1", "seed": "7"},
                {"map": "DeepDesert", "seed": "0"}]
        self.assertEqual(wm.active_dd_seed(rows), 0)

    def test_malformed_rows_skipped(self):
        rows = [{"map": "DeepDesert", "seed": None},
                {"map": "DeepDesert", "seed": "x"},
                {"map": "DeepDesert_1", "seed": "4"}]
        self.assertEqual(wm.active_dd_seed(rows), 4)

    def test_empty(self):
        self.assertIsNone(wm.active_dd_seed([]))


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wm-test-")
        os.makedirs(os.path.join(self.base, "data", "admin"))
        import json
        doc = {"seeds": {"0": {"seed": 0, "pois": [
            {"sector": "I5", "subx": 3, "suby": 3, "type": "wreck"}]},
            "2": {"seed": 2, "pois": []}}}
        with open(os.path.join(self.base, wm.CATALOG_PATH), "w") as f:
            json.dump(doc, f)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_load_known_seed(self):
        layout = wm.load_layout(self.base, 0)
        self.assertEqual(layout["pois"][0]["sector"], "I5")

    def test_load_missing_seed_is_none(self):
        self.assertIsNone(wm.load_layout(self.base, 5))

    def test_load_out_of_range_is_none(self):
        self.assertIsNone(wm.load_layout(self.base, -1))
        self.assertIsNone(wm.load_layout(self.base, 99))

    def test_missing_catalog_is_none(self):
        os.remove(os.path.join(self.base, wm.CATALOG_PATH))
        self.assertIsNone(wm.load_layout(self.base, 0))

    def test_deepdesert_view_shape(self):
        view = wm.deepdesert_view(
            self.base, [{"map": "DeepDesert", "seed": "0"}])
        self.assertEqual(view["seed"], 0)
        self.assertTrue(view["layout_available"])
        self.assertEqual(view["layout"]["pois"][0]["type"], "wreck")

    def test_deepdesert_view_no_seed(self):
        view = wm.deepdesert_view(
            self.base, [{"map": "DeepDesert", "seed": "-1"}])
        self.assertIsNone(view["seed"])
        self.assertFalse(view["layout_available"])
        self.assertIsNone(view["layout"])

    def test_deepdesert_view_seed_without_layout(self):
        # seed resolves but catalog has no entry for it
        view = wm.deepdesert_view(
            self.base, [{"map": "DeepDesert", "seed": "5"}])
        self.assertEqual(view["seed"], 5)
        self.assertFalse(view["layout_available"])


class RealCatalogTests(unittest.TestCase):
    """The shipped catalog must cover all 12 seeds with well-formed POIs."""
    def test_shipped_catalog_is_complete(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import json
        with open(os.path.join(repo, wm.CATALOG_PATH), encoding="utf-8") as f:
            doc = json.load(f)
        seeds = doc["seeds"]
        self.assertEqual(sorted(seeds, key=int), [str(i) for i in range(12)])
        for k, s in seeds.items():
            self.assertEqual(s["seed"], int(k))
            for p in s["pois"]:
                self.assertRegex(p["sector"], r"^[A-I][1-9]$")
                # DST convention (WickMaps.tsx): subx 1-indexed (1-4),
                # suby 0-indexed (0-3); both centre in a 4x4 sub-grid.
                self.assertIn(p["subx"], (1, 2, 3, 4))
                self.assertIn(p["suby"], (0, 1, 2, 3))
                self.assertTrue(p["type"])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wm-sum-")
        os.makedirs(os.path.join(self.base, "data", "admin"))
        import json
        doc = {"seeds": {
            "0": {"seed": 0, "confidence": "CONFIRMED", "reliability": "high",
                  "largeSpiceSectors": ["F1", "H5"],
                  "legend": [{"type": "wreck", "label": "Wreck", "count": 14}],
                  "pois": [{"sector": "I5", "subx": 3, "suby": 3, "type": "wreck"}]},
            # deliberately unordered + a seed with no legend/spice
            "2": {"seed": 2, "pois": []},
        }}
        with open(os.path.join(self.base, wm.CATALOG_PATH), "w") as f:
            json.dump(doc, f)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_sorted_and_shaped(self):
        rows = wm.layouts_summary(self.base)
        self.assertEqual([r["seed"] for r in rows], [0, 2])
        s0 = rows[0]
        self.assertEqual(s0["poiCount"], 1)
        self.assertEqual(s0["spiceSectors"], 2)
        self.assertEqual(s0["confidence"], "CONFIRMED")
        self.assertEqual(s0["legend"], [{"type": "wreck", "label": "Wreck", "count": 14}])

    def test_seed_without_legend_or_spice(self):
        rows = wm.layouts_summary(self.base)
        s2 = next(r for r in rows if r["seed"] == 2)
        self.assertEqual(s2["poiCount"], 0)
        self.assertEqual(s2["spiceSectors"], 0)
        self.assertEqual(s2["legend"], [])
        self.assertIsNone(s2["confidence"])

    def test_missing_catalog_is_empty(self):
        os.remove(os.path.join(self.base, wm.CATALOG_PATH))
        self.assertEqual(wm.layouts_summary(self.base), [])

    def test_shipped_catalog_has_all_12(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rows = wm.layouts_summary(repo)
        self.assertEqual([r["seed"] for r in rows], list(range(12)))
        for r in rows:
            self.assertGreaterEqual(r["poiCount"], 0)
            self.assertIsInstance(r["legend"], list)


if __name__ == "__main__":
    unittest.main()
