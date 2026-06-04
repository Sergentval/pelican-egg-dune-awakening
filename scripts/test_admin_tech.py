#!/usr/bin/env python3
"""Tests for admin_tech: pure mode resolution + tech-knowledge summary."""
import unittest

import admin_tech as tech


class TestPlanTech(unittest.TestCase):
    def test_unlock_all(self):
        self.assertEqual(tech.plan_tech("unlock-all"), ("Purchased", False))

    def test_lock_all(self):
        self.assertEqual(tech.plan_tech("lock-all"), ("NotPurchased", True))

    def test_rejects_unknown(self):
        for bad in ("", "unlock", "purchase", "all", None):
            with self.assertRaises(ValueError):
                tech.plan_tech(bad)  # type: ignore[arg-type]


class TestSummarize(unittest.TestCase):
    def test_counts_purchased(self):
        entries = [
            {"ItemKey": "a", "UnlockedState": "Purchased"},
            {"ItemKey": "b", "UnlockedState": "NotPurchased"},
            {"ItemKey": "c", "UnlockedState": "Purchased"},
        ]
        self.assertEqual(tech.summarize(entries), (3, 2))

    def test_empty_and_none(self):
        self.assertEqual(tech.summarize([]), (0, 0))
        self.assertEqual(tech.summarize(None), (0, 0))

    def test_ignores_garbage_for_unlocked_but_counts_total(self):
        self.assertEqual(tech.summarize(["x", 5, {"UnlockedState": "Purchased"}]), (3, 1))


if __name__ == "__main__":
    unittest.main()
