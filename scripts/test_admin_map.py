#!/usr/bin/env python3
"""Tests for admin_map (Phase 2): map-key validation + marker CSV parsing."""
import unittest

from admin_map import MAP_KEYS, parse_markers, validate_map_key

HEADER = "id,name,online,partition,fls,x,y,z"


class TestValidateMapKey(unittest.TestCase):
    def test_known(self):
        for k in ("HaggaBasin", "DeepDesert", "Arrakeen", "HarkoVillage"):
            self.assertTrue(validate_map_key(k), k)
            self.assertIn(k, MAP_KEYS)

    def test_unknown(self):
        for k in ("", "hagga", "Survival_1", "; DROP", None, 5):
            self.assertFalse(validate_map_key(k))


class TestParseMarkers(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_markers(""), [])
        self.assertEqual(parse_markers(HEADER), [])  # header only, no rows

    def test_basic(self):
        text = HEADER + "\n112,Sergentval,Online,1,DE0BCCAA2501BF22,193795.35,3419.43,2628.8"
        m = parse_markers(text)
        self.assertEqual(len(m), 1)
        r = m[0]
        self.assertEqual(r["id"], "112")
        self.assertEqual(r["name"], "Sergentval")
        self.assertTrue(r["online"])
        self.assertEqual(r["partition"], 1)
        self.assertEqual(r["fls"], "DE0BCCAA2501BF22")
        self.assertAlmostEqual(r["x"], 193795.35, places=2)
        self.assertAlmostEqual(r["y"], 3419.43, places=2)
        self.assertEqual(r["kind"], "player")

    def test_offline_and_blank_name(self):
        r = parse_markers(HEADER + "\n9,,Offline,0,,0,0,0")[0]
        self.assertFalse(r["online"])
        self.assertEqual(r["name"], "Unknown")

    def test_name_with_comma_is_quoted(self):
        r = parse_markers(HEADER + '\n7,"Foo, Bar",Online,2,ABC,1.5,2.5,3.5')[0]
        self.assertEqual(r["name"], "Foo, Bar")
        self.assertEqual(r["partition"], 2)

    def test_bad_numbers_default_zero(self):
        r = parse_markers(HEADER + "\n1,X,Online,notanint,F,notafloat,2,3")[0]
        self.assertEqual(r["partition"], 0)
        self.assertEqual(r["x"], 0.0)
        self.assertEqual(r["y"], 2.0)

    def test_non_finite_coords_are_neutralised(self):
        """float() accepts nan/inf, json.dumps then emits bare NaN/Infinity and
        JSON.parse rejects the whole payload — one bad row blanked the map."""
        r = parse_markers(HEADER + "\n1,X,Online,1,F,nan,inf,-inf")[0]
        self.assertEqual((r["x"], r["y"], r["z"]), (0.0, 0.0, 0.0))

    def test_infinite_partition_does_not_raise(self):
        # int(float("inf")) raises OverflowError, which is not a ValueError.
        r = parse_markers(HEADER + "\n1,X,Online,inf,F,1,2,3")[0]
        self.assertEqual(r["partition"], 0)
        self.assertEqual(r["x"], 1.0)

    def test_blank_coords_skipped(self):
        # null transform.location -> blank x/y -> skip (no phantom origin marker)
        text = HEADER + "\n5,Ghost,Online,1,ABC,,,\n6,Real,Online,1,DEF,10,20,0"
        m = parse_markers(text)
        self.assertEqual([r["name"] for r in m], ["Real"])  # Ghost dropped


if __name__ == "__main__":
    unittest.main()
