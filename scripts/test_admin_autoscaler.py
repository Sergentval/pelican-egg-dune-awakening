#!/usr/bin/env python3
"""Tests for admin_autoscaler — the PURE demand-based scaling policy (Phase 1).

The decision engine is a safety boundary (it scales a live game server up and
down), so the guard cases are exhaustive: never evict a non-empty map, fail safe
when the player count is unreadable, never drop below the per-map floor, and
debounce with the idle-drain + post-demand grace timers. I/O (mock-k8s PATCH,
server-status, log tail, sqlite) is Phase 2 and not exercised here.
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import admin_autoscaler as az

# Fixed instant for deterministic timer math.
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def cfg(min_r, max_r):
    return {"min_replicas": min_r, "max_replicas": max_r}


class TestValidateConfig(unittest.TestCase):
    def test_rejects_non_dict(self):
        c, err = az.validate_config(["nope"])
        self.assertIsNone(c)
        self.assertIsNotNone(err)

    def test_defaults_on_empty(self):
        c, err = az.validate_config({})
        self.assertIsNone(err)
        self.assertFalse(c["enabled"])
        self.assertEqual(c["scan_interval_secs"], 30)  # default = the MIN floor: fastest sanctioned wake-notice cadence
        self.assertEqual(c["idle_drain_secs"], 300)
        self.assertEqual(c["demand_grace_secs"], 120)
        self.assertEqual(c["players_per_instance"], 30)
        # default-managed: all SCALABLE maps, warm floor 1, headroom 2
        self.assertEqual([m["map"] for m in c["maps"]], list(az.SCALABLE_MAPS))
        for m in c["maps"]:
            self.assertEqual((m["min_replicas"], m["max_replicas"], m["enabled"]), (1, 2, True))

    def test_enabled_coercion(self):
        self.assertTrue(az.validate_config({"enabled": "true"})[0]["enabled"])
        self.assertTrue(az.validate_config({"enabled": 1})[0]["enabled"])
        self.assertFalse(az.validate_config({"enabled": "no"})[0]["enabled"])

    def test_scan_interval_floor(self):
        # below the minimum cadence is clamped up, not rejected
        self.assertEqual(az.validate_config({"scan_interval_secs": 5})[0]["scan_interval_secs"],
                         az.MIN_SCAN_INTERVAL_SECS)
        self.assertEqual(az.validate_config({"scan_interval_secs": 90})[0]["scan_interval_secs"], 90)

    def test_int_fields_validated(self):
        for k in ("idle_drain_secs", "demand_grace_secs", "players_per_instance", "scan_interval_secs"):
            self.assertIsNotNone(az.validate_config({k: "abc"})[1], f"{k} non-int should error")

    def test_players_per_instance_floor(self):
        # must be >= 1 (it's a divisor) — 0/negative coerced up to 1
        self.assertEqual(az.validate_config({"players_per_instance": 0})[0]["players_per_instance"], 1)
        self.assertEqual(az.validate_config({"players_per_instance": -4})[0]["players_per_instance"], 1)

    def test_maps_must_be_list(self):
        self.assertIsNotNone(az.validate_config({"maps": {"DeepDesert_1": {}}})[1])

    def test_rejects_unknown_map(self):
        c, err = az.validate_config({"maps": [{"map": "Survival_1", "min_replicas": 1, "max_replicas": 2}]})
        self.assertIsNone(c)
        self.assertIn("Survival_1", err)

    def test_rejects_duplicate_map(self):
        raw = {"maps": [{"map": "SH_Arrakeen", "min_replicas": 0, "max_replicas": 1},
                        {"map": "SH_Arrakeen", "min_replicas": 1, "max_replicas": 2}]}
        self.assertIsNotNone(az.validate_config(raw)[1])

    def test_replica_bounds(self):
        # max must be >= 1 and <= SCALE_REPLICAS_MAX; min in [0, max]
        bad = [
            {"map": "SH_Arrakeen", "min_replicas": 0, "max_replicas": 0},        # max < 1
            {"map": "SH_Arrakeen", "min_replicas": 3, "max_replicas": 2},        # min > max
            {"map": "SH_Arrakeen", "min_replicas": -1, "max_replicas": 2},       # min < 0
            {"map": "SH_Arrakeen", "min_replicas": 1, "max_replicas": az.SCALE_REPLICAS_MAX + 1},  # max too big
        ]
        for m in bad:
            self.assertIsNotNone(az.validate_config({"maps": [m]})[1], f"should reject {m}")

    def test_valid_map_passes_and_is_canonicalized(self):
        raw = {"maps": [{"map": "SH_HarkoVillage", "min_replicas": "0", "max_replicas": "3",
                         "enabled": "false"}]}
        c, err = az.validate_config(raw)
        self.assertIsNone(err)
        m = c["maps"][0]
        self.assertEqual((m["map"], m["min_replicas"], m["max_replicas"], m["enabled"]),
                         ("SH_HarkoVillage", 0, 3, False))

    def test_maps_emitted_in_canonical_order(self):
        raw = {"maps": [{"map": "SH_HarkoVillage", "min_replicas": 1, "max_replicas": 2},
                        {"map": "DeepDesert_1", "min_replicas": 1, "max_replicas": 2}]}
        c, _ = az.validate_config(raw)
        order = [m["map"] for m in c["maps"]]
        self.assertEqual(order, [m for m in az.SCALABLE_MAPS if m in order])


class TestParseTravelDemand(unittest.TestCase):
    def test_dimension_form_num_eq(self):
        line = ("[07:52:32 7 INF Main] [61 occurrences]: Processing travel queue for "
                "DeepDesert_1 (Deep Desert S1, id=QBl6, dimension=1, partition=101, num=5)")
        self.assertEqual(az.parse_travel_demand([line]), {"DeepDesert_1": 5})

    def test_classical_group_form_num_colon(self):
        line = ("Processing travel queue for ClassicalInstancing group CB_Ecolab_Bronze_Green_089 "
                "(servers: [], num: 4)")
        self.assertEqual(az.parse_travel_demand([line]), {"CB_Ecolab_Bronze_Green_089": 4})

    def test_received_travel_request_form(self):
        line = "Received travel request for 3 player(s) to SH_Arrakeen (instancingMode=Dimension)"
        self.assertEqual(az.parse_travel_demand([line]), {"SH_Arrakeen": 3})

    def test_zero_num_recorded_as_zero(self):
        line = "Processing travel queue for SH_HarkoVillage (dimension=1, num=0)"
        self.assertEqual(az.parse_travel_demand([line]), {"SH_HarkoVillage": 0})

    def test_max_over_window(self):
        lines = [
            "Processing travel queue for SH_Arrakeen (num=2)",
            "Processing travel queue for SH_Arrakeen (num=7)",
            "Processing travel queue for SH_Arrakeen (num=1)",
        ]
        self.assertEqual(az.parse_travel_demand(lines), {"SH_Arrakeen": 7})

    def test_ignores_unrelated_lines(self):
        lines = ["LogIGW: Display: Server farm is READY (3 server(s), num=99)",  # not a travel line
                 "random noise", ""]
        self.assertEqual(az.parse_travel_demand(lines), {})

    def test_empty(self):
        self.assertEqual(az.parse_travel_demand([]), {})


class TestDecideMapUp(unittest.TestCase):
    def test_load_scales_up(self):
        d = az.decide_map(cfg(1, 4), current=1, online=70, demand=0, now=NOW,
                          players_per_instance=30)
        self.assertEqual((d.action, d.target), ("up", 3))  # ceil(70/30)=3
        self.assertIsNone(d.idle_since)

    def test_load_up_capped_at_max(self):
        d = az.decide_map(cfg(1, 2), current=1, online=999, demand=0, now=NOW,
                          players_per_instance=30)
        self.assertEqual((d.action, d.target), ("up", 2))

    def test_cold_map_wakes_on_demand(self):
        d = az.decide_map(cfg(0, 2), current=0, online=0, demand=2, now=NOW)
        self.assertEqual((d.action, d.target), ("up", 1))

    def test_demand_wakes_even_when_population_unreadable(self):
        d = az.decide_map(cfg(0, 2), current=0, online=None, demand=1, now=NOW)
        self.assertEqual((d.action, d.target), ("up", 1))

    def test_up_records_demand_and_clears_idle(self):
        d = az.decide_map(cfg(0, 2), current=0, online=0, demand=4, now=NOW,
                          idle_since=NOW - timedelta(seconds=999))
        self.assertEqual(d.action, "up")
        self.assertEqual(d.last_demand, NOW)
        self.assertIsNone(d.idle_since)


class TestDecideMapDownGuards(unittest.TestCase):
    def test_never_evict_nonempty(self):
        d = az.decide_map(cfg(1, 4), current=2, online=20, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=9999))
        self.assertEqual(d.action, "hold")
        self.assertIsNone(d.idle_since)  # active -> idle countdown reset

    def test_unreadable_population_holds(self):
        d = az.decide_map(cfg(1, 4), current=2, online=None, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=9999))
        self.assertEqual(d.action, "hold")
        self.assertIsNone(d.idle_since)  # don't accrue idle while blind

    def test_recent_demand_grace_blocks_drain(self):
        # empty + idle-elapsed, but demand seen 60s ago (< 120s grace) -> hold.
        # Concurrent timers: the idle countdown is PRESERVED through the grace, not reset.
        d = az.decide_map(cfg(0, 2), current=1, online=0, demand=0, now=NOW,
                          last_demand=NOW - timedelta(seconds=60),
                          idle_since=NOW - timedelta(seconds=9999),
                          demand_grace_secs=120, idle_drain_secs=300)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.reason, "post-demand grace")
        self.assertEqual(d.idle_since, NOW - timedelta(seconds=9999))

    def test_at_floor_holds(self):
        d = az.decide_map(cfg(1, 4), current=1, online=0, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=9999))
        self.assertEqual((d.action, d.target), ("hold", 1))


class TestDecideMapDrain(unittest.TestCase):
    def test_starts_idle_timer_when_empty(self):
        d = az.decide_map(cfg(1, 4), current=2, online=0, demand=0, now=NOW,
                          idle_since=None, idle_drain_secs=300)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.idle_since, NOW)  # countdown begins now

    def test_holds_while_draining(self):
        d = az.decide_map(cfg(1, 4), current=2, online=0, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=100), idle_drain_secs=300)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.idle_since, NOW - timedelta(seconds=100))  # timer preserved

    def test_drains_to_floor_after_idle(self):
        d = az.decide_map(cfg(1, 4), current=2, online=0, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=301), idle_drain_secs=300)
        self.assertEqual((d.action, d.target), ("down", 1))
        self.assertIsNone(d.idle_since)

    def test_cold_map_drains_to_zero(self):
        d = az.decide_map(cfg(0, 2), current=1, online=0, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=301), idle_drain_secs=300)
        self.assertEqual((d.action, d.target), ("down", 0))

    def test_never_below_floor(self):
        # warm floor 1, already at 1, empty + idle-elapsed -> cannot drain below floor
        d = az.decide_map(cfg(1, 2), current=1, online=0, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=9999), idle_drain_secs=300)
        self.assertEqual((d.action, d.target), ("hold", 1))


class TestValidateConfigCoercion(unittest.TestCase):
    def test_map_replicas_reject_float_and_bool(self):
        # map ints must reject bool/float exactly like the top-level int fields (no
        # silent truncation), unlike the bare int() the first cut used.
        for bad in (1.9, True, False):
            self.assertIsNotNone(
                az.validate_config({"maps": [{"map": "SH_Arrakeen",
                                              "min_replicas": bad, "max_replicas": 2}]})[1],
                f"should reject min_replicas={bad!r}")


class TestParseTravelDemandHardening(unittest.TestCase):
    def test_nested_paren_serverid_real_format(self):
        # THE LIVE FORMAT: a warm map's record carries the server id in a NESTED paren
        # "(servers: [4 (id)], num: 0)". num must be read past the inner paren — not
        # mis-read as num-less (which would phantom a wake and pin a cold map warm).
        line = "Processing travel queue for SH_HarkoVillage (servers: [4 (C3JSKchzQw+a_Lrr+9XHRw)], num: 0)"
        self.assertEqual(az.parse_travel_demand([line]), {"SH_HarkoVillage": 0})

    def test_nested_paren_serverid_with_demand(self):
        # same nested-paren format but real inbound demand -> wake still parses
        line = "Processing travel queue for SH_HarkoVillage (servers: [4 (idHere)], num: 3)"
        self.assertEqual(az.parse_travel_demand([line]), {"SH_HarkoVillage": 3})

    def test_no_cross_record_borrow(self):
        # Two records concatenated on one physical line by the gateway dedup. num must
        # bind to each record's OWN segment: DD is num-less (-> 0), Arrakeen num=6 and
        # must NOT be swallowed.
        line = ("Processing travel queue for DeepDesert_1 (Deep Desert S1, id=Q, dimension=1, "
                "partition=101) Processing travel queue for SH_Arrakeen (Arrakeen, dimension=0, num=6)")
        self.assertEqual(az.parse_travel_demand([line]), {"DeepDesert_1": 0, "SH_Arrakeen": 6})

    def test_name_strips_trailing_punctuation(self):
        # trailing comma / no-space "(" must not taint the captured map name (else the
        # caller's managed-map match fails and the wake is silently lost).
        self.assertEqual(
            az.parse_travel_demand(["Processing travel queue for DeepDesert_1, (dimension=1, num=5)"]),
            {"DeepDesert_1": 5})
        self.assertEqual(
            az.parse_travel_demand(["Received travel request for 2 player(s) to SH_HarkoVillage(mode=x)"]),
            {"SH_HarkoVillage": 2})

    def test_numless_travel_line_is_zero(self):
        # A travel record with NO num is treated as no demand (real records always carry
        # num — defaulting to 1 is what phantomed demand onto warm maps' server-id lines).
        self.assertEqual(
            az.parse_travel_demand(["Processing travel queue for DeepDesert_1 (Deep Desert S1, dimension=1)"]),
            {"DeepDesert_1": 0})


class TestDemandForMap(unittest.TestCase):
    def test_managed_map(self):
        self.assertEqual(az.demand_for_map({"SH_Arrakeen": 4}, "SH_Arrakeen"), 4)

    def test_group_does_not_drive_managed_map(self):
        self.assertEqual(az.demand_for_map({"CB_Ecolab_Bronze_Green_089": 9}, "DeepDesert_1"), 0)

    def test_missing_is_zero(self):
        self.assertEqual(az.demand_for_map({}, "SH_HarkoVillage"), 0)


class TestUntrustedAndDisabled(unittest.TestCase):
    def test_negative_online_is_unreadable_not_empty(self):
        # an out-of-range count must be treated like None (untrusted), never as "empty"
        d = az.decide_map(cfg(1, 4), current=3, online=-1, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=9999), idle_drain_secs=300)
        self.assertEqual(d.action, "hold")

    def test_bool_online_is_unreadable(self):
        d = az.decide_map(cfg(1, 4), current=3, online=False, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=9999), idle_drain_secs=300)
        self.assertEqual(d.action, "hold")

    def test_disabled_map_never_scales(self):
        mc = {"enabled": False, "min_replicas": 0, "max_replicas": 2}
        # even with strong demand and an elapsed idle timer, a disabled map holds
        self.assertEqual(az.decide_map(mc, current=0, online=0, demand=9, now=NOW).action, "hold")
        self.assertEqual(az.decide_map(mc, current=2, online=0, demand=0, now=NOW,
                                       idle_since=NOW - timedelta(seconds=9999)).action, "hold")


class TestRawCfgClamp(unittest.TestCase):
    def test_malformed_cfg_clamped_to_ceiling(self):
        # decide_map is also called with raw cfg; a min>max, over-ceiling cfg must still
        # produce a target inside [0, SCALE_REPLICAS_MAX]. max clamps to SCALE_REPLICAS_MAX
        # and min clamps to that, so the floor(4) exceeds current(3) -> up to the ceiling, 4.
        mc = {"min_replicas": 5, "max_replicas": 999}
        d = az.decide_map(mc, current=3, online=0, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=9999), idle_drain_secs=300)
        self.assertEqual((d.action, d.target), ("up", az.SCALE_REPLICAS_MAX))
        self.assertLessEqual(d.target, az.SCALE_REPLICAS_MAX)

    def test_up_never_exceeds_ceiling(self):
        d = az.decide_map({"min_replicas": 0, "max_replicas": 999}, current=0, online=10_000,
                          demand=0, now=NOW, players_per_instance=1)
        self.assertEqual(d.action, "up")
        self.assertEqual(d.target, az.SCALE_REPLICAS_MAX)


class TestSimultaneousSignals(unittest.TestCase):
    def test_load_and_demand_together(self):
        d = az.decide_map(cfg(1, 4), current=1, online=70, demand=3, now=NOW,
                          players_per_instance=30)
        self.assertEqual((d.action, d.target), ("up", 3))  # load wins (3) over demand wake (1)
        self.assertEqual(d.last_demand, NOW)               # demand still stamped

    def test_overscaled_empty_drains_to_floor_in_one_jump(self):
        d = az.decide_map(cfg(1, 4), current=4, online=0, demand=0, now=NOW,
                          idle_since=NOW - timedelta(seconds=301), idle_drain_secs=300)
        self.assertEqual((d.action, d.target), ("down", 1))  # 4 -> floor 1, not 4 -> 3


class TestTimerSerialization(unittest.TestCase):
    def test_iso_roundtrip(self):
        self.assertEqual(az.parse_iso(az.iso(NOW)), NOW)

    def test_iso_none(self):
        self.assertIsNone(az.iso(None))
        self.assertIsNone(az.parse_iso(None))
        self.assertIsNone(az.parse_iso("garbage"))

    def test_timers_round_trip_back_into_decide(self):
        # the load-bearing contract: a tick's returned timers, persisted as ISO and read
        # back, must drive the next tick identically (no aware/naive drift).
        t1 = az.decide_map(cfg(1, 4), current=2, online=0, demand=0, now=NOW,
                           idle_since=None, idle_drain_secs=300)
        self.assertEqual(t1.action, "hold")
        persisted = az.iso(t1.idle_since)
        restored = az.parse_iso(persisted)
        later = NOW + timedelta(seconds=301)
        t2 = az.decide_map(cfg(1, 4), current=2, online=0, demand=0, now=later,
                           idle_since=restored, idle_drain_secs=300)
        self.assertEqual((t2.action, t2.target), ("down", 1))


class TestMultiTick(unittest.TestCase):
    """The defining behaviour: timers from one tick fed into the next."""

    def _run(self, mc, ticks, **kw):
        """Replay (now, online, demand) ticks, threading idle_since/last_demand + current."""
        idle_since = last_demand = None
        current = kw.pop("start_current", 1)
        out = []
        for now, online, demand in ticks:
            d = az.decide_map(mc, current=current, online=online, demand=demand, now=now,
                              idle_since=idle_since, last_demand=last_demand, **kw)
            idle_since, last_demand = d.idle_since, d.last_demand
            if d.action in ("up", "down"):
                current = d.target
            out.append((d.action, current))
        return out

    def test_busy_to_empty_to_drain(self):
        mc = cfg(1, 4)
        t = lambda s: NOW + timedelta(seconds=s)  # noqa: E731
        seq = self._run(mc, [
            (t(0),   60, 0),   # busy -> up to 2
            (t(60),  60, 0),   # still busy -> hold 2
            (t(120),  0, 0),   # just emptied -> hold (idle timer starts)
            (t(300),  0, 0),   # still empty, not yet 300s idle -> hold
            (t(430),  0, 0),   # 310s idle -> drain to floor 1
        ], players_per_instance=30, idle_drain_secs=300, demand_grace_secs=120, start_current=1)
        self.assertEqual([a for a, _ in seq], ["up", "hold", "hold", "hold", "down"])
        self.assertEqual(seq[-1], ("down", 1))

    def test_cold_wake_then_load_up(self):
        mc = cfg(0, 4)
        t = lambda s: NOW + timedelta(seconds=s)  # noqa: E731
        seq = self._run(mc, [
            (t(0),   0, 0),    # cold + quiet -> hold at floor 0
            (t(60),  0, 2),    # inbound travel demand -> wake to 1
            (t(120), 0, 0),    # players not landed yet, demand grace -> hold 1
            (t(180), 40, 0),   # 40 landed -> load up to 2 (ceil 40/30)
            (t(240), 40, 0),   # steady -> hold 2
        ], players_per_instance=30, idle_drain_secs=300, demand_grace_secs=120, start_current=0)
        self.assertEqual([a for a, _ in seq], ["hold", "up", "hold", "up", "hold"])
        self.assertEqual(seq[1], ("up", 1))
        self.assertEqual(seq[3], ("up", 2))


class TestReadNewLogLines(unittest.TestCase):
    def _write(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_first_run_skips_backlog(self):
        p = self._write("old 1\nold 2\n")
        lines, off = az.read_new_log_lines(p, None)
        self.assertEqual(lines, [])                 # never replay history
        self.assertEqual(off, os.path.getsize(p))

    def test_reads_only_new_complete_lines(self):
        p = self._write("a\nb\n")
        _, off = az.read_new_log_lines(p, None)     # tail from end
        with open(p, "a") as f:
            f.write("c\nd\n")
        lines, off2 = az.read_new_log_lines(p, off)
        self.assertEqual(lines, ["c", "d"])
        self.assertGreater(off2, off)

    def test_partial_line_held_until_complete(self):
        p = self._write("a\n")
        _, off = az.read_new_log_lines(p, None)
        with open(p, "a") as f:
            f.write("partial-no-newline")
        lines, off2 = az.read_new_log_lines(p, off)
        self.assertEqual(lines, [])                 # don't consume a half-written line
        self.assertEqual(off2, off)
        with open(p, "a") as f:
            f.write(" rest\n")
        lines, _ = az.read_new_log_lines(p, off2)
        self.assertEqual(lines, ["partial-no-newline rest"])

    def test_truncation_resets_to_start(self):
        p = self._write("aaaa\nbbbb\ncccc\n")
        _, off = az.read_new_log_lines(p, None)
        with open(p, "w") as f:                      # rotate: smaller than offset
            f.write("new\n")
        lines, _ = az.read_new_log_lines(p, off)
        self.assertEqual(lines, ["new"])

    def test_missing_file_keeps_offset(self):
        self.assertEqual(az.read_new_log_lines("/no/such.log", None), ([], None))
        self.assertEqual(az.read_new_log_lines("/no/such.log", 42), ([], 42))


class TestAutoscalerLedger(unittest.TestCase):
    def _led(self):
        led = az.AutoscalerLedger(":memory:")
        self.addCleanup(led.close)
        return led

    def test_state_set_get_clear(self):
        led = self._led()
        self.assertIsNone(led.get_state("k"))
        led.set_state("k", "v")
        self.assertEqual(led.get_state("k"), "v")
        led.clear_state("k")
        self.assertIsNone(led.get_state("k"))

    def test_offset_roundtrip(self):
        led = self._led()
        self.assertIsNone(led.get_offset())          # None -> first-run on the tailer
        led.set_offset(123)
        self.assertEqual(led.get_offset(), 123)

    def test_record_and_list(self):
        led = self._led()
        led.record("SH_Arrakeen", "up", "scaled 1->2")
        runs = led.list_runs(10)
        self.assertEqual((runs[0]["map"], runs[0]["action"]), ("SH_Arrakeen", "up"))


class TestLoadConfigIO(unittest.TestCase):
    def _tmp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        return d

    def test_missing_returns_defaults(self):
        c = az.load_config(self._tmp())
        self.assertFalse(c["enabled"])
        self.assertEqual(len(c["maps"]), 3)

    def test_malformed_returns_defaults(self):
        d = self._tmp()
        p = az.config_path(d)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{not json")
        self.assertEqual(len(az.load_config(d)["maps"]), 3)

    def test_write_then_load_roundtrip(self):
        d = self._tmp()
        cfg, _ = az.validate_config(
            {"enabled": True, "maps": [{"map": "SH_Arrakeen", "min_replicas": 0, "max_replicas": 2}]})
        az.write_config(d, cfg)
        c = az.load_config(d)
        self.assertTrue(c["enabled"])
        self.assertEqual([m["map"] for m in c["maps"]], ["SH_Arrakeen"])


def _snap(desired_by_map):
    """Fake mock-k8s /status snapshot resolve_scale_key can parse."""
    return {"maps": [{"map": m, "key": f"default/{m.lower()}", "desired": d}
                     for m, d in desired_by_map.items()]}


class TestRunTick(unittest.TestCase):
    """The tick wiring, with mock-k8s / counts / tailer faked over an in-memory ledger."""

    def setUp(self):
        self.led = az.AutoscalerLedger(":memory:")
        self.addCleanup(self.led.close)
        self.led.set_offset(0)  # past the tailer's first-run branch (don't touch real logs)
        self.patches = []

    def _fakes(self, *, status, counts, demand_lines=([], 0), patch_ret=(200, {})):
        self.calls = []

        def fake_patch(method, path, body=None, **kw):
            self.calls.append((path, body))
            return patch_ret
        for name, fn in (("fetch_mock_status", lambda *a, **k: status),
                         ("read_online_counts", lambda b: counts),
                         ("read_new_log_lines", lambda p, o: demand_lines),
                         ("mock_k8s_request", fake_patch)):
            p = patch.object(az, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_all_at_floor_no_patch(self):
        self._fakes(status=_snap({"DeepDesert_1": 1, "SH_Arrakeen": 1, "SH_HarkoVillage": 1}), counts={})
        cfg, _ = az.validate_config({})  # defaults: warm floor 1
        self.assertEqual(az.run_tick("/base", cfg, self.led), [])
        self.assertEqual(self.calls, [])

    def test_drain_fires_patch_to_floor(self):
        self._fakes(status=_snap({"SH_Arrakeen": 2}), counts={"SH_Arrakeen": 0})
        cfg, _ = az.validate_config(
            {"maps": [{"map": "SH_Arrakeen", "min_replicas": 1, "max_replicas": 4}], "idle_drain_secs": 300})
        self.led.set_state("idle:SH_Arrakeen", az.iso(az._now() - timedelta(seconds=400)))
        actions = az.run_tick("/base", cfg, self.led)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], {"spec": {"replicas": 1}})
        self.assertEqual((actions[0][0], actions[0][1], actions[0][2]), ("SH_Arrakeen", "down", True))

    def test_load_up_patches_target(self):
        self._fakes(status=_snap({"SH_Arrakeen": 1}), counts={"SH_Arrakeen": 70})
        cfg, _ = az.validate_config(
            {"maps": [{"map": "SH_Arrakeen", "min_replicas": 1, "max_replicas": 4}], "players_per_instance": 30})
        actions = az.run_tick("/base", cfg, self.led)
        self.assertEqual(self.calls[0][1], {"spec": {"replicas": 3}})  # ceil(70/30)
        self.assertEqual(actions[0][1], "up")

    def test_cold_wake_on_demand(self):
        self._fakes(status=_snap({"SH_Arrakeen": 0}), counts={"SH_Arrakeen": 0},
                    demand_lines=(["Processing travel queue for SH_Arrakeen (dimension=0, num=2)"], 50))
        cfg, _ = az.validate_config({"maps": [{"map": "SH_Arrakeen", "min_replicas": 0, "max_replicas": 2}]})
        actions = az.run_tick("/base", cfg, self.led)
        self.assertEqual(self.calls[0][1], {"spec": {"replicas": 1}})
        self.assertEqual(actions[0][1], "up")
        self.assertEqual(self.led.get_offset(), 50)  # offset advanced

    def test_mock_down_skips_everything(self):
        self._fakes(status=None, counts=None)
        cfg, _ = az.validate_config({})
        self.assertEqual(az.run_tick("/base", cfg, self.led), [])
        self.assertEqual(self.calls, [])

    def test_unreadable_counts_never_drains(self):
        self._fakes(status=_snap({"SH_Arrakeen": 2}), counts=None)  # server-status failed
        cfg, _ = az.validate_config(
            {"maps": [{"map": "SH_Arrakeen", "min_replicas": 1, "max_replicas": 4}], "idle_drain_secs": 300})
        self.led.set_state("idle:SH_Arrakeen", az.iso(az._now() - timedelta(seconds=9999)))
        self.assertEqual(az.run_tick("/base", cfg, self.led), [])
        self.assertEqual(self.calls, [])  # blind -> no scale-down

    def test_timers_persist_across_ticks(self):
        self._fakes(status=_snap({"SH_Arrakeen": 2}), counts={"SH_Arrakeen": 0})
        cfg, _ = az.validate_config(
            {"maps": [{"map": "SH_Arrakeen", "min_replicas": 1, "max_replicas": 4}], "idle_drain_secs": 300})
        az.run_tick("/base", cfg, self.led)  # empties now -> idle timer starts, no drain yet
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.led.get_state("idle:SH_Arrakeen"))


class TestReadOnlineCounts(unittest.TestCase):
    """The emptiness signal MUST be farm_state.connected_players (live socket count),
    not server-status (which groups by the character's home map and misses hub visitors
    — the bug that drained Harko under a player and kicked them)."""

    @staticmethod
    def _proc(rc=0, out="", err=""):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def test_uses_farm_player_count_not_server_status(self):
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return self._proc(out="map,players\nSH_HarkoVillage,2\nDeepDesert_1,0\n")
        with patch.object(az.subprocess, "run", fake_run):
            out = az.read_online_counts("/base")
        self.assertEqual(out, {"SH_HarkoVillage": 2, "DeepDesert_1": 0})
        self.assertIn("farm-player-count", calls[0])
        self.assertNotIn("server-status", calls[0])

    def test_nonzero_exit_is_none(self):
        with patch.object(az.subprocess, "run", lambda *a, **k: self._proc(rc=1, err="boom")):
            self.assertIsNone(az.read_online_counts("/base"))

    def test_subprocess_error_is_none(self):
        def boom(*a, **k):
            raise OSError("nope")
        with patch.object(az.subprocess, "run", boom):
            self.assertIsNone(az.read_online_counts("/base"))


class TestLiveStatus(unittest.TestCase):
    def test_merges_desired_current_players(self):
        snap = {"maps": [{"map": "SH_HarkoVillage", "desired": 1, "current": 1, "status": "healthy"},
                         {"map": "SH_Arrakeen", "desired": 0, "current": 0, "status": "idle"}]}
        cfg = {"maps": [{"map": "SH_HarkoVillage"}, {"map": "SH_Arrakeen"}, {"map": "DeepDesert_1"}]}
        with patch.object(az, "fetch_mock_status", lambda *a, **k: snap), \
             patch.object(az, "read_online_counts", lambda b: {"SH_HarkoVillage": 2}):
            live, sources = az.live_status("/base", cfg)
        self.assertEqual(live["SH_HarkoVillage"],
                         {"desired": 1, "current": 1, "status": "healthy", "players": 2})
        self.assertEqual(live["SH_Arrakeen"]["players"], 0)      # readable, just absent -> 0
        self.assertIsNone(live["DeepDesert_1"]["desired"])       # not in the mock snapshot
        self.assertEqual(sources, {"mockK8s": True, "players": True})

    def test_players_none_when_unreadable(self):
        with patch.object(az, "fetch_mock_status", lambda *a, **k: None), \
             patch.object(az, "read_online_counts", lambda b: None):
            live, sources = az.live_status("/base", {"maps": [{"map": "SH_Arrakeen"}]})
        self.assertIsNone(live["SH_Arrakeen"]["players"])        # unreadable, not 0
        self.assertEqual(sources, {"mockK8s": False, "players": False})


class TestWebhookNotify(unittest.TestCase):
    def test_format_actions_empty(self):
        self.assertEqual(az.format_actions([]), "")

    def test_format_actions_lists_each(self):
        msg = az.format_actions([("SH_HarkoVillage", "up", True, "wake to 1"),
                                 ("SH_Arrakeen", "down", False, "PATCH http 500")])
        self.assertIn("SH_HarkoVillage", msg)
        self.assertIn("up", msg)
        self.assertIn("⚠️", msg)  # the failed action is flagged

    def test_notify_noop_without_url_or_text(self):
        self.assertFalse(az.notify("", "hi"))
        self.assertFalse(az.notify("https://x", ""))

    def test_notify_posts_when_url_set(self):
        sent = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=5):
            sent["url"], sent["data"] = req.full_url, req.data
            return _Resp()
        with patch.object(az.urllib.request, "urlopen", fake_urlopen):
            ok = az.notify("https://discord/webhook", "hello")
        self.assertTrue(ok)
        self.assertEqual(sent["url"], "https://discord/webhook")
        self.assertIn(b"hello", sent["data"])

    def test_notify_swallows_errors(self):
        def boom(*a, **k):
            raise OSError("net down")
        with patch.object(az.urllib.request, "urlopen", boom):
            self.assertFalse(az.notify("https://x", "hi"))


class TestValidateWebhook(unittest.TestCase):
    def test_accepted_and_stripped(self):
        c, err = az.validate_config({"webhook_url": "  https://discord/w  "})
        self.assertIsNone(err)
        self.assertEqual(c["webhook_url"], "https://discord/w")

    def test_default_empty(self):
        self.assertEqual(az.validate_config({})[0]["webhook_url"], "")

    def test_nonstring_rejected(self):
        self.assertIsNotNone(az.validate_config({"webhook_url": 123})[1])


def _dim(pid, online, players):
    return {"partition_id": pid, "dimension_index": pid - 100, "online": online, "players": players}


class TestDecideDimensionPool(unittest.TestCase):
    def test_spawn_to_floor(self):
        dims = [_dim(101, False, 0), _dim(102, False, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=1, max_dims=3)
        self.assertEqual((d.action, d.target), ("spawn", 101))  # lowest offline, reach floor

    def test_spawn_on_load(self):
        dims = [_dim(101, True, 30), _dim(102, False, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, players_per_dim=30, max_dims=3, min_dims=1)
        self.assertEqual((d.action, d.target), ("spawn", 102))

    def test_no_spawn_below_threshold(self):
        dims = [_dim(101, True, 20), _dim(102, False, 0)]
        self.assertEqual(az.decide_dimension_pool(dims, now=NOW, players_per_dim=30, min_dims=1).action, "hold")

    def test_no_spawn_at_max(self):
        dims = [_dim(101, True, 99), _dim(102, True, 99), _dim(103, True, 99)]
        self.assertEqual(az.decide_dimension_pool(dims, now=NOW, players_per_dim=30, max_dims=3).action, "hold")

    def test_no_spawn_without_offline_row(self):
        dims = [_dim(101, True, 99)]  # over threshold but nothing left to spawn
        self.assertEqual(az.decide_dimension_pool(dims, now=NOW, players_per_dim=30, max_dims=4).action, "hold")

    def test_spawn_cooldown_blocks(self):
        dims = [_dim(101, True, 99), _dim(102, False, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, players_per_dim=30, max_dims=3,
                                     last_spawn=NOW - timedelta(seconds=60), demand_grace_secs=300)
        self.assertEqual(d.action, "hold")

    def test_reap_empty_past_idle(self):
        dims = [_dim(101, True, 5), _dim(102, True, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=1, idle_drain_secs=600,
                                     idle_since={102: NOW - timedelta(seconds=700)})
        self.assertEqual((d.action, d.target), ("reap", 102))

    def test_never_reap_occupied(self):
        # 102 is occupied; even with a stale idle entry it must never be reaped
        dims = [_dim(101, True, 3), _dim(102, True, 8)]
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=1, idle_drain_secs=600,
                                     idle_since={102: NOW - timedelta(seconds=9999)})
        self.assertEqual(d.action, "hold")

    def test_never_reap_below_floor(self):
        dims = [_dim(101, True, 0)]  # lone live dim, empty + idle, but min floor is 1
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=1, idle_drain_secs=600,
                                     idle_since={101: NOW - timedelta(seconds=9999)})
        self.assertEqual((d.action, d.target), ("hold", None))

    def test_reap_picks_highest_empty(self):
        dims = [_dim(101, True, 4), _dim(102, True, 0), _dim(103, True, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=1, idle_drain_secs=600,
                                     idle_since={102: NOW - timedelta(seconds=700), 103: NOW - timedelta(seconds=700)})
        self.assertEqual((d.action, d.target), ("reap", 103))

    def test_reap_blocked_in_cooldown(self):
        dims = [_dim(101, True, 5), _dim(102, True, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=1, idle_drain_secs=600,
                                     idle_since={102: NOW - timedelta(seconds=700)},
                                     last_spawn=NOW - timedelta(seconds=60), demand_grace_secs=300)
        self.assertEqual(d.action, "hold")

    def test_blind_holds(self):
        dims = [_dim(101, True, 5), {"partition_id": 102, "online": True, "players": None}]
        self.assertEqual(az.decide_dimension_pool(dims, now=NOW).action, "hold")

    def test_spawn_wins_over_reap(self):
        # avg over threshold AND an empty idle dim exists -> spawn (one action, up first)
        dims = [_dim(101, True, 60), _dim(102, True, 0), _dim(103, False, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, players_per_dim=30, max_dims=3, min_dims=1,
                                     idle_drain_secs=600, idle_since={102: NOW - timedelta(seconds=700)})
        self.assertEqual(d.action, "spawn")

    def test_max_clamped_to_declared(self):
        # max_dims 5 but only 2 rows declared, both full -> can't exceed 2 -> hold
        dims = [_dim(101, True, 99), _dim(102, True, 99)]
        self.assertEqual(az.decide_dimension_pool(dims, now=NOW, players_per_dim=30, max_dims=5).action, "hold")

    def test_idle_timer_starts_and_clears(self):
        dims = [_dim(101, True, 0), _dim(102, True, 7)]
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=2)  # at floor, no reap
        self.assertEqual(d.idle_since.get(101), NOW)   # empty -> timer started
        self.assertNotIn(102, d.idle_since)            # occupied -> no timer


class TestParseDimRows(unittest.TestCase):
    def test_parses_and_gates_on_alive(self):
        csv = ("partition_id,dimension_index,server_id,game_port,ready,alive,connected_players,label\n"
               "101,1,abc,7790,true,true,3,Deep Desert\n"          # live, 3 players
               "102,2,,,,,0,Deep Desert\n"                          # declared/offline
               "103,3,stale,7792,false,false,9,Deep Desert\n")      # stale (alive=false) -> offline, 0
        rows = az.parse_dim_rows(csv)
        self.assertEqual(rows[0], {"partition_id": 101, "dimension_index": 1, "online": True, "players": 3, "label": "Deep Desert"})
        self.assertEqual((rows[1]["online"], rows[1]["players"]), (False, 0))
        self.assertEqual((rows[2]["online"], rows[2]["players"]), (False, 0))  # stale players NOT trusted


class TestValidateDeepDesert(unittest.TestCase):
    def test_default_present(self):
        dd = az.validate_config({})[0]["deep_desert"]
        self.assertEqual(dd["enabled"], False)
        self.assertEqual((dd["min_dims"], dd["max_dims"], dd["players_per_dim"]), (1, 3, 30))

    def test_enabled_coercion_and_clamp(self):
        dd = az.validate_config({"deep_desert": {"enabled": "true", "min_dims": 9, "max_dims": 2}})[0]["deep_desert"]
        self.assertTrue(dd["enabled"])
        self.assertEqual(dd["min_dims"], 2)  # clamped to max_dims

    def test_nonint_rejected(self):
        self.assertIsNotNone(az.validate_config({"deep_desert": {"players_per_dim": "x"}})[1])

    def test_nondict_rejected(self):
        self.assertIsNotNone(az.validate_config({"deep_desert": "nope"})[1])


class TestDimInFlightAndCooldown(unittest.TestCase):
    def test_inflight_lock_holds(self):
        # a spawn in flight blocks ALL action, even with load over the threshold
        dims = [_dim(101, True, 99), _dim(102, False, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, spawning=102, players_per_dim=30, max_dims=3, min_dims=1)
        self.assertEqual(d.action, "hold")

    def test_reap_arms_cooldown(self):
        dims = [_dim(101, True, 3), _dim(102, True, 0)]
        d = az.decide_dimension_pool(dims, now=NOW, min_dims=1, idle_drain_secs=600,
                                     idle_since={102: NOW - timedelta(seconds=700)})
        self.assertEqual((d.action, d.target), ("reap", 102))
        self.assertEqual(d.last_spawn, NOW)  # reap arms the cooldown (anti reap->respawn flap)

    def test_demand_grace_floored(self):
        dd = az.validate_config({"deep_desert": {"demand_grace_secs": 10}})[0]["deep_desert"]
        self.assertGreaterEqual(dd["demand_grace_secs"], az.SPAWN_COOLDOWN_FLOOR)


class TestRunDdTick(unittest.TestCase):
    """Multi-tick DD I/O over a real in-memory ledger with faked read/actuate."""

    def setUp(self):
        self.led = az.AutoscalerLedger(":memory:")
        self.addCleanup(self.led.close)
        self.calls = []

    def _run(self, rows, now, cfg=None, ok=True):
        cfg = cfg or {"min_dims": 1, "max_dims": 3, "players_per_dim": 30,
                      "idle_drain_secs": 600, "demand_grace_secs": 300}

        def fake_pub(b, v, p):
            self.calls.append((v, p))
            return ok
        with patch.object(az, "read_dim_rows", lambda b: rows), \
             patch.object(az, "_run_publish_dim", fake_pub):
            return az.run_dd_tick("/b", cfg, self.led, now)

    def test_inflight_lock_blocks_respawn(self):
        cfg = {"min_dims": 2, "max_dims": 3, "players_per_dim": 30, "idle_drain_secs": 600, "demand_grace_secs": 300}
        rows = [_dim(101, True, 5), _dim(102, False, 0), _dim(103, False, 0)]
        self._run(rows, NOW, cfg)
        self.assertEqual(self.calls, [("dimension-up", 102)])
        self.assertEqual(self.led.get_state("dd_spawning_pid"), "102")
        # cooldown expired AND 102 still booting -> the lock holds, no second spawn
        self._run(rows, NOW + timedelta(seconds=400), cfg)
        self.assertEqual(self.calls, [("dimension-up", 102)])

    def test_inflight_cleared_when_live(self):
        cfg = {"min_dims": 2, "max_dims": 3, "demand_grace_secs": 300}
        self._run([_dim(101, True, 5), _dim(102, False, 0)], NOW, cfg)
        self.assertEqual(self.led.get_state("dd_spawning_pid"), "102")
        self._run([_dim(101, True, 5), _dim(102, True, 0)], NOW + timedelta(seconds=350), cfg)
        self.assertIsNone(self.led.get_state("dd_spawning_pid"))  # went live -> marker cleared

    def test_inflight_times_out_then_retries(self):
        cfg = {"min_dims": 2, "max_dims": 3, "demand_grace_secs": 300}
        rows = [_dim(101, True, 5), _dim(102, False, 0)]
        self._run(rows, NOW, cfg)
        # never came up; past the in-flight window -> marker cleared -> retry
        self._run(rows, NOW + timedelta(seconds=az.SPAWN_INFLIGHT_MAX_WAIT + 10), cfg)
        self.assertEqual(self.calls, [("dimension-up", 102), ("dimension-up", 102)])

    def test_reap_after_idle(self):
        rows = [_dim(101, True, 5), _dim(102, True, 0)]
        self._run(rows, NOW)                                    # 102 empty -> timer starts, no reap
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.led.get_state("dd_idle:102"))
        self._run(rows, NOW + timedelta(seconds=650))          # idle elapsed -> reap
        self.assertEqual(self.calls, [("dimension-down", 102)])

    def test_idle_timer_no_leak_on_vanish(self):
        self._run([_dim(101, True, 5), _dim(102, True, 0)], NOW)
        self.assertIsNotNone(self.led.get_state("dd_idle:102"))
        # 102 drops out of the query result -> its stale timer must be cleared
        self._run([_dim(101, True, 5)], NOW + timedelta(seconds=300))
        self.assertIsNone(self.led.get_state("dd_idle:102"))
        # reappears empty -> fresh timer, NOT reaped on first sight
        self._run([_dim(101, True, 5), _dim(102, True, 0)], NOW + timedelta(seconds=400))
        self.assertEqual(self.calls, [])

    def test_failed_reap_keeps_timer(self):
        rows = [_dim(101, True, 5), _dim(102, True, 0)]
        self.led.set_state("dd_idle:102", az.iso(NOW - timedelta(seconds=700)))
        self._run(rows, NOW, ok=False)                         # reap attempted, actuator fails
        self.assertEqual(self.calls, [("dimension-down", 102)])
        self.assertIsNotNone(self.led.get_state("dd_idle:102"))  # timer preserved for retry

    def test_blind_read_holds(self):
        with patch.object(az, "read_dim_rows", lambda b: None):
            self.assertEqual(az.run_dd_tick("/b", {"min_dims": 1}, self.led, NOW), [])
        self.assertEqual(self.calls, [])


class TestSummarizeDimPool(unittest.TestCase):
    """The DD-pool live view for the SPA (read-only mirror of what the scaler sees)."""

    def test_none_rows_unreadable(self):
        s = az.summarize_dim_pool(None, {"enabled": True, "min_dims": 1, "max_dims": 3})
        self.assertFalse(s["readable"])
        self.assertEqual(s["dims"], [])
        self.assertEqual(s["live"], 0)
        self.assertEqual(s["players"], 0)
        self.assertTrue(s["enabled"])

    def test_aggregates_live_and_players(self):
        rows = [_dim(101, True, 5), _dim(102, True, 0), _dim(103, False, 0)]
        s = az.summarize_dim_pool(rows, {"enabled": True, "min_dims": 1, "max_dims": 3, "players_per_dim": 30})
        self.assertTrue(s["readable"])
        self.assertEqual(s["live"], 2)
        self.assertEqual(s["declared"], 3)
        self.assertEqual(s["players"], 5)
        self.assertEqual([d["partition_id"] for d in s["dims"]], [101, 102, 103])  # sorted by pid

    def test_max_dims_clamped_to_declared(self):
        rows = [_dim(101, True, 0), _dim(102, False, 0)]  # only 2 declared rows
        s = az.summarize_dim_pool(rows, {"min_dims": 1, "max_dims": 5})
        self.assertEqual(s["max_dims"], 2)   # ceiling clamped to declared, like decide_dimension_pool
        self.assertEqual(s["min_dims"], 1)

    def test_min_dims_clamped_to_effective_max(self):
        rows = [_dim(101, True, 0)]           # declared=1
        s = az.summarize_dim_pool(rows, {"min_dims": 5, "max_dims": 3})
        self.assertEqual(s["max_dims"], 1)
        self.assertEqual(s["min_dims"], 1)   # floor clamped down to the effective ceiling

    def test_offline_dim_players_never_trusted(self):
        # an offline row reporting players must contribute 0 (matches parse_dim_rows + the scaler)
        rows = [_dim(101, True, 4), _dim(102, False, 99)]
        s = az.summarize_dim_pool(rows, {"min_dims": 0, "max_dims": 3})
        self.assertEqual(s["players"], 4)
        off = next(d for d in s["dims"] if d["partition_id"] == 102)
        self.assertEqual(off["players"], 0)
        self.assertFalse(off["online"])

    def test_dd_live_status_reads_rows(self):
        with patch.object(az, "read_dim_rows", lambda b: [_dim(101, True, 2)]):
            s = az.dd_live_status("/base", {"enabled": True, "min_dims": 1, "max_dims": 3})
        self.assertTrue(s["readable"])
        self.assertEqual(s["live"], 1)
        self.assertEqual(s["players"], 2)

    def test_dd_live_status_unreadable(self):
        with patch.object(az, "read_dim_rows", lambda b: None):
            s = az.dd_live_status("/base", {"min_dims": 1, "max_dims": 3})
        self.assertFalse(s["readable"])


class TestStatusDeepDesertBlock(unittest.TestCase):
    """_status() surfaces a deep_desert block whenever the config carries one."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.base, "server", "state", "admin"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_status_includes_dd_when_configured(self):
        cfg_obj = {"enabled": False, "maps": [],
                   "deep_desert": {"enabled": True, "min_dims": 1, "max_dims": 3}}
        with patch.object(az, "load_config", lambda b: cfg_obj), \
             patch.object(az, "live_status", lambda b, c: ({}, {"mockK8s": True, "players": True})), \
             patch.object(az, "read_dim_rows", lambda b: [_dim(101, True, 3), _dim(102, False, 0)]):
            out = az._status(self.base)
        self.assertIn("deep_desert", out)
        self.assertEqual(out["deep_desert"]["live"], 1)
        self.assertEqual(out["deep_desert"]["players"], 3)

    def test_status_omits_dd_when_absent(self):
        with patch.object(az, "load_config", lambda b: {"enabled": False, "maps": []}), \
             patch.object(az, "live_status", lambda b, c: ({}, {"mockK8s": True, "players": True})):
            out = az._status(self.base)
        self.assertNotIn("deep_desert", out)


if __name__ == "__main__":
    unittest.main()
