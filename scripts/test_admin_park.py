#!/usr/bin/env python3
"""Tests for admin_park — the parked-sietch id set (reboot-durable "pause but keep data").

The FAIL-SAFE is the safety boundary: a missing/corrupt/garbage file MUST read as the
EMPTY set (nothing parked). The parked set GATES boot spawning — a read that wrongly
returned ids would SKIP (and thus effectively kill) live sietches at boot. So every
read-error path must degrade to "spawn everything", never "skip something"."""
import json
import os
import shutil
import tempfile
import unittest

import admin_park as ap


class TestAdminPark(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def _p(self):
        return ap.park_path(self.base)

    def test_empty_when_no_file(self):
        self.assertEqual(ap.parked_ids(self.base), set())

    def test_park_adds_and_persists_as_int_array(self):
        self.assertEqual(ap.park(self.base, 200), {200})
        self.assertEqual(ap.parked_ids(self.base), {200})
        with open(self._p()) as f:
            self.assertEqual(sorted(json.load(f)), [200])

    def test_park_idempotent(self):
        ap.park(self.base, 200)
        ap.park(self.base, 200)
        self.assertEqual(ap.parked_ids(self.base), {200})

    def test_park_pid_coerced_to_int(self):
        ap.park(self.base, "200")  # string pid (from a CLI arg) is coerced
        self.assertEqual(ap.parked_ids(self.base), {200})

    def test_unpark_removes(self):
        ap.park(self.base, 200)
        ap.park(self.base, 201)
        self.assertEqual(ap.unpark(self.base, 200), {201})
        self.assertEqual(ap.parked_ids(self.base), {201})

    def test_unpark_missing_is_noop(self):
        ap.park(self.base, 200)
        self.assertEqual(ap.unpark(self.base, 999), {200})

    def test_is_parked(self):
        ap.park(self.base, 200)
        self.assertTrue(ap.is_parked(self.base, 200))
        self.assertTrue(ap.is_parked(self.base, "200"))
        self.assertFalse(ap.is_parked(self.base, 201))

    def test_prune_drops_dangling(self):
        for pid in (200, 201, 202):
            ap.park(self.base, pid)
        # only 200 + 202 are still real partitions -> 201 dangling, dropped
        self.assertEqual(ap.prune(self.base, {200, 202, 8}), {200, 202})
        self.assertEqual(ap.parked_ids(self.base), {200, 202})

    def test_corrupt_file_reads_empty_failsafe(self):
        os.makedirs(os.path.dirname(self._p()), exist_ok=True)
        with open(self._p(), "w") as f:
            f.write("{ not json")
        self.assertEqual(ap.parked_ids(self.base), set())

    def test_non_list_reads_empty(self):
        os.makedirs(os.path.dirname(self._p()), exist_ok=True)
        with open(self._p(), "w") as f:
            json.dump({"parked": [200]}, f)  # wrong shape (object not array)
        self.assertEqual(ap.parked_ids(self.base), set())

    def test_non_int_entries_ignored(self):
        os.makedirs(os.path.dirname(self._p()), exist_ok=True)
        with open(self._p(), "w") as f:
            json.dump([200, "x", 201, None, 3.5, True], f)
        self.assertEqual(ap.parked_ids(self.base), {200, 201})  # only clean ints; bool excluded

    def test_atomic_write_leaves_no_tmp(self):
        ap.park(self.base, 200)
        self.assertFalse(os.path.exists(self._p() + ".tmp"))

    def test_path_under_server_state_admin(self):
        self.assertTrue(ap.park_path(self.base).endswith(os.path.join("server", "state", "admin", "parked-sietches.json")))


if __name__ == "__main__":
    unittest.main()
