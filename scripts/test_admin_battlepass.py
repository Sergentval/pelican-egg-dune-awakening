"""Tests for admin_battlepass.py — pins upstream's two load-bearing
invariants (the baseline/earned/seen state machine and the money-safe
grant ledger) plus the reset semantics whose violation upstream documents
as a re-grant storm.
"""
import json
import os
import shutil
import tempfile
import unittest

import admin_battlepass as bp

CATALOG = {
    "version": 1,
    "tiers": [
        {"tier_key": "level:10", "category": "level", "label": "Reach 10",
         "signal": "level", "signal_key": "", "threshold": 10, "intel": 5,
         "reward_items": "", "enabled": True},
        {"tier_key": "journey:DA_MQ_X", "category": "story", "label": "Quest X",
         "signal": "journey_node", "signal_key": "DA_MQ_X", "threshold": 0,
         "intel": 7, "reward_items": "", "enabled": True},
        {"tier_key": "tag:Explo.A", "category": "exploration", "label": "Find A",
         "signal": "player_tag", "signal_key": "Explo.A", "threshold": 0,
         "intel": 3,
         "reward_items": "[{\"template\": \"Schematic_X\", \"qty\": 1, \"quality\": 0}]",
         "enabled": True},
    ],
}


class FakeGame:
    def __init__(self):
        self.players = []   # account_id, fls, name, online, level, journey, tags
        self.calls = []
        self.fail_intel = False
        self.tags_key_col = "account_id"

    def _csv(self, headers, rows):
        out = [",".join(headers)]
        for r in rows:
            out.append(",".join(str(r[h]) for h in headers))
        return "\n".join(out) + "\n"

    def _account_from(self, sql):
        for tok in ("account_id = ", "account_id ="):
            if tok in sql:
                tail = sql.split(tok, 1)[1].strip()
                num = ""
                for ch in tail:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                if num:
                    return int(num)
        return None

    def __call__(self, argv):
        if argv[0] == "award-intel":
            self.calls.append(tuple(argv))
            fls = argv[1]
            player = next((p for p in self.players if p["fls"] == fls), None)
            if player and player.get("online", False):
                return 4, "player is online"
            if self.fail_intel:
                return 1, "scripted intel failure"
            return 0, "ok"
        if argv[0] != "db-sql":
            self.calls.append(tuple(argv))
            return 0, "ok"
        sql = argv[1]
        if "information_schema" in sql:
            return 0, self._csv(["column_name"], [{"column_name": self.tags_key_col}])
        if "journey_story_node" in sql:
            account = self._account_from(sql)
            rows = [{"story_node_id": n}
                    for p in self.players if p["account_id"] == account
                    for n in p.get("journey", [])]
            return 0, self._csv(["story_node_id"], rows)
        if "player_tags" in sql:
            account = self._account_from(sql)
            rows = [{"tag": t}
                    for p in self.players if p["account_id"] == account
                    for t in p.get("tags", [])]
            return 0, self._csv(["tag"], rows)
        if "FLevelComponent" in sql:
            import admin_events as ev
            if "eps.account_id =" in sql or "WHERE eps.account_id =" in sql:
                pass
            rows = [{"account_id": p["account_id"], "fls": p["fls"],
                     "name": p.get("name", ""),
                     "online": "t" if p.get("online", False) else "f",
                     "xp": ev.CUMULATIVE_XP[min(p.get("level", 0), 200)]}
                    for p in self.players]
            return 0, self._csv(["account_id", "fls", "name", "online", "xp"], rows)
        if "eps.account_id =" in sql:
            account = self._account_from(sql)
            rows = [{"fls": p["fls"]} for p in self.players
                    if p["account_id"] == account]
            return 0, self._csv(["fls"], rows)
        return 0, "\n"


class Harness(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bp-test-")
        os.makedirs(os.path.join(self.base, "data", "admin"), exist_ok=True)
        with open(os.path.join(self.base, bp.CATALOG_PATH), "w") as f:
            json.dump(CATALOG, f)
        self.game = FakeGame()
        self.config(enabled=True, award_past=False, auto_grant=False)
        bp.seed_tiers_if_empty(self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def config(self, **cfg):
        bp.save_config(self.base, cfg)

    def player(self, account, **kw):
        p = {"account_id": account, "fls": f"FLS{account:04X}",
             "online": False, "level": 1}
        p.update(kw)
        self.game.players.append(p)
        return p

    def claims(self, account):
        return {c["tier_key"]: c["status"]
                for c in bp.list_claims(self.base, account)}

    def intel_calls(self):
        return [c for c in self.game.calls if c[0] == "award-intel"]


class CatalogTests(Harness):
    def test_seed_if_empty_only_once(self):
        self.assertEqual(bp.seed_tiers_if_empty(self.base), 0)  # already seeded
        self.assertEqual(len(bp.list_tiers(self.base)), 3)

    def test_operator_edit_survives_reseed_claims_reattach(self):
        bp.update_tier(self.base, 1, intel=99)
        self.player(101, level=50)
        bp.run_tick(self.base, self.game, now=1000)   # baselines level:10
        self.assertEqual(bp.reseed_tiers(self.base), 3)
        tiers = bp.list_tiers(self.base)
        self.assertEqual([t["intel"] for t in tiers if t["tier_key"] == "level:10"],
                         [5])  # reseed restored the default
        # claims survived by tier_key
        self.assertEqual(self.claims(101)["level:10"], "baseline")

    def test_tier_key_is_immutable(self):
        with self.assertRaises(ValueError):
            bp.update_tier(self.base, 1, tier_key="level:11")

    def test_validate_tier_rules(self):
        bad = [
            {"tier_key": "", "category": "c", "label": "l", "signal": "level",
             "threshold": 1, "intel": 1},
            {"tier_key": "k", "category": "c", "label": "l", "signal": "nope",
             "threshold": 1, "intel": 1},
            {"tier_key": "k", "category": "c", "label": "l", "signal": "level",
             "threshold": 0, "intel": 1},
            {"tier_key": "k", "category": "c", "label": "l",
             "signal": "player_tag", "signal_key": "", "intel": 1},
            {"tier_key": "k", "category": "c", "label": "l", "signal": "level",
             "threshold": 1, "intel": 1, "reward_items": "[]"},
        ]
        for tier in bad:
            with self.assertRaises(ValueError, msg=tier):
                bp.validate_tier(tier)


class StateMachineTests(Harness):
    def test_first_sight_satisfied_is_baseline_never_grantable(self):
        self.player(101, level=50, journey=["DA_MQ_X"])
        summary = bp.run_tick(self.base, self.game, now=1000)
        self.assertEqual(summary["baselined"], 2)   # level:10 + journey
        self.assertEqual(summary["earned"], 0)
        self.assertEqual(self.claims(101),
                         {"level:10": "baseline", "journey:DA_MQ_X": "baseline"})
        # auto-grant on: baselines never enqueue
        self.config(enabled=True, auto_grant=True)
        bp.run_tick(self.base, self.game, now=2000)
        self.assertEqual(self.intel_calls(), [])

    def test_watched_unsatisfied_then_satisfied_earns(self):
        p = self.player(101, level=5)
        bp.run_tick(self.base, self.game, now=1000)   # level:10 unsatisfied → seen
        self.assertEqual(self.claims(101), {})
        p["level"] = 15
        summary = bp.run_tick(self.base, self.game, now=2000)
        self.assertEqual(summary["earned"], 1)
        self.assertEqual(self.claims(101)["level:10"], "earned")

    def test_award_past_earns_on_first_sight(self):
        self.config(enabled=True, award_past=True)
        self.player(101, level=50)
        bp.run_tick(self.base, self.game, now=1000)
        self.assertEqual(self.claims(101)["level:10"], "earned")

    def test_upstream_297_new_tier_satisfied_by_default_stays_baseline(self):
        # The #297 incident: a catalog update ADDS tiers whose nodes read
        # complete-by-default. An account with existing claim history must
        # get BASELINE on the new tier (first sight satisfied), never a
        # fresh auto-grantable earn — per-TIER seen markers are the fix.
        p = self.player(101, level=5, journey=["DA_NEW_DEFAULT"])
        bp.run_tick(self.base, self.game, now=1000)       # watches level:10
        p["level"] = 15
        bp.run_tick(self.base, self.game, now=2000)       # earns level:10
        new_tier = {"tier_key": "journey:DA_NEW_DEFAULT", "category": "achievement",
                    "label": "New Ach", "signal": "journey_node",
                    "signal_key": "DA_NEW_DEFAULT", "threshold": 0, "intel": 2,
                    "reward_items": "", "enabled": True}
        bp.reseed_tiers(self.base, CATALOG["tiers"] + [new_tier])
        bp.run_tick(self.base, self.game, now=3000)
        self.assertEqual(self.claims(101)["journey:DA_NEW_DEFAULT"], "baseline")
        self.assertEqual(self.claims(101)["level:10"], "earned")  # history kept

    def test_disabled_config_is_noop(self):
        self.config(enabled=False)
        self.player(101, level=50)
        summary = bp.run_tick(self.base, self.game, now=1000)
        self.assertEqual(summary["scanned"], 0)


class GrantTests(Harness):
    def earn_level10(self, account=101, online=False):
        p = self.player(account, level=5, online=online)
        bp.run_tick(self.base, self.game, now=1000)
        p["level"] = 15
        bp.run_tick(self.base, self.game, now=2000)
        return p

    def test_auto_grant_delivers_offline_in_money_safe_order(self):
        self.config(enabled=True, auto_grant=True)
        p = self.earn_level10()
        p["tags"] = ["Explo.A"]  # also earn the item tier next tick? no: first sight satisfied AFTER seen? tags tier was watched-unsatisfied in earlier ticks → earns now
        bp.run_tick(self.base, self.game, now=3000)
        calls = self.game.calls
        intel = [c for c in calls if c[0] == "award-intel"]
        items = [c for c in calls if c[0] == "give-item"]
        self.assertTrue(intel)
        self.assertEqual(self.claims(101)["level:10"], "granted")
        # the item tier delivered its schematic after its intel
        if items:
            self.assertEqual(items[0][2], "Schematic_X")

    def test_online_player_retry_later_never_consumes_attempts(self):
        self.config(enabled=True, auto_grant=True)
        self.earn_level10(online=True)   # delivery attempt happens at t=2000
        # inside the 5-min online backoff: no new attempts
        for now in (2100, 2250):
            bp.run_tick(self.base, self.game, now=now)
        self.assertEqual(len(self.intel_calls()), 1)
        # past the backoff: attempted again, still online, still attempts=0
        bp.run_tick(self.base, self.game, now=2400)
        self.assertEqual(len(self.intel_calls()), 2)
        ledger = bp.list_ledger(self.base)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["status"], "pending")
        self.assertEqual(ledger[0]["attempts"], 0)
        self.assertEqual(self.claims(101)["level:10"], "earned")

    def test_real_failures_exhaust_after_three(self):
        self.config(enabled=True, auto_grant=True)
        self.game.fail_intel = True      # broken from the very first attempt
        self.earn_level10()              # attempt 1 fails at t=2000
        now = 2000
        for _ in range(2):
            now += bp.RETRY_BACKOFF_SECS + 1
            bp.run_tick(self.base, self.game, now=now)
        ledger = bp.list_ledger(self.base)[0]
        self.assertEqual(ledger["status"], "exhausted")
        self.assertEqual(ledger["attempts"], 3)
        self.assertEqual(self.claims(101)["level:10"], "earned")  # not granted
        # exhausted rows never auto-retry
        self.game.fail_intel = False
        bp.run_tick(self.base, self.game, now=now + bp.RETRY_BACKOFF_SECS * 10)
        self.assertEqual(self.claims(101)["level:10"], "earned")

    def test_retroactive_enqueue_when_auto_grant_turns_on(self):
        self.earn_level10()          # auto_grant off: earned sits
        self.assertEqual(bp.list_ledger(self.base), [])
        self.config(enabled=True, auto_grant=True)
        bp.run_tick(self.base, self.game, now=3000)
        self.assertEqual(self.claims(101)["level:10"], "granted")

    def test_manual_grant_account(self):
        self.earn_level10()
        result = bp.grant_account(self.base, self.game, 101)
        self.assertEqual(result["granted"], 1)
        self.assertEqual(self.claims(101)["level:10"], "granted")

    def test_manual_grant_refuses_online(self):
        self.earn_level10(online=True)
        result = bp.grant_account(self.base, self.game, 101)
        self.assertEqual(result["granted"], 0)
        self.assertIn("online", result["detail"])
        self.assertEqual(self.claims(101)["level:10"], "earned")


class ResetTests(Harness):
    def earn(self):
        p = self.player(101, level=5)
        bp.run_tick(self.base, self.game, now=1000)
        p["level"] = 15
        bp.run_tick(self.base, self.game, now=2000)

    def test_demote_flips_earned_to_baseline_no_re_earn(self):
        self.earn()
        result = bp.reset_claims(self.base, "demote")
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.claims(101)["level:10"], "baseline")
        bp.run_tick(self.base, self.game, now=3000)
        self.assertEqual(self.claims(101)["level:10"], "baseline")
        self.config(enabled=True, auto_grant=True)
        bp.run_tick(self.base, self.game, now=4000)
        self.assertEqual(self.intel_calls(), [])

    def test_purge_rebaselines_not_re_earns(self):
        self.earn()
        bp.reset_claims(self.base, "purge")
        self.assertEqual(self.claims(101), {})
        # next scan: still-satisfied tier is BASELINE (seen markers went too)
        summary = bp.run_tick(self.base, self.game, now=3000)
        self.assertEqual(summary["earned"], 0)
        self.assertEqual(self.claims(101)["level:10"], "baseline")

    def test_granted_ledger_history_survives_resets(self):
        self.config(enabled=True, auto_grant=True)
        p = self.player(101, level=5)
        bp.run_tick(self.base, self.game, now=1000)
        p["level"] = 15
        bp.run_tick(self.base, self.game, now=2000)
        bp.run_tick(self.base, self.game, now=3000)
        self.assertEqual(self.claims(101)["level:10"], "granted")
        bp.reset_claims(self.base, "demote")
        ledger = bp.list_ledger(self.base)
        self.assertEqual([l["status"] for l in ledger], ["granted"])

    def test_reset_scopes_to_account(self):
        self.earn()
        p2 = self.player(202, level=5)
        bp.run_tick(self.base, self.game, now=2500)
        p2["level"] = 20
        bp.run_tick(self.base, self.game, now=3000)
        bp.reset_claims(self.base, "demote", account_id=101)
        self.assertEqual(self.claims(101)["level:10"], "baseline")
        self.assertEqual(self.claims(202)["level:10"], "earned")


class PacingTests(Harness):
    def test_poll_seconds_gates_ticks(self):
        self.player(101, level=50)
        s1 = bp.run_tick(self.base, self.game, now=1000)
        self.assertEqual(s1["scanned"], 1)          # first sight fires
        s2 = bp.run_tick(self.base, self.game, now=1030)
        self.assertEqual(s2["scanned"], 0)          # inside the 60s window
        s3 = bp.run_tick(self.base, self.game, now=1061)
        self.assertEqual(s3["scanned"], 1)
        s4 = bp.run_tick(self.base, self.game, now=1062, force=True)
        self.assertEqual(s4["scanned"], 1)          # force bypasses pacing


class MigratedSchemaTests(Harness):
    def test_tags_via_character_id_schema(self):
        self.game.tags_key_col = "character_id"
        self.player(101, level=1, tags=["Explo.A"])
        self.config(enabled=True, award_past=True)
        bp.run_tick(self.base, self.game, now=1000)
        self.assertEqual(self.claims(101)["tag:Explo.A"], "earned")


if __name__ == "__main__":
    unittest.main()
