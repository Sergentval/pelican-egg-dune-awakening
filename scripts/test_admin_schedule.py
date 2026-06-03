#!/usr/bin/env python3
"""Tests for admin_schedule (scheduler Phase 2): due-logic + run_tick."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import admin_schedule as sch

# A fixed Wednesday 08:30 UTC for deterministic slot math.
WED_0830 = datetime(2026, 6, 3, 8, 30, 0, tzinfo=timezone.utc)


def mem_ledger():
    return sch.SchedulerLedger(":memory:")


class TestSlotAndDue(unittest.TestCase):
    def test_slot_today(self):
        self.assertEqual(sch._slot_today(WED_0830, "08:00"),
                         WED_0830.replace(hour=8, minute=0, second=0, microsecond=0))
        self.assertIsNone(sch._slot_today(WED_0830, "nope"))

    def test_restart_due_happy(self):
        rcfg = {"enabled": True, "time": "08:00", "days": list(sch.DAYS), "catch_up_grace_secs": 3600}
        self.assertTrue(sch.restart_due(rcfg, WED_0830, None))  # 08:30 within [08:00, 09:00)

    def test_restart_disabled_or_wrong_day(self):
        self.assertFalse(sch.restart_due({"enabled": False, "time": "08:00"}, WED_0830, None))
        self.assertFalse(sch.restart_due({"enabled": True, "time": "08:00", "days": ["mon"]}, WED_0830, None))

    def test_restart_before_slot_and_after_grace(self):
        rcfg = {"enabled": True, "time": "08:00", "catch_up_grace_secs": 600}
        before = WED_0830.replace(hour=7, minute=59)
        self.assertFalse(sch.restart_due(rcfg, before, None))           # before slot
        late = WED_0830.replace(hour=9, minute=0)                       # 08:00 + 600s = 08:10 < 09:00
        self.assertFalse(sch.restart_due(rcfg, late, None))             # past grace

    def test_restart_dedupe_after_warn(self):
        rcfg = {"enabled": True, "time": "08:00", "catch_up_grace_secs": 3600}
        warned = sch._iso(WED_0830.replace(hour=8, minute=5))           # warned at 08:05 (>= 08:00 slot)
        self.assertFalse(sch.restart_due(rcfg, WED_0830, warned))

    def test_backup_due(self):
        self.assertFalse(sch.backup_due({"enabled": False}, WED_0830, None))
        self.assertTrue(sch.backup_due({"enabled": True, "every_hours": 24}, WED_0830, None))  # never run
        recent = sch._iso(WED_0830 - timedelta(hours=1))
        self.assertFalse(sch.backup_due({"enabled": True, "every_hours": 24}, WED_0830, recent))
        old = sch._iso(WED_0830 - timedelta(hours=25))
        self.assertTrue(sch.backup_due({"enabled": True, "every_hours": 24}, WED_0830, old))


class TestLoadConfig(unittest.TestCase):
    def test_defaults_on_missing(self):
        d = tempfile.mkdtemp()
        c = sch.load_config(d)
        self.assertFalse(c["restart"]["enabled"])
        self.assertFalse(c["backup"]["enabled"])

    def test_merge_partial(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "data", "admin"))
        with open(sch.config_path(d), "w") as f:
            json.dump({"backup": {"enabled": True, "every_hours": 6}}, f)
        c = sch.load_config(d)
        self.assertTrue(c["backup"]["enabled"])
        self.assertEqual(c["backup"]["every_hours"], 6)
        self.assertEqual(c["backup"]["retention"], 7)        # default preserved
        self.assertFalse(c["restart"]["enabled"])            # default preserved


class TestRunTick(unittest.TestCase):
    def setUp(self):
        self._orig = (sch.run_backup, sch.broadcast_restart, sch.pelican_restart, sch.restart_configured)

    def tearDown(self):
        sch.run_backup, sch.broadcast_restart, sch.pelican_restart, sch.restart_configured = self._orig

    def test_backup_runs_when_due(self):
        sch.run_backup = lambda base: (True, "backup=ok file=x bytes=10")
        led = mem_ledger()
        cfg = {"restart": {"enabled": False}, "backup": {"enabled": True, "every_hours": 24}}
        acts = sch.run_tick("/b", led, now=WED_0830, cfg=cfg)
        self.assertEqual(acts, [("backup", True, "backup=ok file=x bytes=10")])
        self.assertEqual(led.last("backup")[:4], "2026")

    def test_restart_warn_arms_pending(self):
        sch.restart_configured = lambda: True
        sch.broadcast_restart = lambda base, lead, freq: (True, "publish=ok")
        led = mem_ledger()
        cfg = {"restart": {"enabled": True, "time": "08:00", "days": list(sch.DAYS),
                           "warn_lead_secs": 300, "catch_up_grace_secs": 3600},
               "backup": {"enabled": False}}
        acts = sch.run_tick("/b", led, now=WED_0830, cfg=cfg)
        self.assertEqual(acts[0][0], "restart-warn")
        self.assertTrue(acts[0][1])
        self.assertEqual(led.get_state(sch.PENDING_KEY), sch._iso(WED_0830 + timedelta(seconds=300)))

    def test_restart_enabled_but_unconfigured_skips(self):
        sch.restart_configured = lambda: False
        led = mem_ledger()
        cfg = {"restart": {"enabled": True, "time": "08:00", "days": list(sch.DAYS), "catch_up_grace_secs": 3600},
               "backup": {"enabled": False}}
        acts = sch.run_tick("/b", led, now=WED_0830, cfg=cfg)
        self.assertEqual(acts[0], ("restart-warn", False, "not configured"))
        self.assertIsNone(led.get_state(sch.PENDING_KEY))  # no pending armed

    def test_pending_fires_restart(self):
        sch.pelican_restart = lambda: (True, "power restart HTTP 204")
        led = mem_ledger()
        led.set_state(sch.PENDING_KEY, sch._iso(WED_0830 - timedelta(seconds=5)))
        acts = sch.run_tick("/b", led, now=WED_0830, cfg={"restart": {}, "backup": {"enabled": False}})
        self.assertEqual(acts, [("restart", True, "power restart HTTP 204")])
        self.assertIsNone(led.get_state(sch.PENDING_KEY))  # cleared

    def test_idle_when_all_disabled(self):
        led = mem_ledger()
        self.assertEqual(sch.run_tick("/b", led, now=WED_0830,
                                      cfg={"restart": {"enabled": False}, "backup": {"enabled": False}}), [])


if __name__ == "__main__":
    unittest.main()
