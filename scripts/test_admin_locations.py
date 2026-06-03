#!/usr/bin/env python3
"""Tests for admin_locations (Phase 3): the teleport-locations store."""
import os
import tempfile
import unittest

import admin_locations as al


class TestNormalize(unittest.TestCase):
    def test_valid(self):
        loc, err = al.normalize_location({"name": " Hagga safe outpost ", "map": "HaggaBasin", "x": 1, "y": "2.5", "z": -3})
        self.assertIsNone(err)
        self.assertEqual(loc, {"name": "Hagga safe outpost", "map": "HaggaBasin", "x": 1.0, "y": 2.5, "z": -3.0})

    def test_bad_map(self):
        _, err = al.normalize_location({"name": "x", "map": "Survival_1", "x": 0, "y": 0, "z": 0})
        self.assertIn("map", err)

    def test_missing_name(self):
        _, err = al.normalize_location({"name": "", "map": "HaggaBasin", "x": 0, "y": 0, "z": 0})
        self.assertIn("name", err)

    def test_bad_coords(self):
        _, err = al.normalize_location({"name": "x", "map": "HaggaBasin", "x": "nope", "y": 0, "z": 0})
        self.assertIn("x, y, z", err)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_load_missing_is_empty(self):
        self.assertEqual(al.load_locations(self.dir), [])

    def test_upsert_add_replace_sort(self):
        locs, err = al.upsert_location(self.dir, {"name": "Zeta", "map": "HaggaBasin", "x": 1, "y": 1, "z": 1})
        self.assertIsNone(err)
        al.upsert_location(self.dir, {"name": "Alpha", "map": "HaggaBasin", "x": 2, "y": 2, "z": 2})
        # replace Zeta (case-insensitive) with new coords
        locs, _ = al.upsert_location(self.dir, {"name": "zeta", "map": "DeepDesert", "x": 9, "y": 9, "z": 9})
        self.assertEqual(len(locs), 2)
        names = [l["name"] for l in al.load_locations(self.dir)]
        # sorted by (map, name): zeta is now DeepDesert (sorts before HaggaBasin), Alpha is HaggaBasin
        self.assertEqual(names, ["zeta", "Alpha"])
        z = al.find_location(self.dir, "ZETA")
        self.assertEqual(z["map"], "DeepDesert")
        self.assertEqual(z["x"], 9.0)

    def test_remove(self):
        al.upsert_location(self.dir, {"name": "A", "map": "HaggaBasin", "x": 0, "y": 0, "z": 0})
        al.upsert_location(self.dir, {"name": "B", "map": "HaggaBasin", "x": 0, "y": 0, "z": 0})
        left = al.remove_location(self.dir, "a")
        self.assertEqual([l["name"] for l in left], ["B"])
        self.assertIsNone(al.find_location(self.dir, "A"))

    def test_persisted_to_state_path(self):
        al.upsert_location(self.dir, {"name": "P", "map": "Arrakeen", "x": 0, "y": 0, "z": 0})
        self.assertTrue(os.path.isfile(al.store_path(self.dir)))

    def test_count_cap(self):
        orig = al.MAX_LOCATIONS
        al.MAX_LOCATIONS = 2
        try:
            al.upsert_location(self.dir, {"name": "A", "map": "HaggaBasin", "x": 0, "y": 0, "z": 0})
            al.upsert_location(self.dir, {"name": "B", "map": "HaggaBasin", "x": 0, "y": 0, "z": 0})
            locs, err = al.upsert_location(self.dir, {"name": "C", "map": "HaggaBasin", "x": 0, "y": 0, "z": 0})
            self.assertIsNone(locs)
            self.assertIn("too many", err)
            # replacing an existing name at the cap still works (no growth)
            locs, err = al.upsert_location(self.dir, {"name": "a", "map": "DeepDesert", "x": 9, "y": 9, "z": 9})
            self.assertIsNone(err)
            self.assertEqual(len(locs), 2)
        finally:
            al.MAX_LOCATIONS = orig


if __name__ == "__main__":
    unittest.main()
