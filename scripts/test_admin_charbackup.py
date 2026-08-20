#!/usr/bin/env python3
"""Character backup/restore file+metadata layer (issue-less port, 2026-08).

The export/import SQL lives in admin-publish.sh; this tests the pure logic:
naming, sidecar metadata, checksum extraction, listing, retention, and the
path-safety gate the HTTP layer relies on. Mechanism ported from
Icehunter/dune-admin v0.46.0 (MIT).

Run: python3 scripts/test_admin_charbackup.py
"""
import json
import os
import tempfile
import unittest

from admin_charbackup import (
    backup_name,
    checksum_of,
    is_backup,
    list_backups,
    meta_name,
    safe_backup_path,
    select_for_prune,
    write_meta,
)


class TestNaming(unittest.TestCase):
    def test_backup_name_shape(self):
        self.assertEqual(backup_name("DE0BCCAA2501BF22", "20260820-120000"),
                         "char-DE0BCCAA2501BF22-20260820-120000.json")

    def test_is_backup_accepts_canonical(self):
        self.assertTrue(is_backup("char-DE0BCCAA2501BF22-20260820-120000.json"))

    def test_is_backup_rejects_noise(self):
        for bad in ("char-DE0BCCAA2501BF22-20260820-120000.meta.json",
                    "dune-20260820-120000.dump",
                    "char-../../etc/passwd-20260820-120000.json",
                    "char-DE0BCCAA2501BF22-2026-bad.json",
                    ""):
            self.assertFalse(is_backup(bad), bad)

    def test_meta_name(self):
        self.assertEqual(meta_name("char-AA11-20260820-120000.json"),
                         "char-AA11-20260820-120000.meta.json")


class TestChecksum(unittest.TestCase):
    """The export jsonb carries '_patches_checksum'; a restore on a later game
    patch fails inside the proc, so the checksum is recorded at backup time to
    give the operator a clear pre-restore signal."""

    def test_extracts_checksum(self):
        self.assertEqual(checksum_of('{"_patches_checksum": "abc123", "x": 1}'), "abc123")

    def test_missing_checksum_is_empty_not_error(self):
        self.assertEqual(checksum_of('{"x": 1}'), "")

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            checksum_of("not json")


class TestMetaAndList(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="charbk-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def make(self, fls, ts, name="Paul", data=None):
        fn = backup_name(fls, ts)
        with open(os.path.join(self.dir, fn), "w", encoding="utf-8") as f:
            f.write(data if data is not None else '{"_patches_checksum": "ck-%s"}' % ts)
        return fn

    def test_write_meta_records_everything(self):
        fn = self.make("AA11BB22CC33DD44", "20260820-120000")
        meta = write_meta(self.dir, fn, "AA11BB22CC33DD44", "Paul", "manual", "pre-wipe")
        self.assertEqual(meta["fls"], "AA11BB22CC33DD44")
        self.assertEqual(meta["character_name"], "Paul")
        self.assertEqual(meta["action"], "manual")
        self.assertEqual(meta["reason"], "pre-wipe")
        self.assertEqual(meta["patches_checksum"], "ck-20260820-120000")
        self.assertGreater(meta["bytes"], 0)
        with open(os.path.join(self.dir, meta_name(fn)), encoding="utf-8") as f:
            self.assertEqual(json.load(f), meta)

    def test_write_meta_rejects_malformed_export(self):
        fn = self.make("AA11BB22CC33DD44", "20260820-120000", data="garbage")
        with self.assertRaises(ValueError):
            write_meta(self.dir, fn, "AA11BB22CC33DD44", "Paul", "manual", "")

    def test_list_newest_first_with_meta(self):
        f1 = self.make("AA11BB22CC33DD44", "20260820-110000")
        f2 = self.make("AA11BB22CC33DD44", "20260820-120000")
        write_meta(self.dir, f1, "AA11BB22CC33DD44", "Paul", "manual", "")
        write_meta(self.dir, f2, "AA11BB22CC33DD44", "Paul", "pre-delete", "oops")
        rows = list_backups(self.dir)
        self.assertEqual([r["file"] for r in rows], [f2, f1])
        self.assertEqual(rows[0]["action"], "pre-delete")

    def test_list_filters_by_fls(self):
        self.make("AA11BB22CC33DD44", "20260820-110000")
        self.make("EE55FF66AA77BB88", "20260820-120000")
        rows = list_backups(self.dir, fls="EE55FF66AA77BB88")
        self.assertEqual(len(rows), 1)
        self.assertIn("EE55FF66AA77BB88", rows[0]["file"])

    def test_list_tolerates_missing_meta(self):
        fn = self.make("AA11BB22CC33DD44", "20260820-110000")
        rows = list_backups(self.dir)
        self.assertEqual(rows[0]["file"], fn)
        self.assertEqual(rows[0].get("character_name", ""), "")

    def test_list_of_missing_dir_is_empty(self):
        self.assertEqual(list_backups(os.path.join(self.dir, "nope")), [])


class TestPrune(unittest.TestCase):
    """Retention is PER PLAYER: pruning after a new backup for player A must
    never delete player B's only backup."""

    def test_keeps_newest_per_player(self):
        names = [
            "char-AAAA111122223333-20260820-100000.json",
            "char-AAAA111122223333-20260820-110000.json",
            "char-AAAA111122223333-20260820-120000.json",
            "char-BBBB111122223333-20260820-090000.json",
        ]
        doomed = select_for_prune(names, keep=2)
        self.assertEqual(doomed, ["char-AAAA111122223333-20260820-100000.json"])

    def test_keep_zero_is_refused(self):
        with self.assertRaises(ValueError):
            select_for_prune([], keep=0)


class TestPathSafety(unittest.TestCase):
    """The HTTP layer passes operator-supplied file names; the gate must
    contain them to the backups dir (same policy as admin_logs)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="charbk-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_accepts_existing_canonical_file(self):
        fn = "char-AA11BB22CC33DD44-20260820-120000.json"
        open(os.path.join(self.dir, fn), "w").close()
        self.assertEqual(safe_backup_path(self.dir, fn), os.path.join(self.dir, fn))

    def test_rejects_traversal_and_noise(self):
        for bad in ("../char-AA11BB22CC33DD44-20260820-120000.json",
                    "char-AA11BB22CC33DD44-20260820-120000.meta.json",
                    "/etc/passwd", "x.json"):
            self.assertIsNone(safe_backup_path(self.dir, bad), bad)

    def test_rejects_missing_file(self):
        self.assertIsNone(safe_backup_path(self.dir, "char-AA11BB22CC33DD44-20260820-120000.json"))


if __name__ == "__main__":
    unittest.main()
