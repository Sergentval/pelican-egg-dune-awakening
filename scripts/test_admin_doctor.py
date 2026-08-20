#!/usr/bin/env python3
"""Connection-doctor verdict logic (2026-08 ecosystem-survey port C2).

Facts gathering (SQL, ss, curl, log tail) lives in admin-publish.sh `doctor`;
this tests the pure analysis: every check's ok/warn/error path against
synthetic facts. Mechanisms ported from coastal-ms/DST-DuneServerTool P34
(Apache-2.0) and Red-Blink/dune-awakening-selfhost-docker doctor.sh (MIT).

Run: python3 scripts/test_admin_doctor.py
"""
import time
import unittest

from admin_doctor import analyze, is_private_ip


def row(**kw):
    base = {"server_id": "s1", "map": "Survival_1", "game_addr": "51.178.25.173",
            "game_port": 7777, "igw_addr": "51.178.25.173", "igw_port": 7950,
            "ready": True, "alive": True, "connected_players": 0}
    base.update(kw)
    return base


def facts(**kw):
    base = {
        "external_ip": "51.178.25.173",
        "real_ip": "51.178.25.173",
        "farm": [row()],
        "partition_maps": ["Survival_1", "Overmap", "DeepDesert_1"],
        "udp_ports": [7777],
        "heartbeat_epoch": int(time.time()) - 30,
        "now_epoch": int(time.time()),
    }
    base.update(kw)
    return base


def by_id(checks):
    return {c["id"]: c for c in checks}


class TestIpChecks(unittest.TestCase):
    def test_all_green(self):
        checks = by_id(analyze(facts()))
        for cid in ("external-ip-set", "external-ip-public", "external-vs-real",
                    "advertised-addresses", "igw-addresses", "port-validity",
                    "port-collisions", "ports-bound", "ready-stuck",
                    "farm-partition-coherence", "fls-heartbeat"):
            self.assertEqual(checks[cid]["status"], "ok", cid)

    def test_missing_external_ip_is_error(self):
        checks = by_id(analyze(facts(external_ip="")))
        self.assertEqual(checks["external-ip-set"]["status"], "error")

    def test_private_external_ip_warns(self):
        checks = by_id(analyze(facts(external_ip="192.168.1.10", real_ip="")))
        self.assertEqual(checks["external-ip-public"]["status"], "warn")

    def test_is_private_ip(self):
        for ip in ("10.0.0.1", "172.16.5.5", "192.168.0.2", "127.0.0.1"):
            self.assertTrue(is_private_ip(ip), ip)
        for ip in ("51.178.25.173", "8.8.8.8"):
            self.assertFalse(is_private_ip(ip), ip)

    def test_real_ip_mismatch_is_error(self):
        checks = by_id(analyze(facts(real_ip="1.2.3.4")))
        self.assertEqual(checks["external-vs-real"]["status"], "error")

    def test_unknown_real_ip_skips(self):
        checks = by_id(analyze(facts(real_ip="")))
        self.assertEqual(checks["external-vs-real"]["status"], "skip")


class TestFarmChecks(unittest.TestCase):
    def test_advertised_drift_is_error(self):
        # DST P34's core finding: a map advertising an address that is not
        # the external IP is unjoinable, and the panel looks healthy.
        f = facts(farm=[row(), row(server_id="s2", map="Overmap", game_addr="10.0.3.2")])
        checks = by_id(analyze(f))
        self.assertEqual(checks["advertised-addresses"]["status"], "error")
        self.assertIn("Overmap", checks["advertised-addresses"]["detail"])

    def test_igw_drift_is_error(self):
        # Wrong inter-world gateway address = map travel hangs (2G2 family).
        f = facts(farm=[row(igw_addr="172.17.0.2")])
        checks = by_id(analyze(f))
        self.assertEqual(checks["igw-addresses"]["status"], "error")

    def test_loopback_igw_is_ok(self):
        # Everything lives in ONE container on this stack, so S2S travel over
        # loopback is the design, not a drift (live-verified).
        f = facts(farm=[row(igw_addr="127.0.0.1")])
        checks = by_id(analyze(f))
        self.assertEqual(checks["igw-addresses"]["status"], "ok")

    def test_port_collision_is_error(self):
        f = facts(farm=[row(), row(server_id="s2", map="Overmap", game_port=7777)],
                  udp_ports=[7777])
        checks = by_id(analyze(f))
        self.assertEqual(checks["port-collisions"]["status"], "error")
        self.assertIn("7777", checks["port-collisions"]["detail"])

    def test_unbound_port_is_error(self):
        f = facts(udp_ports=[7999])
        checks = by_id(analyze(f))
        self.assertEqual(checks["ports-bound"]["status"], "error")

    def test_dead_rows_do_not_flag_ports(self):
        f = facts(farm=[row(alive=False, ready=False)], udp_ports=[])
        checks = by_id(analyze(f))
        self.assertEqual(checks["ports-bound"]["status"], "ok")
        self.assertEqual(checks["port-collisions"]["status"], "ok")

    def test_alive_row_without_port_warns(self):
        # A row the port checks cannot examine must be surfaced, not silently
        # exempted from both collision and listener checks.
        f = facts(farm=[row(game_port=None)])
        checks = by_id(analyze(f))
        self.assertEqual(checks["port-validity"]["status"], "warn")
        self.assertEqual(checks["port-collisions"]["status"], "ok")

    def test_ready_stuck_warns(self):
        f = facts(farm=[row(ready=False)])
        checks = by_id(analyze(f))
        self.assertEqual(checks["ready-stuck"]["status"], "warn")

    def test_farm_map_without_partition_warns(self):
        f = facts(farm=[row(map="SH_FallenLight")])
        checks = by_id(analyze(f))
        self.assertEqual(checks["farm-partition-coherence"]["status"], "warn")

    def test_empty_farm_warns(self):
        checks = by_id(analyze(facts(farm=[])))
        self.assertEqual(checks["farm-registrations"]["status"], "warn")


class TestHeartbeat(unittest.TestCase):
    def test_stale_heartbeat_is_error(self):
        now = int(time.time())
        checks = by_id(analyze(facts(heartbeat_epoch=now - 3600, now_epoch=now)))
        self.assertEqual(checks["fls-heartbeat"]["status"], "error")

    def test_missing_heartbeat_warns(self):
        checks = by_id(analyze(facts(heartbeat_epoch=None)))
        self.assertEqual(checks["fls-heartbeat"]["status"], "warn")


class TestOverall(unittest.TestCase):
    def test_summary_counts(self):
        from admin_doctor import summarize
        checks = analyze(facts(real_ip="1.2.3.4", external_ip="192.168.1.4"))
        s = summarize(checks)
        self.assertGreaterEqual(s["error"], 1)
        self.assertEqual(s["total"], len(checks))


if __name__ == "__main__":
    unittest.main()
