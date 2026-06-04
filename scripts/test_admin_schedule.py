#!/usr/bin/env python3
"""Tests for admin_schedule (scheduler Phase 2): due-logic + run_tick."""
import http.client
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
        self.assertIsNone(sch._slot_today(WED_0830, "25:00"))   # hour out of range -> None, not crash
        self.assertIsNone(sch._slot_today(WED_0830, "12:60"))   # minute out of range -> None, not crash

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


class TestValidateConfig(unittest.TestCase):
    def test_valid_coerces(self):
        cfg, err = sch.validate_config({
            "restart": {"enabled": True, "time": "06:30", "days": ["sun", "mon"], "warn_lead_secs": "120"},
            "backup": {"enabled": True, "every_hours": "12", "retention": 0},
        })
        self.assertIsNone(err)
        self.assertTrue(cfg["restart"]["enabled"])
        self.assertEqual(cfg["restart"]["days"], ["mon", "sun"])   # canonical order
        self.assertEqual(cfg["restart"]["warn_lead_secs"], 120)
        self.assertEqual(cfg["backup"]["every_hours"], 12)
        self.assertEqual(cfg["backup"]["retention"], 1)            # clamped to >=1

    def test_rejects(self):
        for bad in (
            "notadict",
            {"restart": "x"},
            {"restart": {"time": "9pm"}},
            {"restart": {"days": ["funday"]}},
            {"restart": {"days": []}},
            {"backup": {"every_hours": "soon"}},
        ):
            _, err = sch.validate_config(bad)
            self.assertIsNotNone(err, bad)

    def test_save_roundtrip(self):
        d = tempfile.mkdtemp()
        cfg, _ = sch.validate_config({"backup": {"enabled": True, "every_hours": 6}})
        sch.save_config(d, cfg)
        self.assertTrue(sch.load_config(d)["backup"]["enabled"])
        self.assertEqual(sch.load_config(d)["backup"]["every_hours"], 6)


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


class TestPowerEndpoint(unittest.TestCase):
    def test_https_default_port(self):
        scheme, host, port, path = sch._power_endpoint("https://pelican.example.com", "abc123")
        self.assertEqual((scheme, host, port), ("https", "pelican.example.com", 443))
        self.assertEqual(path, "/api/client/servers/abc123/power")

    def test_explicit_port(self):
        scheme, host, port, _ = sch._power_endpoint("https://panel.example.com:8443", "s")
        self.assertEqual((scheme, host, port), ("https", "panel.example.com", 8443))

    def test_http_default_port(self):
        scheme, host, port, _ = sch._power_endpoint("http://10.0.0.5", "s")
        self.assertEqual((scheme, host, port), ("http", "10.0.0.5", 80))


class TestResolvingConnection(unittest.TestCase):
    def test_open_conn_uses_resolver_when_ip_set(self):
        # With a resolve IP, TCP target is the IP but SNI/cert/Host stay the hostname
        # (curl --resolve equivalent) so a CDN/proxy in front of DNS is bypassed.
        conn = sch._open_power_conn("https", "pelican.example.com", 443, "10.99.0.1", timeout=5)
        self.assertIsInstance(conn, sch._ResolvingHTTPSConnection)
        self.assertEqual(conn.host, "pelican.example.com")
        self.assertEqual(conn._resolve_ip, "10.99.0.1")
        self.assertEqual(conn.port, 443)

    def test_open_conn_plain_https_without_ip(self):
        conn = sch._open_power_conn("https", "pelican.example.com", 443, None, timeout=5)
        self.assertIsInstance(conn, http.client.HTTPSConnection)
        self.assertNotIsInstance(conn, sch._ResolvingHTTPSConnection)
        self.assertEqual(conn.host, "pelican.example.com")

    def test_open_conn_http(self):
        conn = sch._open_power_conn("http", "10.0.0.5", 80, None, timeout=5)
        self.assertIsInstance(conn, http.client.HTTPConnection)
        self.assertNotIsInstance(conn, http.client.HTTPSConnection)


class TestPelicanRestartSkips(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("DUNE_PELICAN_URL", "DUNE_PELICAN_CLIENT_KEY",
                        "DUNE_PELICAN_SERVER_ID", "DUNE_PELICAN_RESOLVE")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_skips_when_unset(self):
        for k in self._saved:
            os.environ.pop(k, None)
        ok, detail = sch.pelican_restart()
        self.assertFalse(ok)
        self.assertIn("skipped", detail)


# ---- Phase 1: generic scheduled tasks (broadcast + framework) ----------------

class TestTaskValidation(unittest.TestCase):
    def test_valid_broadcast_daily(self):
        cfg, err = sch.validate_config({"tasks": [
            {"id": "reset-warn", "type": "broadcast", "enabled": True,
             "schedule": {"kind": "daily", "time": "07:00", "days": ["fri", "mon"]},
             "params": {"title": "Notice", "body": "DD resets soon", "duration": "20"}}]})
        self.assertIsNone(err)
        self.assertEqual(len(cfg["tasks"]), 1)
        t = cfg["tasks"][0]
        self.assertEqual(t["id"], "reset-warn")
        self.assertEqual(t["type"], "broadcast")
        self.assertTrue(t["enabled"])
        self.assertEqual(t["schedule"]["days"], ["mon", "fri"])   # canonical order
        self.assertEqual(t["params"]["duration"], 20)             # coerced from "20"

    def test_valid_interval_and_param_defaults(self):
        cfg, err = sch.validate_config({"tasks": [
            {"id": "ping", "type": "broadcast", "enabled": False,
             "schedule": {"kind": "interval", "every_minutes": "30"},
             "params": {"title": "t", "body": "b"}}]})
        self.assertIsNone(err)
        self.assertEqual(cfg["tasks"][0]["schedule"]["every_minutes"], 30)
        self.assertEqual(cfg["tasks"][0]["params"]["duration"], 30)   # default duration

    def test_no_tasks_defaults_empty(self):
        cfg, err = sch.validate_config({"backup": {"enabled": True}})
        self.assertIsNone(err)
        self.assertEqual(cfg["tasks"], [])

    def test_rejects_bad_tasks(self):
        ok_sched = {"kind": "interval", "every_minutes": 5}
        ok_params = {"title": "t", "body": "b"}
        for bad in (
            {"tasks": "notalist"},
            {"tasks": [{"id": "", "type": "broadcast", "schedule": ok_sched, "params": ok_params}]},
            {"tasks": [{"id": "Bad Id", "type": "broadcast", "schedule": ok_sched, "params": ok_params}]},
            {"tasks": [{"id": "x", "type": "nope", "schedule": ok_sched, "params": ok_params}]},
            {"tasks": [{"id": "x", "type": "broadcast", "schedule": {"kind": "weekly"}, "params": ok_params}]},
            {"tasks": [{"id": "x", "type": "broadcast",
                        "schedule": {"kind": "daily", "time": "9pm"}, "params": ok_params}]},
            {"tasks": [{"id": "x", "type": "broadcast",
                        "schedule": {"kind": "daily", "days": ["funday"]}, "params": ok_params}]},
            {"tasks": [{"id": "x", "type": "broadcast", "schedule": ok_sched,
                        "params": {"title": "", "body": "b"}}]},
            {"tasks": [{"id": "x", "type": "broadcast",
                        "schedule": {"kind": "interval", "every_minutes": 0}, "params": ok_params}]},
            {"tasks": [
                {"id": "dup", "type": "broadcast", "schedule": ok_sched, "params": ok_params},
                {"id": "dup", "type": "broadcast", "schedule": ok_sched, "params": ok_params}]},
        ):
            _, err = sch.validate_config(bad)
            self.assertIsNotNone(err, bad)


class TestTaskDue(unittest.TestCase):
    def test_daily_happy(self):
        t = {"enabled": True, "schedule": {"kind": "daily", "time": "08:00", "days": list(sch.DAYS)}}
        self.assertTrue(sch.task_due(t, WED_0830, None))

    def test_daily_wrong_day(self):
        t = {"enabled": True, "schedule": {"kind": "daily", "time": "08:00", "days": ["mon"]}}
        self.assertFalse(sch.task_due(t, WED_0830, None))

    def test_daily_before_slot_and_past_grace(self):
        t = {"enabled": True, "schedule": {"kind": "daily", "time": "08:00", "days": list(sch.DAYS)}}
        self.assertFalse(sch.task_due(t, WED_0830.replace(hour=7, minute=59), None))   # before slot
        self.assertFalse(sch.task_due(t, WED_0830.replace(hour=10, minute=0), None))   # past 1h grace

    def test_daily_dedupe_once_per_slot(self):
        t = {"enabled": True, "schedule": {"kind": "daily", "time": "08:00", "days": list(sch.DAYS)}}
        ran = sch._iso(WED_0830.replace(hour=8, minute=10))
        self.assertFalse(sch.task_due(t, WED_0830, ran))

    def test_interval(self):
        t = {"enabled": True, "schedule": {"kind": "interval", "every_minutes": 60}}
        self.assertTrue(sch.task_due(t, WED_0830, None))                                # never run
        self.assertFalse(sch.task_due(t, WED_0830, sch._iso(WED_0830 - timedelta(minutes=30))))
        self.assertTrue(sch.task_due(t, WED_0830, sch._iso(WED_0830 - timedelta(minutes=90))))

    def test_disabled(self):
        t = {"enabled": False, "schedule": {"kind": "interval", "every_minutes": 1}}
        self.assertFalse(sch.task_due(t, WED_0830, None))


class TestRunTickTasks(unittest.TestCase):
    def setUp(self):
        self._orig = sch.run_task

    def tearDown(self):
        sch.run_task = self._orig

    def _cfg(self, tasks):
        return {"restart": {"enabled": False}, "backup": {"enabled": False}, "tasks": tasks}

    def test_due_task_runs_and_records(self):
        calls = []
        sch.run_task = lambda base, task: (calls.append(task["id"]) or ("ok", "publish=ok"))
        led = mem_ledger()
        cfg = self._cfg([{"id": "reset-warn", "type": "broadcast", "enabled": True,
                          "schedule": {"kind": "daily", "time": "08:00", "days": list(sch.DAYS)},
                          "params": {"title": "t", "body": "b", "duration": 20}}])
        acts = sch.run_tick("/b", led, now=WED_0830, cfg=cfg)
        self.assertEqual(calls, ["reset-warn"])
        self.assertIn(("task:reset-warn", True, "publish=ok"), acts)
        self.assertEqual(led.last("task:reset-warn")[:4], "2026")

    def test_not_due_task_skipped(self):
        sch.run_task = lambda base, task: (_ for _ in ()).throw(AssertionError("must not run"))
        led = mem_ledger()
        cfg = self._cfg([{"id": "x", "type": "broadcast", "enabled": False,
                          "schedule": {"kind": "interval", "every_minutes": 5},
                          "params": {"title": "t", "body": "b"}}])
        self.assertEqual(sch.run_tick("/b", led, now=WED_0830, cfg=cfg), [])

    def test_bad_task_isolated_from_others(self):
        def boom(base, task):
            if task["id"] == "bad":
                raise RuntimeError("kaboom")
            return "ok", "ok"
        sch.run_task = boom
        led = mem_ledger()
        cfg = self._cfg([
            {"id": "bad", "type": "broadcast", "enabled": True,
             "schedule": {"kind": "interval", "every_minutes": 1}, "params": {"title": "t", "body": "b"}},
            {"id": "good", "type": "broadcast", "enabled": True,
             "schedule": {"kind": "interval", "every_minutes": 1}, "params": {"title": "t", "body": "b"}}])
        acts = sch.run_tick("/b", led, now=WED_0830, cfg=cfg)
        ids = {a[0]: a[1] for a in acts}
        self.assertFalse(ids["task:bad"])
        self.assertTrue(ids["task:good"])


class TestRunTaskDispatch(unittest.TestCase):
    def test_unknown_type(self):
        status, detail = sch.run_task("/b", {"id": "x", "type": "frobnicate", "params": {}})
        self.assertEqual(status, "error")
        self.assertIn("unknown task type", detail)

    def test_broadcast_requires_title_body(self):
        status, detail = sch.run_task("/b", {"id": "x", "type": "broadcast", "params": {"title": "", "body": ""}})
        self.assertEqual(status, "error")
        self.assertIn("title", detail)


class TestLoadConfigTasks(unittest.TestCase):
    def test_load_includes_tasks(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "data", "admin"))
        with open(sch.config_path(d), "w") as f:
            json.dump({"tasks": [{"id": "x", "type": "broadcast"}]}, f)
        c = sch.load_config(d)
        self.assertEqual(len(c["tasks"]), 1)
        self.assertEqual(c["tasks"][0]["id"], "x")

    def test_load_defaults_empty_tasks(self):
        self.assertEqual(sch.load_config(tempfile.mkdtemp())["tasks"], [])


# ---- Phase 2: scale-instance task type + retry/dedupe -------------------------

class TestScaleTaskValidation(unittest.TestCase):
    def test_valid_scale_daily_coerces(self):
        cfg, err = sch.validate_config({"tasks": [
            {"id": "dd-up", "type": "scale-instance", "enabled": True,
             "schedule": {"kind": "daily", "time": "18:00", "days": ["fri", "mon"]},
             "params": {"map": "DeepDesert_1", "replicas": "2", "force": "true"}}]})
        self.assertIsNone(err)
        t = cfg["tasks"][0]
        self.assertEqual(t["type"], "scale-instance")
        self.assertEqual(t["schedule"]["days"], ["mon", "fri"])
        self.assertEqual(t["params"]["map"], "DeepDesert_1")
        self.assertEqual(t["params"]["replicas"], 2)        # coerced from "2"
        self.assertIs(t["params"]["force"], True)           # coerced from "true"

    def test_valid_scale_interval_force_default_false(self):
        cfg, err = sch.validate_config({"tasks": [
            {"id": "ark-idle", "type": "scale-instance", "enabled": True,
             "schedule": {"kind": "interval", "every_minutes": "30"},
             "params": {"map": "SH_Arrakeen", "replicas": 0}}]})
        self.assertIsNone(err)
        self.assertEqual(cfg["tasks"][0]["params"]["replicas"], 0)   # zero = scale to idle
        self.assertIs(cfg["tasks"][0]["params"]["force"], False)     # default

    def test_valid_scale_replicas_at_max(self):
        cfg, err = sch.validate_config({"tasks": [
            {"id": "harko-max", "type": "scale-instance", "enabled": False,
             "schedule": {"kind": "interval", "every_minutes": 60},
             "params": {"map": "SH_HarkoVillage", "replicas": sch.SCALE_REPLICAS_MAX}}]})
        self.assertIsNone(err)
        self.assertEqual(cfg["tasks"][0]["params"]["replicas"], sch.SCALE_REPLICAS_MAX)

    def test_rejects_bad_scale_tasks(self):
        ok_sched = {"kind": "interval", "every_minutes": 5}

        def t(params):
            return {"tasks": [{"id": "x", "type": "scale-instance", "schedule": ok_sched, "params": params}]}

        for bad in (
            t({"replicas": 1}),                                          # missing map
            t({"map": "Overmap", "replicas": 1}),                        # not in allowlist
            t({"map": "", "replicas": 1}),                               # empty map
            t({"map": "DeepDesert_1", "replicas": "two"}),               # non-numeric
            t({"map": "DeepDesert_1", "replicas": -1}),                  # negative
            t({"map": "DeepDesert_1", "replicas": sch.SCALE_REPLICAS_MAX + 1}),  # over max
            t({"map": "DeepDesert_1", "replicas": True}),                # bool (not int)
            {"tasks": [{"id": "x", "type": "scale-instance", "schedule": ok_sched}]},  # no params
        ):
            _, err = sch.validate_config(bad)
            self.assertIsNotNone(err, bad)

    def test_scale_reuses_schedule_validation(self):
        _, err = sch.validate_config({"tasks": [
            {"id": "x", "type": "scale-instance", "schedule": {"kind": "daily", "time": "9pm"},
             "params": {"map": "DeepDesert_1", "replicas": 1}}]})
        self.assertIsNotNone(err)


class TestRunTaskScaleDispatch(unittest.TestCase):
    def setUp(self):
        self._orig = (sch.fetch_mock_status, sch.mock_k8s_request, sch.online_count_for_map)

    def tearDown(self):
        sch.fetch_mock_status, sch.mock_k8s_request, sch.online_count_for_map = self._orig

    def _status(self, current):
        return {"maps": [{"map": "DeepDesert_1", "key": "default/world-dd", "desired": current}]}

    def _scale(self, replicas, force=False, mp="DeepDesert_1"):
        return sch.run_task("/b", {"id": "s", "type": "scale-instance",
                                   "params": {"map": mp, "replicas": replicas, "force": force}})

    def test_scale_up_patches_no_guard(self):
        seen = {}
        sch.fetch_mock_status = lambda *a, **k: self._status(0)
        sch.online_count_for_map = lambda base, mp: (_ for _ in ()).throw(AssertionError("no guard on scale-up"))
        sch.mock_k8s_request = lambda method, path, body=None, **k: (
            seen.update(method=method, path=path, body=body) or (200, {"spec": {"replicas": 2}}))
        status, detail = self._scale(2)
        self.assertEqual(status, "ok")
        self.assertEqual(seen["method"], "PATCH")
        self.assertEqual(seen["path"], "/apis/igw.funcom.com/v1/namespaces/default/serversetscales/world-dd")
        self.assertEqual(seen["body"], {"spec": {"replicas": 2}})

    def test_idempotent_noop(self):
        sch.fetch_mock_status = lambda *a, **k: self._status(2)
        sch.online_count_for_map = lambda *a, **k: 0
        sch.mock_k8s_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PATCH on no-op"))
        status, detail = self._scale(2)
        self.assertEqual(status, "ok")
        self.assertIn("already", detail)

    def test_scale_down_with_players_skips(self):
        sch.fetch_mock_status = lambda *a, **k: self._status(3)
        sch.online_count_for_map = lambda base, mp: 3
        sch.mock_k8s_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scale"))
        status, detail = self._scale(0, force=False)
        self.assertEqual(status, "skipped")
        self.assertIn("player", detail.lower())

    def test_scale_down_force_proceeds(self):
        seen = []
        sch.fetch_mock_status = lambda *a, **k: self._status(3)
        sch.online_count_for_map = lambda base, mp: 3
        sch.mock_k8s_request = lambda method, path, body=None, **k: (seen.append(body) or (200, {"spec": {"replicas": 0}}))
        status, detail = self._scale(0, force=True)
        self.assertEqual(status, "ok")
        self.assertEqual(seen, [{"spec": {"replicas": 0}}])

    def test_status_unreachable_error(self):
        sch.fetch_mock_status = lambda *a, **k: None
        sch.online_count_for_map = lambda *a, **k: 0
        sch.mock_k8s_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PATCH without status"))
        status, detail = self._scale(1)
        self.assertEqual(status, "error")
        self.assertIn("unreachable", detail)

    def test_map_not_tracked_error(self):
        sch.fetch_mock_status = lambda *a, **k: {"maps": [{"map": "SH_Arrakeen", "key": "default/x", "desired": 1}]}
        sch.online_count_for_map = lambda *a, **k: 0
        sch.mock_k8s_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PATCH untracked"))
        status, detail = self._scale(1)
        self.assertEqual(status, "error")
        self.assertIn("not tracked", detail)

    def test_patch_http_error(self):
        sch.fetch_mock_status = lambda *a, **k: self._status(0)
        sch.online_count_for_map = lambda *a, **k: 0
        sch.mock_k8s_request = lambda *a, **k: (500, {"message": "boom"})
        status, detail = self._scale(1)
        self.assertEqual(status, "error")
        self.assertIn("500", detail)


class TestSchedulerLedgerSettled(unittest.TestCase):
    def test_last_settled_excludes_error(self):
        led = mem_ledger()
        led.record("task:x", "error", "boom")
        self.assertIsNone(led.last_settled("task:x"))     # an error does not settle
        self.assertIsNotNone(led.last("task:x"))          # but last() still sees it
        led.record("task:x", "ok", "done")
        self.assertIsNotNone(led.last_settled("task:x"))
        led.record("task:x", "skipped", "players")
        self.assertEqual(led.last_settled("task:x"), led.last("task:x"))  # skipped settles too


class TestRunTickTaskRetry(unittest.TestCase):
    def setUp(self):
        self._orig = sch.run_task

    def tearDown(self):
        sch.run_task = self._orig

    def _cfg(self):
        return {"restart": {"enabled": False}, "backup": {"enabled": False},
                "tasks": [{"id": "t", "type": "broadcast", "enabled": True,
                           "schedule": {"kind": "daily", "time": "08:00", "days": list(sch.DAYS)},
                           "params": {"title": "T", "body": "B"}}]}

    def test_error_does_not_settle_slot_retries_next_tick(self):
        sch.run_task = lambda base, task: ("error", "transient")
        led = mem_ledger()
        cfg = self._cfg()
        acts1 = sch.run_tick("/b", led, now=WED_0830, cfg=cfg)
        self.assertEqual(acts1[0][0], "task:t")
        self.assertFalse(acts1[0][1])                      # errored
        acts2 = sch.run_tick("/b", led, now=WED_0830.replace(minute=35), cfg=cfg)
        self.assertEqual([a[0] for a in acts2], ["task:t"])  # retried (error didn't settle slot)

    def test_skip_settles_slot_no_retry(self):
        sch.run_task = lambda base, task: ("skipped", "players online")
        led = mem_ledger()
        cfg = self._cfg()
        sch.run_tick("/b", led, now=WED_0830, cfg=cfg)
        acts2 = sch.run_tick("/b", led, now=WED_0830.replace(minute=40), cfg=cfg)
        self.assertEqual(acts2, [])                         # skip settled the slot


# ---- Review fixes: interval cadence, fail-safe guard, CLI status, integration -----

class TestRunTickIntervalSettle(unittest.TestCase):
    """An errored interval task must respect every_minutes, not refire every tick
    (kind-aware dedupe: interval keys off last(), daily off last_settled())."""
    def setUp(self):
        self._orig = sch.run_task

    def tearDown(self):
        sch.run_task = self._orig

    def _cfg(self):
        return {"restart": {"enabled": False}, "backup": {"enabled": False},
                "tasks": [{"id": "iv", "type": "broadcast", "enabled": True,
                           "schedule": {"kind": "interval", "every_minutes": 60},
                           "params": {"title": "T", "body": "B"}}]}

    def _seed(self, led, status, minutes_ago):
        # record() stamps with real _now_dt(), so seed relative to real now and tick at real now.
        led.conn.execute("INSERT INTO scheduler_runs (task,status,detail,at) VALUES (?,?,?,?)",
                         ("task:iv", status, "", sch._iso(sch._now_dt() - timedelta(minutes=minutes_ago))))
        led.conn.commit()

    def test_interval_error_within_window_no_refire(self):
        # The bug: an errored interval run refired every tick (last_settled ignored the
        # error -> None -> always due). Kind-aware dedupe keys interval off last(), so a
        # recent error advances the clock and the task waits out every_minutes.
        led = mem_ledger()
        self._seed(led, "error", 5)                       # errored 5 min ago; interval is 60
        sch.run_task = lambda b, t: (_ for _ in ()).throw(AssertionError("must not refire within interval after error"))
        self.assertEqual(sch.run_tick("/b", led, now=sch._now_dt(), cfg=self._cfg()), [])

    def test_interval_refires_after_window(self):
        led = mem_ledger()
        self._seed(led, "ok", 120)                        # last run 120 min ago; interval 60 -> due
        fired = []
        sch.run_task = lambda b, t: (fired.append(1) or ("ok", "done"))
        sch.run_tick("/b", led, now=sch._now_dt(), cfg=self._cfg())
        self.assertEqual(fired, [1])


class TestScaleGuardEdgeCases(unittest.TestCase):
    def setUp(self):
        self._orig = (sch.fetch_mock_status, sch.mock_k8s_request, sch.online_count_for_map)

    def tearDown(self):
        sch.fetch_mock_status, sch.mock_k8s_request, sch.online_count_for_map = self._orig

    def test_scale_down_unconfirmable_count_skips(self):
        # fail-safe: if the player count can't be read, a non-force scale-down must skip.
        sch.fetch_mock_status = lambda *a, **k: {"maps": [{"map": "DeepDesert_1", "key": "default/world-dd", "desired": 3}]}
        sch.online_count_for_map = lambda base, mp: None
        sch.mock_k8s_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scale when count unknown"))
        status, detail = sch.run_task("/b", {"id": "d", "type": "scale-instance",
                                             "params": {"map": "DeepDesert_1", "replicas": 0, "force": False}})
        self.assertEqual(status, "skipped")
        self.assertIn("confirm", detail.lower())

    def test_patch_transport_failure_none_code(self):
        sch.fetch_mock_status = lambda *a, **k: {"maps": [{"map": "DeepDesert_1", "key": "default/world-dd", "desired": 0}]}
        sch.online_count_for_map = lambda *a, **k: 0
        sch.mock_k8s_request = lambda *a, **k: (None, None)
        status, detail = sch.run_task("/b", {"id": "x", "type": "scale-instance",
                                             "params": {"map": "DeepDesert_1", "replicas": 1}})
        self.assertEqual(status, "error")
        self.assertIn("failed", detail.lower())


class TestRunTickScaleIntegration(unittest.TestCase):
    """run_tick -> real run_task -> run_scale_instance (the seam tests previously
    only covered with run_task monkeypatched)."""
    def setUp(self):
        self._orig = (sch.fetch_mock_status, sch.mock_k8s_request, sch.online_count_for_map)

    def tearDown(self):
        sch.fetch_mock_status, sch.mock_k8s_request, sch.online_count_for_map = self._orig

    def _cfg(self, replicas, force=False):
        return {"restart": {"enabled": False}, "backup": {"enabled": False},
                "tasks": [{"id": "dd", "type": "scale-instance", "enabled": True,
                           "schedule": {"kind": "interval", "every_minutes": 1},
                           "params": {"map": "DeepDesert_1", "replicas": replicas, "force": force}}]}

    def test_real_scale_up_through_run_tick(self):
        seen = {}
        sch.fetch_mock_status = lambda *a, **k: {"maps": [{"map": "DeepDesert_1", "key": "default/world-dd", "desired": 0}]}
        sch.online_count_for_map = lambda *a, **k: 0
        sch.mock_k8s_request = lambda m, p, b=None, **k: (seen.update(body=b) or (200, {}))
        led = mem_ledger()
        acts = sch.run_tick("/b", led, now=WED_0830, cfg=self._cfg(2))
        self.assertEqual(acts, [("task:dd", True, "scaled DeepDesert_1 0->2")])
        self.assertEqual(seen["body"], {"spec": {"replicas": 2}})
        self.assertIsNotNone(led.last_settled("task:dd"))

    def test_real_scale_down_skip_settles_slot(self):
        sch.fetch_mock_status = lambda *a, **k: {"maps": [{"map": "DeepDesert_1", "key": "default/world-dd", "desired": 3}]}
        sch.online_count_for_map = lambda *a, **k: 3
        sch.mock_k8s_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not PATCH"))
        led = mem_ledger()
        acts = sch.run_tick("/b", led, now=WED_0830, cfg=self._cfg(0))
        self.assertEqual(acts[0][0], "task:dd")
        self.assertFalse(acts[0][1])                          # skipped -> ok flag False
        self.assertIsNotNone(led.last_settled("task:dd"))     # but slot IS settled (no retry storm)


class TestRunTaskCli(unittest.TestCase):
    def setUp(self):
        self._orig = sch.run_task
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "data", "admin"), exist_ok=True)
        os.makedirs(os.path.join(self.d, "server", "state"), exist_ok=True)

    def tearDown(self):
        sch.run_task = self._orig

    def _write_task(self):
        with open(sch.config_path(self.d), "w") as f:
            json.dump({"tasks": [{"id": "dd", "type": "scale-instance",
                                  "schedule": {"kind": "interval", "every_minutes": 1},
                                  "params": {"map": "DeepDesert_1", "replicas": 0, "force": False}}]}, f)

    def test_unknown_id_exits_1(self):
        self.assertEqual(sch._main(["x", "run-task", self.d, "nope"]), 1)

    def test_skipped_status_recorded_verbatim(self):
        # run-task must record the real status, not coerce skipped/error to "ok".
        self._write_task()
        sch.run_task = lambda base, task: ("skipped", "players online")
        sch._main(["x", "run-task", self.d, "dd"])
        led = sch.SchedulerLedger(sch.ledger_path(self.d))
        cur = led.conn.execute("SELECT status FROM scheduler_runs WHERE task='task:dd' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        led.close()
        self.assertEqual(row[0], "skipped")


if __name__ == "__main__":
    unittest.main()
