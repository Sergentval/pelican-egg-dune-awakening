"""Regression harness for apply-world-reset.sh — THE consequential file of
the world-reset feature. Each test builds a throwaway $BASE tree, runs the
REAL hook via subprocess, and asserts on the resulting tree: no branch may
wipe without consent, brick the boot (non-zero exit), or fabricate success.
Mirrors the review sandbox that first exercised these branches by hand.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import admin_worldreset as wr

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply-world-reset.sh")
ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_worldreset.py")


class HookHarness(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="wr-hook-test-")
        self.state = os.path.join(self.base, "server", "state")
        os.makedirs(self.state)
        os.makedirs(os.path.join(self.base, "backups"))
        os.makedirs(os.path.join(self.base, "scripts"))
        shutil.copy(HOOK, os.path.join(self.base, "scripts", "apply-world-reset.sh"))
        shutil.copy(ENGINE, os.path.join(self.base, "scripts", "admin_worldreset.py"))

    def tearDown(self):
        subprocess.run(["chmod", "-R", "u+w", self.base], check=False)
        shutil.rmtree(self.base, ignore_errors=True)

    # -- fixture helpers ----------------------------------------------------

    def make_world(self, marker="live-world"):
        d = os.path.join(self.state, "pg", "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "PG_VERSION"), "w") as f:
            f.write("17\n")
        with open(os.path.join(d, "WORLD_ID"), "w") as f:
            f.write(marker)
        with open(os.path.join(self.state, "schema-loaded"), "w") as f:
            f.write("")

    def make_preserved(self, name, marker):
        d = os.path.join(self.state, name, "data")
        os.makedirs(d)
        with open(os.path.join(d, "PG_VERSION"), "w") as f:
            f.write("17\n")
        with open(os.path.join(d, "WORLD_ID"), "w") as f:
            f.write(marker)

    def make_backup(self, name="dune-x.dump", size=4096):
        with open(os.path.join(self.base, "backups", name), "wb") as f:
            f.write(b"\x00" * size)

    def arm_reset(self):
        self.make_backup()
        self.assertTrue(wr.arm_reset(self.base, "dune-x.dump", 4096, []))

    def run_hook(self):
        res = subprocess.run(["bash", os.path.join(self.base, "scripts", "apply-world-reset.sh"),
                              self.base], capture_output=True, text=True, timeout=60)
        # The boot contract: the hook may never fail the entrypoint.
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout + res.stderr

    def world_id(self, rel="pg"):
        with open(os.path.join(self.state, rel, "data", "WORLD_ID")) as f:
            return f.read()

    def preserved(self):
        return wr.preserved_dirs(self.base)

    # -- scenarios ----------------------------------------------------------

    def test_no_marker_is_a_no_op(self):
        self.make_world()
        self.run_hook()
        self.assertEqual(self.world_id(), "live-world")
        self.assertEqual(self.preserved(), [])
        self.assertIsNone(wr.read_result(self.base))

    def test_reset_moves_world_aside_with_sentinel(self):
        self.make_world()
        self.arm_reset()
        out = self.run_hook()
        self.assertIn("WORLD RESET", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "pg")))
        kept = self.preserved()
        self.assertEqual(len(kept), 1)
        self.assertEqual(self.world_id(kept[0]), "live-world")
        # prestart's schema sentinel must travel with the preserved world —
        # leaving it behind makes the fresh boot skip schema creation and die
        # in migrate-db (found on the live e2e).
        self.assertFalse(os.path.exists(os.path.join(self.state, "schema-loaded")))
        self.assertTrue(os.path.exists(os.path.join(self.state, kept[0], "schema-loaded.sentinel")))
        self.assertIsNone(wr.read_pending(self.base))
        result = wr.read_result(self.base)
        self.assertTrue(result["ok"])
        self.assertEqual(result["operation"], "reset")

    def test_reset_refuses_missing_backup(self):
        self.make_world()
        self.arm_reset()
        os.remove(os.path.join(self.base, "backups", "dune-x.dump"))
        self.run_hook()
        self.assertEqual(self.world_id(), "live-world")
        self.assertIsNone(wr.read_pending(self.base))
        self.assertFalse(wr.read_result(self.base)["ok"])

    def test_reset_refuses_size_drift(self):
        self.make_world()
        self.arm_reset()
        self.make_backup(size=17)
        self.run_hook()
        self.assertEqual(self.world_id(), "live-world")
        self.assertFalse(wr.read_result(self.base)["ok"])

    def test_rollback_swaps_and_restores_sentinel(self):
        self.make_world(marker="fresh-world")
        self.make_preserved("pg.pre-reset-20260820-100000", "old-world")
        self.assertTrue(wr.arm_rollback(self.base, "pg.pre-reset-20260820-100000"))
        out = self.run_hook()
        self.assertIn("ROLLBACK", out)
        self.assertEqual(self.world_id(), "old-world")
        parked = [d for d in os.listdir(self.state) if d.startswith("pg.rolled-back-")]
        self.assertEqual(len(parked), 1)
        self.assertEqual(self.world_id(parked[0]), "fresh-world")
        # The restored world provably has a schema: sentinel recreated; the
        # fresh world's copy travels with its parked dir.
        self.assertTrue(os.path.exists(os.path.join(self.state, "schema-loaded")))
        self.assertTrue(os.path.exists(os.path.join(self.state, parked[0], "schema-loaded.sentinel")))
        self.assertIsNone(wr.read_rollback(self.base))
        self.assertTrue(wr.read_result(self.base)["ok"])

    def test_rollback_refuses_missing_target(self):
        self.make_world(marker="fresh-world")
        self.make_preserved("pg.pre-reset-20260820-100000", "old-world")
        wr.arm_rollback(self.base, "pg.pre-reset-20260820-100000")
        shutil.rmtree(os.path.join(self.state, "pg.pre-reset-20260820-100000"))
        self.run_hook()
        self.assertEqual(self.world_id(), "fresh-world")
        self.assertIsNone(wr.read_rollback(self.base))
        self.assertFalse(wr.read_result(self.base)["ok"])

    def test_rollback_wins_over_reset_and_clears_both(self):
        self.make_world(marker="fresh-world")
        self.make_preserved("pg.pre-reset-20260820-100000", "old-world")
        self.arm_reset()
        wr.arm_rollback(self.base, "pg.pre-reset-20260820-100000")
        self.run_hook()
        self.assertEqual(self.world_id(), "old-world")
        self.assertIsNone(wr.read_pending(self.base))
        self.assertIsNone(wr.read_rollback(self.base))

    def test_missing_engine_with_marker_warns_and_keeps_everything(self):
        self.make_world()
        self.arm_reset()
        os.remove(os.path.join(self.base, "scripts", "admin_worldreset.py"))
        out = self.run_hook()
        self.assertIn("WARN", out)
        self.assertEqual(self.world_id(), "live-world")
        # The marker deliberately stays: the hook cannot trust a broken
        # engine to clear it safely.
        self.assertTrue(os.path.exists(wr.pending_path(self.base)))

    def test_reset_prunes_to_two_preserved_one_rolled_back(self):
        self.make_world()
        for ts in ("20260810-000000", "20260811-000000", "20260812-000000"):
            self.make_preserved(f"pg.pre-reset-{ts}", ts)
        for ts in ("20260810-000000", "20260811-000000"):
            self.make_preserved(f"pg.rolled-back-{ts}", ts)
        self.arm_reset()
        self.run_hook()
        kept = self.preserved()
        self.assertEqual(len(kept), 2)
        self.assertTrue(kept[0].startswith("pg.pre-reset-2026"))  # the new one is newest
        rolled = sorted(d for d in os.listdir(self.state) if d.startswith("pg.rolled-back-"))
        self.assertEqual(rolled, ["pg.rolled-back-20260811-000000"])

    def test_rollback_prunes_rolled_back_dirs(self):
        self.make_world(marker="fresh-world")
        self.make_preserved("pg.pre-reset-20260820-100000", "old-world")
        self.make_preserved("pg.rolled-back-20260810-000000", "ancient-fresh")
        wr.arm_rollback(self.base, "pg.pre-reset-20260820-100000")
        self.run_hook()
        rolled = [d for d in os.listdir(self.state) if d.startswith("pg.rolled-back-")]
        # Only the newest rolled-back dir (the one just parked) survives.
        self.assertEqual(len(rolled), 1)
        self.assertEqual(self.world_id(rolled[0]), "fresh-world")

    def test_mv_failure_clears_marker_and_boots_old_world(self):
        self.make_world()
        self.arm_reset()
        # Make server/state unwritable: the mv must fail, the world must
        # stay, the marker must be CLEARED (no infinite per-boot retry) —
        # but marker files live in state/, so clearing fails too; what MUST
        # hold: exit 0 and the world untouched.
        os.chmod(self.state, stat.S_IRUSR | stat.S_IXUSR)
        try:
            out = self.run_hook()
        finally:
            os.chmod(self.state, stat.S_IRWXU)
        self.assertIn("WARN", out)
        self.assertEqual(self.world_id(), "live-world")

    def test_result_marker_is_valid_json_after_each_run(self):
        self.make_world()
        self.arm_reset()
        self.run_hook()
        with open(wr.result_path(self.base), encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn(data["operation"], ("reset", "rollback"))


if __name__ == "__main__":
    unittest.main()
