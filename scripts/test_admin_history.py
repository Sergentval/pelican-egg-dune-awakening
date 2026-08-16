#!/usr/bin/env python3
"""Tests for the persisted command audit (admin_history).

Run: python3 scripts/test_admin_history.py
"""
import os
import tempfile
import threading
import time
import unittest

import admin_history as hist


class Filtering(unittest.TestCase):
    """Read-only polling is what made the old trail useless: MapTab polls
    every 4s, so 200 slots of map-markers evicted every real action."""

    def test_polling_traffic_is_skipped(self):
        for argv in (["map-markers", "Survival_1"], ["players"], ["server-status"],
                     ["db-sample", "actors"], ["db-sql", "select 1"]):
            with self.subTest(argv=argv):
                self.assertFalse(hist.is_recordable(argv))

    def test_actions_are_recorded(self):
        for argv in (["broadcast", "t", "b"], ["kick", "x"], ["give", "x", "y"],
                     ["shutdown", "restart"], ["account-delete", "x", "y", "z"]):
            with self.subTest(argv=argv):
                self.assertTrue(hist.is_recordable(argv))

    def test_db_backup_is_an_action_not_a_read(self):
        self.assertTrue(hist.is_recordable(["db-backup"]))
        self.assertFalse(hist.is_recordable(["db-backup-list"]))

    def test_unknown_subcommands_default_to_recorded(self):
        # Forgetting to classify a new subcommand should make the trail
        # noisier, never make it silently incomplete.
        self.assertTrue(hist.is_recordable(["some-future-subcommand"]))

    def test_empty_argv(self):
        self.assertFalse(hist.is_recordable([]))
        self.assertFalse(hist.is_recordable(None))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name

    def entry(self, *argv, ok=True, out="", err="", ts=None):
        return {"ts": ts or int(time.time()), "argv": list(argv), "ok": ok,
                "exit_code": 0 if ok else 1, "stdout": out, "stderr": err}

    def test_roundtrip_preserves_the_shape_the_spa_renders(self):
        self.assertTrue(hist.record(self.base, self.entry("broadcast", "Hi", "There",
                                                          out="publish=ok")))
        entries, total = hist.recent(self.base)
        self.assertEqual(total, 1)
        e = entries[0]
        self.assertEqual(e["argv"], ["broadcast", "Hi", "There"])
        self.assertTrue(e["ok"])
        self.assertEqual(e["stdout"], "publish=ok")
        self.assertIsInstance(e["ts"], int)

    def test_skipped_entries_are_not_stored(self):
        self.assertFalse(hist.record(self.base, self.entry("map-markers", "Survival_1")))
        self.assertEqual(hist.recent(self.base), ([], 0))

    def test_survives_a_restart(self):
        """The whole point: a new process sees what the old one recorded."""
        hist.record(self.base, self.entry("kick", "player-1"))
        entries, total = hist.recent(self.base)  # simulates a fresh process
        self.assertEqual(total, 1)
        self.assertEqual(entries[0]["argv"], ["kick", "player-1"])

    def test_lands_under_server_state_so_a_reinstall_keeps_it(self):
        hist.record(self.base, self.entry("kick", "x"))
        self.assertTrue(os.path.isfile(os.path.join(self.base, "server", "state",
                                                    "admin-history.db")))

    def test_oldest_first_like_the_deque_it_replaces(self):
        for i in range(5):
            hist.record(self.base, self.entry("kick", f"p{i}", ts=1000 + i))
        entries, _ = hist.recent(self.base)
        self.assertEqual([e["argv"][1] for e in entries], ["p0", "p1", "p2", "p3", "p4"])

    def test_limit_returns_the_newest(self):
        for i in range(10):
            hist.record(self.base, self.entry("kick", f"p{i}", ts=1000 + i))
        entries, total = hist.recent(self.base, limit=3)
        self.assertEqual(total, 10)
        self.assertEqual([e["argv"][1] for e in entries], ["p7", "p8", "p9"])

    def test_pruning_keeps_the_newest(self):
        for i in range(12):
            hist.record(self.base, self.entry("kick", f"p{i}", ts=1000 + i), keep=5)
        entries, total = hist.recent(self.base, limit=50)
        self.assertEqual(total, 5)
        self.assertEqual([e["argv"][1] for e in entries],
                         ["p7", "p8", "p9", "p10", "p11"])

    def test_huge_output_is_clipped(self):
        hist.record(self.base, self.entry("db-backup", out="x" * 50_000))
        entries, _ = hist.recent(self.base)
        self.assertLess(len(entries[0]["stdout"]), hist.MAX_STREAM_CHARS + 100)
        self.assertIn("truncated", entries[0]["stdout"])

    def test_failures_are_recorded_as_failures(self):
        hist.record(self.base, self.entry("kick", "ghost", ok=False, err="no such player"))
        entries, _ = hist.recent(self.base)
        self.assertFalse(entries[0]["ok"])
        self.assertEqual(entries[0]["stderr"], "no such player")

    def test_note_marks_a_restart_in_the_trail(self):
        hist.note(self.base, "(admin panel started)", "mode=UI")
        entries, _ = hist.recent(self.base)
        self.assertEqual(entries[0]["argv"], ["(admin panel started)"])
        self.assertEqual(entries[0]["stdout"], "mode=UI")

    def test_an_unwritable_trail_never_breaks_a_command(self):
        # Storage failure must be swallowed: losing an audit line is bad,
        # failing the admin action that produced it is worse.
        blocked = os.path.join(self.base, "blocked")
        open(blocked, "w").close()          # a FILE where a dir must go
        self.assertFalse(hist.record(blocked, self.entry("kick", "x")))
        self.assertEqual(hist.recent(blocked), ([], 0))

    def test_corrupt_database_degrades_quietly(self):
        path = hist.history_path(self.base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("this is not a database")
        self.assertFalse(hist.record(self.base, self.entry("kick", "x")))
        self.assertEqual(hist.recent(self.base), ([], 0))

    def test_concurrent_writers(self):
        """The listener is threaded; two admin actions can land together."""
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    hist.record(self.base, self.entry("kick", f"w{n}-{i}"))
            except BaseException as exc:  # noqa: BLE001 — the assertion is "nothing escapes"
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        _, total = hist.recent(self.base)
        self.assertEqual(total, 120)


if __name__ == "__main__":
    unittest.main()
