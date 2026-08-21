"""Tests for admin_events.py — pins the upstream behavioral truths:
one-outcome-per-tick zone races, claim-ledger idempotency, structured-
cursor resume that never re-pays, 3-attempt exhaustion, silent backfill,
and announce-after-grant ordering. The game is a scripted fake publish.
"""
import os
import shutil
import tempfile
import unittest

import admin_events as ev


class FakeGame:
    """publish(argv) double: serves the engine's db-sql reads from in-memory
    player state and records every grant/broadcast."""

    def __init__(self):
        self.players = []      # dicts: account_id,fls,name,online,x,y,z,map,level,tags
        self.calls = []        # every non-db-sql argv
        self.fail_on = set()   # ("give-currency",) or ("give","TemplateX") …

    def _csv(self, headers, rows):
        out = [",".join(headers)]
        for r in rows:
            out.append(",".join(str(r[h]) for h in headers))
        return "\n".join(out) + "\n"

    def __call__(self, argv):
        if argv[0] != "db-sql":
            self.calls.append(tuple(argv))
            key1, key2 = (argv[0],), (argv[0], argv[2] if len(argv) > 2 else "")
            if key1 in self.fail_on or key2 in self.fail_on:
                return 1, "scripted failure"
            return 0, "ok"
        sql = argv[1]
        online = [p for p in self.players if p.get("online", True)]
        if "((a.transform).location)" in sql:
            rows = [{"account_id": p["account_id"], "map": p.get("map", ""),
                     "x": p.get("x", 0), "y": p.get("y", 0), "z": p.get("z", 0)}
                    for p in online if str(p["account_id"]) in sql]
            return 0, self._csv(["account_id", "map", "x", "y", "z"], rows)
        if "FLevelComponent" in sql:
            rows = [{"account_id": p["account_id"],
                     "xp": ev.CUMULATIVE_XP[min(p.get("level", 0), 200)]}
                    for p in self.players if str(p["account_id"]) in sql]
            return 0, self._csv(["account_id", "xp"], rows)
        if "player_tags" in sql and "information_schema" not in sql:
            tag = sql.split("pt.tag = '")[1].split("'")[0]
            rows = [{"account_id": p["account_id"]}
                    for p in self.players
                    if str(p["account_id"]) in sql and tag in p.get("tags", [])]
            return 0, self._csv(["account_id"], rows)
        if "information_schema" in sql:
            return 0, self._csv(["column_name"], [{"column_name": "account_id"}])
        if "eps.account_id =" in sql:
            account = int(sql.split("eps.account_id =")[1].strip().rstrip())
            rows = [{"fls": p["fls"],
                     "online": "t" if p.get("online", True) else "f",
                     "name": p.get("name", "")}
                    for p in self.players if p["account_id"] == account]
            return 0, self._csv(["fls", "online", "name"], rows)
        # default: the online-players query
        rows = [{"account_id": p["account_id"], "controller_id": 100,
                 "pawn_id": 200, "fls": p["fls"], "name": p.get("name", "")}
                for p in online]
        return 0, self._csv(["account_id", "controller_id", "pawn_id",
                             "fls", "name"], rows)


class EngineHarness(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="events-test-")
        os.makedirs(os.path.join(self.base, "data", "admin"), exist_ok=True)
        with open(os.path.join(self.base, ev.CONFIG_PATH), "w") as f:
            f.write('{"enabled": true}')
        self.game = FakeGame()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def player(self, account, **kw):
        p = {"account_id": account, "fls": f"FLS{account:04X}",
             "name": f"P{account}", "online": True}
        p.update(kw)
        self.game.players.append(p)
        return p

    def tick(self, now=1000, boot=False):
        return ev.run_tick(self.base, self.game, now=now, boot=boot)

    def grants(self):
        return [c for c in self.game.calls if c[0] != "broadcast"]

    def broadcasts(self):
        return [c for c in self.game.calls if c[0] == "broadcast"]


class ConfigGateTests(EngineHarness):
    def test_disabled_config_is_a_no_op(self):
        with open(os.path.join(self.base, ev.CONFIG_PATH), "w") as f:
            f.write('{"enabled": false}')
        self.player(101, level=50)
        ev.create_event(self.base, "M", "milestone",
                        '{"signal": "level", "threshold": 5}')
        summary = self.tick()
        self.assertEqual(summary["due"], 0)
        self.assertEqual(self.game.calls, [])


class ValidationTests(EngineHarness):
    def test_bad_definitions_refused(self):
        cases = [
            ("", "milestone", "{}", ""),
            ("X", "nope", "{}", ""),
            ("X", "zone_race", '{"radius": 0, "participants": [1]}', ""),
            ("X", "zone_race", '{"radius": 5, "participants": []}', ""),
            ("X", "milestone", '{"signal": "level", "threshold": 0}', ""),
            ("X", "milestone", '{"signal": "achievement_tag"}', ""),
            ("X", "milestone", '{"signal": "level", "threshold": 5}',
             '{"faction_scrip": 5}'),
        ]
        for name, typ, cfg, reward in cases:
            with self.assertRaises(ValueError, msg=(name, typ, cfg, reward)):
                ev.validate_definition(name, typ, cfg, reward)


class MilestoneTests(EngineHarness):
    def make_event(self, reward='{"currency": 100}', threshold=10, poll=7):
        eid = ev.create_event(
            self.base, "Level Up", "milestone",
            f'{{"signal": "level", "threshold": {threshold}}}', reward,
            announce_template="{player} reached {event}!",
            poll_seconds=poll)
        ev.set_enabled(self.base, eid, True)
        return eid

    def test_qualifiers_granted_once_and_announced(self):
        self.player(101, level=50)
        self.player(102, level=5)   # below threshold
        self.make_event()
        summary = self.tick(now=1000)
        self.assertEqual(summary["granted"], 1)
        self.assertEqual(self.grants(),
                         [("give-currency", "FLS0065", "100", "allow-online")])
        self.assertEqual(len(self.broadcasts()), 1)
        self.assertIn("P101 reached Level Up!", self.broadcasts()[0][2])
        # second due tick: idempotent (claim ledger)
        summary = self.tick(now=2000)
        self.assertEqual(summary["granted"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(len(self.grants()), 1)

    def test_poll_gating_fires_immediately_then_waits(self):
        self.player(101, level=50)
        self.make_event(poll=30)
        self.assertEqual(self.tick(now=1000)["due"], 1)
        self.assertEqual(self.tick(now=1010)["due"], 0)   # inside poll window
        self.assertEqual(self.tick(now=1040)["due"], 1)   # past poll+jitter max

    def test_announce_only_event_seals_without_grants(self):
        self.player(101, level=50)
        self.make_event(reward="")
        summary = self.tick()
        self.assertEqual(summary["granted"], 1)
        self.assertEqual(self.grants(), [])
        self.assertEqual(len(self.broadcasts()), 1)

    def test_reset_claims_repays(self):
        self.player(101, level=50)
        eid = self.make_event()
        self.tick(now=1000)
        self.assertEqual(ev.reset_claims(self.base, eid), 1)
        self.tick(now=2000)
        self.assertEqual(len(self.grants()), 2)

    def test_tag_milestone(self):
        self.player(101, tags=["Exploration.POI.Arrakeen"])
        self.player(102, tags=[])
        eid = ev.create_event(
            self.base, "Explorer", "milestone",
            '{"signal": "achievement_tag", "tag_name": "Exploration.POI.Arrakeen"}',
            '{"currency": 5}')
        ev.set_enabled(self.base, eid, True)
        self.assertEqual(self.tick()["granted"], 1)


class ZoneRaceTests(EngineHarness):
    def make_race(self, participants, radius=100, map_name="HaggaBasin"):
        eid = ev.create_event(
            self.base, "Dash", "zone_race",
            f'{{"map": "{map_name}", "x": 0, "y": 0, "z": 0,'
            f' "radius": {radius}, "participants": {participants}}}',
            '{"currency": 10}')
        ev.set_enabled(self.base, eid, True)
        return eid

    def test_first_in_list_order_wins_one_per_tick(self):
        self.player(101, map="HaggaBasin", x=10, y=0, z=0)
        self.player(102, map="HaggaBasin", x=5, y=0, z=0)  # closer but later in list
        self.make_race([101, 102])
        self.assertEqual(self.tick(now=1000)["granted"], 1)
        self.assertEqual(self.grants(), [("give-currency", "FLS0065", "10", "allow-online")])
        # next due tick: 101 claimed → 102 wins
        self.assertEqual(self.tick(now=2000)["granted"], 1)
        self.assertEqual(self.grants()[-1], ("give-currency", "FLS0066", "10", "allow-online"))

    def test_map_and_radius_filters(self):
        self.player(101, map="DeepDesert", x=0, y=0, z=0)      # wrong map
        self.player(102, map="HaggaBasin", x=500, y=0, z=0)    # outside radius
        self.make_race([101, 102])
        self.assertEqual(self.tick()["granted"], 0)
        self.assertEqual(self.grants(), [])

    def test_offline_participant_is_skipped(self):
        self.player(101, online=False, map="HaggaBasin", x=0, y=0, z=0)
        self.player(102, map="HaggaBasin", x=0, y=0, z=0)
        self.make_race([101, 102])
        self.tick()
        self.assertEqual(self.grants(), [("give-currency", "FLS0066", "10", "allow-online")])


class RetryTests(EngineHarness):
    REWARD = ('{"currency": 100,'
              ' "items": [{"template": "GoodItem", "qty": 1, "quality": 0},'
              '           {"template": "BadItem", "qty": 1, "quality": 0}],'
              ' "xp": [{"track": "Combat", "amount": 10}]}')

    def make_event(self):
        eid = ev.create_event(self.base, "M", "milestone",
                              '{"signal": "level", "threshold": 10}',
                              self.REWARD,
                              announce_template="{player} did it!")
        ev.set_enabled(self.base, eid, True)
        return eid

    def test_pending_is_retried_by_normal_evaluation_without_repaying(self):
        # Upstream claim matrix: pending is NOT blocked — the next normal
        # tick resumes delivery at the cursor and must never re-pay.
        self.player(101, level=50)
        eid = self.make_event()
        self.game.fail_on.add(("give", "BadItem"))
        self.assertEqual(self.tick(now=1000)["failed"], 1)
        self.assertEqual([c[0] for c in self.grants()],
                         ["give-currency", "give", "give"])
        claims = ev.list_claims(self.base, eid)
        self.assertEqual(claims[0]["status"], "pending")
        self.assertIn('"items"', claims[0]["cursor"])
        self.game.fail_on.clear()
        summary = self.tick(now=2000)
        self.assertEqual(summary["granted"], 1)
        tail = self.grants()[3:]
        self.assertEqual([(c[0], c[2]) for c in tail],
                         [("give", "BadItem"), ("award-track-xp", "Combat")])
        self.assertEqual(ev.list_claims(self.base, eid)[0]["status"], "granted")
        # one announce total: the successful completion, not the failed try
        self.assertEqual(len(self.broadcasts()), 1)

    def test_offline_pending_waits_for_backoff_then_retries_offline(self):
        # A pending claim whose player leaves the evaluation set (offline)
        # is the retry loop's job: not before next_attempt_at, offline-
        # capable delivery (give-item), and never re-announced.
        self.player(101, level=50)
        eid = self.make_event()
        self.game.fail_on.add(("give", "BadItem"))
        self.tick(now=1000)
        self.game.fail_on.clear()
        self.game.players[0]["online"] = False
        self.assertEqual(self.tick(now=2000)["retried"], 0)  # before backoff
        summary = self.tick(now=1000 + ev.RETRY_BACKOFF_SECS + 1)
        self.assertEqual(summary["retried"], 1)
        tail = self.grants()[3:]
        self.assertEqual([(c[0], c[2]) for c in tail],
                         [("give-item", "BadItem"), ("award-track-xp", "Combat")])
        self.assertEqual(ev.list_claims(self.base, eid)[0]["status"], "granted")
        self.assertEqual(len(self.broadcasts()), 0)  # retries never announce

    def test_three_failures_exhaust(self):
        self.player(101, level=50)
        eid = self.make_event()
        self.game.fail_on.add(("give", "BadItem"))
        self.game.fail_on.add(("give-item", "BadItem"))
        self.tick(now=1000)
        self.game.players[0]["online"] = False  # hand the claim to the retry loop
        now = 1000
        for _ in range(2):
            now += ev.RETRY_BACKOFF_SECS + 1
            self.tick(now=now)
        claims = ev.list_claims(self.base, eid)
        self.assertEqual(claims[0]["status"], "exhausted")
        self.assertEqual(claims[0]["attempts"], 3)
        # exhausted claims never retry again
        now += ev.RETRY_BACKOFF_SECS * 100
        self.assertEqual(self.tick(now=now)["retried"], 0)


class BackfillTests(EngineHarness):
    def make_milestone(self, award_past, reward='{"currency": 100}'):
        eid = ev.create_event(
            self.base, "M", "milestone",
            f'{{"signal": "level", "threshold": 10,'
            f' "award_past": {"true" if award_past else "false"}}}', reward,
            announce_template="{player}!")
        ev.set_enabled(self.base, eid, True)
        return eid

    def test_boot_backfill_seals_silently_without_award_past(self):
        self.player(101, level=50)
        eid = self.make_milestone(award_past=False)
        summary = self.tick(now=1000, boot=True)
        self.assertEqual(summary["backfilled"], 1)
        self.assertEqual(self.grants(), [])          # no reward
        self.assertEqual(self.broadcasts(), [])      # no announce
        self.assertEqual(ev.list_claims(self.base, eid)[0]["status"], "granted")
        # the regular evaluation in the same tick skips the sealed claim
        self.assertEqual(summary["granted"], 0)

    def test_boot_backfill_grants_with_award_past(self):
        self.player(101, level=50)
        self.make_milestone(award_past=True)
        summary = self.tick(now=1000, boot=True)
        self.assertEqual(summary["backfilled"], 1)
        self.assertEqual(self.grants(), [("give-currency", "FLS0065", "100", "allow-online")])
        self.assertEqual(self.broadcasts(), [])      # backfill never announces

    def test_zone_races_never_backfilled(self):
        self.player(101, map="HaggaBasin", x=0, y=0, z=0)
        eid = ev.create_event(self.base, "Dash", "zone_race",
                              '{"map": "", "x": 0, "y": 0, "z": 0,'
                              ' "radius": 100, "participants": [101]}',
                              '{"currency": 10}')
        ev.set_enabled(self.base, eid, True)
        summary = self.tick(now=1000, boot=True)
        self.assertEqual(summary["backfilled"], 0)
        # the race still evaluates normally in the same tick
        self.assertEqual(summary["granted"], 1)


if __name__ == "__main__":
    unittest.main()


class RewardEditedWhilePendingTests(EngineHarness):
    def test_edited_reward_parks_claim_exhausted_never_underdelivers(self):
        # Review MEDIUM: resuming an old cursor against an EDITED reward
        # silently skips the new spec's early steps. The port refuses:
        # the claim parks as exhausted with an operator-facing reason.
        self.player(101, level=50)
        eid = ev.create_event(
            self.base, "M", "milestone",
            '{"signal": "level", "threshold": 10}',
            '{"currency": 100,'
            ' "items": [{"template": "GoodItem", "qty": 1, "quality": 0},'
            '           {"template": "BadItem", "qty": 1, "quality": 0}]}')
        ev.set_enabled(self.base, eid, True)
        self.game.fail_on.add(("give", "BadItem"))
        self.tick(now=1000)   # fails at BadItem, cursor mid-items
        self.game.fail_on.clear()
        ev.update_event(self.base, eid, reward_json=(
            '{"currency": 100,'
            ' "items": [{"template": "NewA", "qty": 1, "quality": 0},'
            '           {"template": "NewB", "qty": 1, "quality": 0}],'
            ' "xp": [{"track": "Combat", "amount": 999}]}'))
        summary = self.tick(now=2000)
        self.assertEqual(summary["failed"], 1)
        claims = ev.list_claims(self.base, eid)
        self.assertEqual(claims[0]["status"], "exhausted")
        self.assertIn("reward changed", claims[0]["last_error"])
        # nothing from the NEW spec was delivered (no NewA/NewB/xp calls)
        delivered = [c for c in self.grants() if c[0] != "give-currency"]
        self.assertEqual([c[2] for c in delivered], ["GoodItem", "BadItem"])


class ConcurrencyTests(unittest.TestCase):
    def test_forked_ticks_pay_exactly_once(self):
        # Review HIGH-1 repro, inverted: N processes ticking the same DB
        # must land exactly ONE grant thanks to the BEGIN IMMEDIATE lock
        # held across delivery.
        import multiprocessing
        import tempfile, shutil, os
        base = tempfile.mkdtemp(prefix="events-conc-")
        try:
            os.makedirs(os.path.join(base, "data", "admin"), exist_ok=True)
            with open(os.path.join(base, ev.CONFIG_PATH), "w") as f:
                f.write('{"enabled": true}')
            eid = ev.create_event(base, "M",
                                  "milestone",
                                  '{"signal": "level", "threshold": 10}',
                                  '{"currency": 100}')
            ev.set_enabled(base, eid, True)
            log_path = os.path.join(base, "grants.log")
            procs = [multiprocessing.Process(
                target=_concurrent_tick, args=(base, log_path))
                for _ in range(6)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
            with open(log_path) as f:
                grants = [line for line in f if line.strip()]
            self.assertEqual(len(grants), 1, grants)
        finally:
            shutil.rmtree(base, ignore_errors=True)


def _concurrent_tick(base, log_path):
    import time as _time

    def publish(argv):
        if argv[0] == "db-sql":
            sql = argv[1]
            if "FLevelComponent" in sql:
                return 0, "account_id,xp\n101,28190\n"
            if "eps.account_id =" in sql:
                return 0, "fls,online,name\nFLS0065,t,P101\n"
            return 0, ("account_id,controller_id,pawn_id,fls,name\n"
                       "101,100,200,FLS0065,P101\n")
        if argv[0] == "give-currency":
            _time.sleep(0.3)  # widen the race window
            with open(log_path, "a") as f:
                f.write(" ".join(argv) + "\n")
        return 0, "ok"

    try:
        ev.run_tick(base, publish, now=1000)
    except Exception:
        pass  # a locked-out tick failing cleanly is the expected loser path
