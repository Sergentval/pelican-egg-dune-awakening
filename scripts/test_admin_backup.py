#!/usr/bin/env python3
"""Tests for admin_backup retention/naming (scheduler Phase 1)."""
import os
import tempfile
import unittest

import admin_backup as ab


class TestNaming(unittest.TestCase):
    def test_backup_name_and_match(self):
        n = ab.backup_name("20260603-041500")
        self.assertEqual(n, "dune-20260603-041500.dump")
        self.assertTrue(ab.is_backup(n))

    def test_rejects_non_backups(self):
        for bad in ("dune.dump", "dune-2026.dump", "notes.txt", "dune-20260603-0415.dump", ""):
            self.assertFalse(ab.is_backup(bad), bad)


class TestSelectForPrune(unittest.TestCase):
    NAMES = [
        "dune-20260601-010000.dump",
        "dune-20260602-010000.dump",
        "dune-20260603-010000.dump",
        "dune-20260603-020000.dump",
        "README.md",  # ignored
    ]

    def test_keep_more_than_have(self):
        self.assertEqual(ab.select_for_prune(self.NAMES, 10), [])

    def test_keep_subset_drops_oldest(self):
        doomed = ab.select_for_prune(self.NAMES, 2)
        self.assertEqual(doomed, ["dune-20260601-010000.dump", "dune-20260602-010000.dump"])

    def test_keep_zero_drops_all(self):
        self.assertEqual(len(ab.select_for_prune(self.NAMES, 0)), 4)

    def test_ignores_non_backup_names(self):
        self.assertNotIn("README.md", ab.select_for_prune(self.NAMES, 0))


class TestPruneDir(unittest.TestCase):
    def test_prune_dir_deletes_oldest(self):
        d = tempfile.mkdtemp()
        for n in TestSelectForPrune.NAMES:
            open(os.path.join(d, n), "w").close()
        deleted = ab._prune_dir(d, 2)
        self.assertEqual(sorted(deleted), ["dune-20260601-010000.dump", "dune-20260602-010000.dump"])
        left = sorted(x for x in os.listdir(d) if ab.is_backup(x))
        self.assertEqual(left, ["dune-20260603-010000.dump", "dune-20260603-020000.dump"])
        self.assertTrue(os.path.isfile(os.path.join(d, "README.md")))  # untouched


if __name__ == "__main__":
    unittest.main()
