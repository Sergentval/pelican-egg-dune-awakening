#!/usr/bin/env python3
"""Live-map projection + Deep Desert sector grid — calibration regression (#116).

Why this test exists in Python, driving TypeScript
--------------------------------------------------
The projection lives in `web/src/mapProjection.ts` and there is no JS test
runner in this repo (`web/package.json` has dev/build/preview only). Rather
than invent a toolchain, this test uses what is already here: the repo's own
`typescript` devDependency to transpile that ONE dependency-free module, and
node to run it. It is wired into the existing offline suite — TESTING.md's
`for t in scripts/test_*.py; do python3 "$t" || break; done` picks it up with
no change. It skips cleanly (not fails) where `web/node_modules` or node is
absent, e.g. on a machine that only runs the server side.

It asserts against the REAL shipped code, not a Python re-implementation of
it — a re-implementation would agree with a wrong projection just as happily.

What it pins
------------
The Deep Desert bounds are game-authoritative: the UE5 dedicated server prints
its own world box at boot under `LogDuneWorldPartitioner`
(`Min=(X=-1270000 Y=-1270000) Max=(X=1168400 Y=1168400)`, identical on all four
DD dimensions, Dreamworld revision 1973075), corroborated by
cdn.th.gl's tiles.json for the very image we ship. The two probes below are the
independent check on them: live coordinates from issue #116, each paired with
the sector its reporter read off the in-game map.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")
SRC = os.path.join(WEB, "src", "mapProjection.ts")
TSC = os.path.join(WEB, "node_modules", ".bin", "tsc")

# --- ground truth ------------------------------------------------------
# Two live player positions from issue #116 (@iamc0ke), each with the sector
# read off the in-game map. Independent points, different corners of the map.
P1 = (516429, -1009962, "I7")
P2 = (1127612, 1077779, "A9")
# Every check below iterates GROUND_TRUTH, so a third observation is a one-line
# addition here and nowhere else. Format: (world x, world y, sector read in game).
GROUND_TRUTH = (P1, P2)

# The Deep Desert box as shipped before #116: a round-number guess inherited
# from dune-admin that was never calibrated. Pinned here so this test proves
# it is not vacuous — it must reject the old value on P1.
OLD_DD = {"key": "OldDeepDesert", "label": "old", "image": "deepdesert.webp",
          "minX": -1300000, "maxX": 1200000, "minY": -1300000, "maxY": 1200000,
          "flipY": True}

# The numbers the game itself reports, from LogDuneWorldPartitioner in the UE5
# dedicated server's own boot log. Any change to these must come with a new
# source, not a new fit.
DD_BOX = {"minX": -1270000, "maxX": 1168400, "minY": -1270000, "maxY": 1168400}
HAGGA_BOX = {"minX": -457200, "maxX": 355600, "minY": -457200, "maxY": 355600}

# Maps whose shipped image is a truncated crop, so no box can project onto it
# correctly. They must keep declaring that rather than looking authoritative.
UNCALIBRATED = ("Arrakeen", "HarkoVillage")

HARNESS = r'''
import { MAPS, worldToPct, pctToWorld, sectorForPct, sectorForWorld } from "./mapProjection.js";
import { readFileSync } from "node:fs";

// JSON cannot carry NaN/Infinity, so they arrive as strings.
const num = (v) => (typeof v === "string" ? Number(v) : v);
const cfgOf = (c) => (typeof c === "string" ? MAPS.find((m) => m.key === c) : c);

const calls = JSON.parse(readFileSync(0, "utf8"));
const out = calls.map((c) => {
  switch (c.fn) {
    case "maps": return MAPS;
    case "worldToPct": return worldToPct(num(c.x), num(c.y), cfgOf(c.cfg));
    case "pctToWorld": return pctToWorld(num(c.clientX), num(c.clientY), c.rect, cfgOf(c.cfg));
    case "sectorForPct": return sectorForPct(num(c.left), num(c.top));
    case "sectorForWorld": return sectorForWorld(num(c.x), num(c.y), cfgOf(c.cfg));
    default: throw new Error("unknown fn " + c.fn);
  }
});
process.stdout.write(JSON.stringify(out));
'''


class ProjectionHarness:
    """Compiles web/src/mapProjection.ts once, then evaluates batches of calls."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="mapproj-")
        subprocess.run(
            [TSC, SRC, "--outDir", self.dir, "--module", "es2020",
             "--target", "es2020", "--moduleResolution", "bundler", "--strict"],
            check=True, capture_output=True, text=True, cwd=WEB)
        self.entry = os.path.join(self.dir, "harness.mjs")
        with open(self.entry, "w", encoding="utf-8") as f:
            f.write(HARNESS)

    def __call__(self, calls):
        proc = subprocess.run([shutil.which("node"), self.entry],
                              input=json.dumps(calls), capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("harness failed: " + proc.stderr.strip())
        return json.loads(proc.stdout)

    def one(self, call):
        return self([call])[0]

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


HARNESS_SINGLETON = None


def setUpModule():
    global HARNESS_SINGLETON
    if not os.path.exists(TSC):
        raise unittest.SkipTest("web/node_modules missing — run `npm ci` in web/ to include this test")
    if not shutil.which("node"):
        raise unittest.SkipTest("node not on PATH")
    HARNESS_SINGLETON = ProjectionHarness()


def tearDownModule():
    if HARNESS_SINGLETON:
        HARNESS_SINGLETON.close()


class TestDeepDesertCalibration(unittest.TestCase):
    """The heart of #116: the two reported points must land in their sectors."""

    def test_bounds_are_the_game_authoritative_box(self):
        maps = HARNESS_SINGLETON.one({"fn": "maps"})
        dd = next(m for m in maps if m["key"] == "DeepDesert")
        for k, v in DD_BOX.items():
            self.assertEqual(dd[k], v, f"DeepDesert {k} changed — was it re-sourced or re-guessed?")
        self.assertTrue(dd["flipY"], "world Y grows south; the image is north-up")
        self.assertEqual(dd["maxX"] - dd["minX"], dd["maxY"] - dd["minY"],
                         "the Deep Desert landscape is square")

    def test_ground_truth_points_land_in_their_reported_sectors(self):
        for x, y, want in GROUND_TRUTH:
            got = HARNESS_SINGLETON.one(
                {"fn": "sectorForWorld", "x": x, "y": y, "cfg": "DeepDesert"})
            self.assertEqual(got, want, f"({x}, {y}) should read {want} in game")

    def test_ground_truth_points_are_inside_the_map(self):
        for x, y, _ in GROUND_TRUTH:
            p = HARNESS_SINGLETON.one(
                {"fn": "worldToPct", "x": x, "y": y, "cfg": "DeepDesert"})
            self.assertTrue(p["inBounds"], f"({x}, {y}) fell outside the declared bounds")

    def test_old_guessed_bounds_are_rejected(self):
        """Proof this test is not vacuous: the pre-#116 box mislabels P1."""
        x, y, want = P1
        got = HARNESS_SINGLETON.one({"fn": "sectorForWorld", "x": x, "y": y, "cfg": OLD_DD})
        self.assertEqual(got, "H7")          # the bug as reported
        self.assertNotEqual(got, want)

    def test_ground_truth_points_are_not_on_a_cell_boundary(self):
        """Each point sits strictly inside its cell, so the labels are decisions.

        P1's row margin is the tight one (~0.04 of a sector); it is the reason a
        third ground-truth point near a row line would still be worth having.
        """
        for x, y, label in GROUND_TRUTH:
            p = HARNESS_SINGLETON.one(
                {"fn": "worldToPct", "x": x, "y": y, "cfg": "DeepDesert"})
            for axis, pct in (("column", p["left"]), ("row", p["top"])):
                frac = (pct / 100.0) * 9.0
                margin = min(frac % 1.0, 1.0 - frac % 1.0)
                self.assertGreater(margin, 0.01,
                                   f"{label}: {axis} sits on a grid line ({margin:.4f} sector)")


class TestOtherMapCalibration(unittest.TestCase):
    """#116 follow-through: the same server log calibrates every map, and the
    two we cannot fix must say so instead of looking authoritative."""

    def test_hagga_bounds_are_the_game_authoritative_box(self):
        maps = HARNESS_SINGLETON.one({"fn": "maps"})
        hagga = next(m for m in maps if m["key"] == "HaggaBasin")
        for k, v in HAGGA_BOX.items():
            self.assertEqual(hagga[k], v, f"HaggaBasin {k} changed — new source, or a new fit?")
        self.assertEqual(hagga["maxX"] - hagga["minX"], hagga["maxY"] - hagga["minY"],
                         "the Hagga Basin landscape is square, like its 512x512 image")

    def test_hagga_is_no_longer_the_old_hand_fit(self):
        """Proof this is not vacuous: the superseded eyeballed box was not square."""
        maps = HARNESS_SINGLETON.one({"fn": "maps"})
        hagga = next(m for m in maps if m["key"] == "HaggaBasin")
        self.assertNotEqual(hagga["minX"], -437871, "reverted to the pre-#116 hand fit")

    def test_truncated_image_maps_declare_themselves_uncalibrated(self):
        maps = HARNESS_SINGLETON.one({"fn": "maps"})
        for key in UNCALIBRATED:
            m = next(x for x in maps if x["key"] == key)
            self.assertTrue(
                m.get("uncalibrated"),
                f"{key} ships a truncated map image; dropping the flag would let the "
                f"panel present an approximate dot as an exact one")

    def test_calibrated_maps_carry_no_warning(self):
        maps = HARNESS_SINGLETON.one({"fn": "maps"})
        for key in ("HaggaBasin", "DeepDesert"):
            m = next(x for x in maps if x["key"] == key)
            self.assertIsNone(m.get("uncalibrated", None),
                              f"{key} is calibrated from the game's own box")


class TestGridOrientation(unittest.TestCase):
    """Row I is north, row A is south (the arrival row); col 1 west, col 9 east."""

    def test_corners(self):
        b = DD_BOX
        mid_x = (b["minX"] + b["maxX"]) / 2
        mid_y = (b["minY"] + b["maxY"]) / 2
        cases = [
            (mid_x, b["minY"] + 1, "I5"),      # far north
            (mid_x, b["maxY"] - 1, "A5"),      # far south
            (b["minX"] + 1, mid_y, "E1"),      # far west
            (b["maxX"] - 1, mid_y, "E9"),      # far east
            (b["minX"] + 1, b["minY"] + 1, "I1"),
            (b["maxX"] - 1, b["maxY"] - 1, "A9"),
        ]
        got = HARNESS_SINGLETON([
            {"fn": "sectorForWorld", "x": x, "y": y, "cfg": "DeepDesert"} for x, y, _ in cases])
        self.assertEqual(got, [want for _, _, want in cases])

    def test_row_letter_tracks_southward_y(self):
        """Walking south through the map must walk I -> A, once each."""
        b = DD_BOX
        span = b["maxY"] - b["minY"]
        rows = HARNESS_SINGLETON([
            {"fn": "sectorForWorld", "cfg": "DeepDesert",
             "x": (b["minX"] + b["maxX"]) / 2,
             "y": b["minY"] + span * (i + 0.5) / 9} for i in range(9)])
        self.assertEqual([r[0] for r in rows], list("IHGFEDCBA"))


class TestSectorForPct(unittest.TestCase):
    def test_extremes(self):
        got = HARNESS_SINGLETON([
            {"fn": "sectorForPct", "left": 0, "top": 0},
            {"fn": "sectorForPct", "left": 100, "top": 100},
            {"fn": "sectorForPct", "left": 100, "top": 0},
            {"fn": "sectorForPct", "left": 0, "top": 100},
        ])
        self.assertEqual(got, ["I1", "A9", "I9", "A1"])

    def test_exact_grid_lines_label_the_cell_the_line_opens(self):
        """((1/3)*100)/100*9 is 2.999999999999999 and ((2/3)*100)/100*9 is
        5.999999999999998 in IEEE754 — so without the epsilon in cellIndex a
        point exactly on the 3rd or 6th line was labelled one cell west (or
        north) of the line the SVG draws. ((1/3)*9 on its own is exactly 3; it
        is the round-trip through percent that loses the bit.)"""
        got = HARNESS_SINGLETON([
            {"fn": "sectorForPct", "left": (i / 9.0) * 100, "top": (i / 9.0) * 100}
            for i in range(10)])
        self.assertEqual(got, ["I1", "H2", "G3", "F4", "E5", "D6", "C7", "B8", "A9", "A9"])

    def test_outside_returns_null(self):
        got = HARNESS_SINGLETON([
            {"fn": "sectorForPct", "left": -0.0001, "top": 50},
            {"fn": "sectorForPct", "left": 50, "top": 100.0001},
            {"fn": "sectorForPct", "left": "NaN", "top": 50},
        ])
        self.assertEqual(got, [None, None, None])


class TestWorldToPct(unittest.TestCase):
    def test_out_of_bounds_is_clamped_but_flagged(self):
        """The UI pins a stray coord to the edge; inBounds is the only signal
        that the sector it then reports is not a real reading."""
        p = HARNESS_SINGLETON.one(
            {"fn": "worldToPct", "x": 5000000, "y": 5000000, "cfg": "DeepDesert"})
        self.assertFalse(p["inBounds"])
        self.assertEqual((p["left"], p["top"]), (100.0, 100.0))

    def test_non_finite_returns_null(self):
        got = HARNESS_SINGLETON([
            {"fn": "worldToPct", "x": "NaN", "y": 0, "cfg": "DeepDesert"},
            {"fn": "worldToPct", "x": 0, "y": "Infinity", "cfg": "DeepDesert"},
        ])
        self.assertEqual(got, [None, None])

    def test_every_map_is_north_up(self):
        maps = HARNESS_SINGLETON.one({"fn": "maps"})
        self.assertEqual([m["key"] for m in maps],
                         ["HaggaBasin", "DeepDesert", "Arrakeen", "HarkoVillage"])
        for m in maps:
            self.assertTrue(m.get("flipY"), f"{m['key']} lost flipY — world Y grows south")


class TestPctToWorld(unittest.TestCase):
    RECT = {"left": 20.0, "top": 40.0, "width": 760.0, "height": 760.0}

    def test_round_trips_worldToPct(self):
        pts = [(0, 0), *[(x, y) for x, y, _ in GROUND_TRUTH], (-1269999, 1168399)]
        proj = HARNESS_SINGLETON([
            {"fn": "worldToPct", "x": x, "y": y, "cfg": "DeepDesert"} for x, y in pts])
        back = HARNESS_SINGLETON([
            {"fn": "pctToWorld", "cfg": "DeepDesert", "rect": self.RECT,
             "clientX": self.RECT["left"] + p["left"] / 100.0 * self.RECT["width"],
             "clientY": self.RECT["top"] + p["top"] / 100.0 * self.RECT["height"]}
            for p in proj])
        for (x, y), got in zip(pts, back):
            self.assertLessEqual(abs(got["x"] - x), 1, f"x round-trip for {(x, y)}")
            self.assertLessEqual(abs(got["y"] - y), 1, f"y round-trip for {(x, y)}")

    def test_zero_sized_rect_returns_null(self):
        """An <img> that has not decoded reports height 0; dividing by it used
        to yield maxY and save a plausible-looking southern coordinate."""
        rect = dict(self.RECT, height=0.0)
        got = HARNESS_SINGLETON.one({"fn": "pctToWorld", "cfg": "DeepDesert", "rect": rect,
                                     "clientX": 100, "clientY": 40})
        self.assertIsNone(got)

    def test_click_outside_the_image_returns_null(self):
        """The pointer handler sits on the viewport, which is bigger than the
        image — clamping turned every mis-click into a corner location."""
        got = HARNESS_SINGLETON([
            {"fn": "pctToWorld", "cfg": "DeepDesert", "rect": self.RECT, "clientX": 10, "clientY": 400},
            {"fn": "pctToWorld", "cfg": "DeepDesert", "rect": self.RECT, "clientX": 400, "clientY": 10},
            {"fn": "pctToWorld", "cfg": "DeepDesert", "rect": self.RECT, "clientX": 1000, "clientY": 400},
        ])
        self.assertEqual(got, [None, None, None])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
