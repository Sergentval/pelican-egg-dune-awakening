#!/usr/bin/env python3
"""Unit tests for admin_status (Phase 4 — live server status grid merge logic)."""
import unittest

from admin_status import parse_player_counts, merge_status


class TestParsePlayerCounts(unittest.TestCase):
    def test_normal_with_header(self):
        csv = "map,players\nSurvival_1,3\nOvermap,1\nDeepDesert_1,0\n"
        self.assertEqual(
            parse_player_counts(csv),
            {"Survival_1": 3, "Overmap": 1, "DeepDesert_1": 0},
        )

    def test_skips_header_blank_and_crlf(self):
        csv = "map,players\r\n\r\nSurvival_1,2\r\n   \nOvermap,5\r\n"
        self.assertEqual(parse_player_counts(csv), {"Survival_1": 2, "Overmap": 5})

    def test_skips_non_numeric_count(self):
        csv = "map,players\nSurvival_1,notanumber\nOvermap,4\n"
        self.assertEqual(parse_player_counts(csv), {"Overmap": 4})

    def test_empty(self):
        self.assertEqual(parse_player_counts(""), {})
        self.assertEqual(parse_player_counts("map,players\n"), {})


class TestMergeStatus(unittest.TestCase):
    def _mock(self):
        return {
            "uptimeSeconds": 120,
            "reconcile": {"enabled": True},
            "pool": {"size": 25, "used": 8, "free": 17},
            "maps": [
                {"map": "Survival_1", "desired": 1, "current": 1, "status": "healthy"},
                {"map": "Overmap", "desired": 1, "current": 1, "status": "healthy"},
                {"map": "DeepDesert_1", "desired": 1, "current": 1, "status": "healthy"},
            ],
        }

    def test_merges_counts_and_defaults_zero(self):
        grid = merge_status(self._mock(), {"Survival_1": 3, "Overmap": 1})
        by = {r["map"]: r for r in grid["maps"]}
        self.assertEqual(by["Survival_1"]["players"], 3)
        self.assertEqual(by["Overmap"]["players"], 1)
        self.assertEqual(by["DeepDesert_1"]["players"], 0)  # no count -> 0
        # instance-status fields preserved
        self.assertEqual(by["Survival_1"]["status"], "healthy")

    def test_totals(self):
        grid = merge_status(self._mock(), {"Survival_1": 3, "Overmap": 1})
        self.assertEqual(grid["totalPlayers"], 4)
        self.assertEqual(grid["totalServers"], 3)

    def test_orphan_player_map_included_as_unknown(self):
        # A map with players that mock-k8s doesn't list must NOT be dropped.
        grid = merge_status(self._mock(), {"Survival_1": 1, "SH_Arrakeen": 2})
        by = {r["map"]: r for r in grid["maps"]}
        self.assertIn("SH_Arrakeen", by)
        self.assertEqual(by["SH_Arrakeen"]["players"], 2)
        self.assertEqual(by["SH_Arrakeen"]["status"], "unknown")
        self.assertEqual(grid["totalPlayers"], 3)
        self.assertEqual(grid["totalServers"], 4)  # 3 mock + 1 orphan

    def test_does_not_mutate_inputs(self):
        mock = self._mock()
        counts = {"Survival_1": 3}
        merge_status(mock, counts)
        self.assertNotIn("players", mock["maps"][0])
        self.assertEqual(counts, {"Survival_1": 3})

    def test_tolerates_empty_inputs(self):
        self.assertEqual(merge_status({}, {})["maps"], [])
        self.assertEqual(merge_status(None, None)["totalPlayers"], 0)
        grid = merge_status({"maps": [{"map": "X"}]}, None)
        self.assertEqual(grid["maps"][0]["players"], 0)


if __name__ == "__main__":
    unittest.main()
