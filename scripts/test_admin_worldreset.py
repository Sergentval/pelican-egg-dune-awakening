"""Tests for admin_worldreset.py — the world-reset marker/state engine.

Pure file-state logic: durable markers under server/state/ that the boot
hook consumes, written by the arm subcommands. No DB, no network.
"""
import os
import shutil
import tempfile
import unittest

import admin_worldreset as wr


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="worldreset-test-")
        os.makedirs(os.path.join(self.base, "server", "state"), exist_ok=True)
        os.makedirs(os.path.join(self.base, "backups"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def make_backup(self, name="dune-x.dump", size=2048):
        path = os.path.join(self.base, "backups", name)
        with open(path, "wb") as f:
            f.write(b"\x00" * size)
        return path

    def test_no_marker_reads_none(self):
        self.assertIsNone(wr.read_pending(self.base))
        self.assertIsNone(wr.read_rollback(self.base))

    def test_arm_reset_round_trips(self):
        self.make_backup()
        self.assertTrue(wr.arm_reset(self.base, "dune-x.dump", 2048, ["charA"]))
        p = wr.read_pending(self.base)
        self.assertIsNotNone(p)
        self.assertEqual(p["backup_file"], "dune-x.dump")
        self.assertEqual(p["backup_bytes"], 2048)
        self.assertEqual(p["char_backups"], ["charA"])
        self.assertIn("requested_at", p)

    def test_arm_reset_refuses_missing_backup(self):
        self.assertFalse(wr.arm_reset(self.base, "nope.dump", 2048, []))
        self.assertIsNone(wr.read_pending(self.base))

    def test_arm_reset_refuses_size_mismatch(self):
        self.make_backup(size=100)
        self.assertFalse(wr.arm_reset(self.base, "dune-x.dump", 2048, []))
        self.assertIsNone(wr.read_pending(self.base))

    def test_clear_pending(self):
        self.make_backup()
        wr.arm_reset(self.base, "dune-x.dump", 2048, [])
        self.assertTrue(wr.clear_pending(self.base))
        self.assertIsNone(wr.read_pending(self.base))

    def test_clear_pending_when_absent_is_ok(self):
        self.assertTrue(wr.clear_pending(self.base))

    def test_malformed_marker_reads_none(self):
        with open(wr.pending_path(self.base), "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertIsNone(wr.read_pending(self.base))

    def test_verify_pending_ok_and_failures(self):
        self.make_backup()
        wr.arm_reset(self.base, "dune-x.dump", 2048, [])
        pending = wr.read_pending(self.base)
        self.assertEqual(wr.verify_pending(self.base, pending), "")
        os.remove(os.path.join(self.base, "backups", "dune-x.dump"))
        self.assertIn("missing", wr.verify_pending(self.base, pending))
        self.make_backup(size=7)
        self.assertIn("size", wr.verify_pending(self.base, pending))

    def test_backup_file_name_is_contained(self):
        # A path-traversal name must never validate: the boot hook joins it
        # under backups/ and trusts the result exists.
        self.make_backup()
        self.assertFalse(wr.arm_reset(self.base, "../server/state/x", 2048, []))
        self.assertFalse(wr.arm_reset(self.base, "/etc/passwd", 2048, []))
        self.assertFalse(wr.arm_reset(self.base, "", 2048, []))


class RollbackMarkerTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="worldreset-test-")
        os.makedirs(os.path.join(self.base, "server", "state"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def make_preserved(self, name="pg.pre-reset-20260820-120000"):
        d = os.path.join(self.base, "server", "state", name)
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        with open(os.path.join(d, "data", "PG_VERSION"), "w") as f:
            f.write("17\n")
        return name

    def test_preserved_dirs_listing_newest_first(self):
        self.make_preserved("pg.pre-reset-20260820-110000")
        self.make_preserved("pg.pre-reset-20260820-120000")
        self.assertEqual(wr.preserved_dirs(self.base),
                         ["pg.pre-reset-20260820-120000",
                          "pg.pre-reset-20260820-110000"])

    def test_preserved_ignores_non_matching_and_invalid(self):
        self.make_preserved("pg.pre-reset-20260820-110000")
        os.makedirs(os.path.join(self.base, "server", "state", "pg.pre-reset-junk"))
        os.makedirs(os.path.join(self.base, "server", "state", "pg"))
        self.assertEqual(wr.preserved_dirs(self.base),
                         ["pg.pre-reset-20260820-110000"])

    def test_arm_rollback_targets_existing_dir(self):
        name = self.make_preserved()
        self.assertTrue(wr.arm_rollback(self.base, name))
        r = wr.read_rollback(self.base)
        self.assertEqual(r["restore_dir"], name)

    def test_arm_rollback_refuses_missing_or_traversal(self):
        self.assertFalse(wr.arm_rollback(self.base, "pg.pre-reset-20990101-000000"))
        self.assertFalse(wr.arm_rollback(self.base, "../../etc"))
        self.assertFalse(wr.arm_rollback(self.base, "pg"))
        self.assertIsNone(wr.read_rollback(self.base))

    def test_clear_rollback(self):
        name = self.make_preserved()
        wr.arm_rollback(self.base, name)
        self.assertTrue(wr.clear_rollback(self.base))
        self.assertIsNone(wr.read_rollback(self.base))


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="worldreset-test-")
        os.makedirs(os.path.join(self.base, "server", "state"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_result_round_trip(self):
        self.assertIsNone(wr.read_result(self.base))
        self.assertTrue(wr.write_result(self.base, "reset", True, "fresh world",
                                        preserved="pg.pre-reset-x"))
        r = wr.read_result(self.base)
        self.assertEqual(r["operation"], "reset")
        self.assertTrue(r["ok"])
        self.assertEqual(r["preserved"], "pg.pre-reset-x")
        self.assertIn("at", r)

    def test_result_failure_shape(self):
        wr.write_result(self.base, "rollback", False, "target missing")
        r = wr.read_result(self.base)
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "target missing")


if __name__ == "__main__":
    unittest.main()
