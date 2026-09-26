#!/usr/bin/env python3
"""Tests for admin_ban: the player denylist the FLS stub enforces (#118)."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import admin_ban as ab

NOW = datetime(2026, 9, 26, 8, 0, 0, tzinfo=timezone.utc)
FLS = "DE0BCCAA2501BF22"

# The real body of Battlegroups_IsPlayerAuthorized, captured on our test
# server on 2026-09-26 (the stub's capture mode).
REAL_BODY = json.dumps({
    "BattlegroupId": "sh-de0bccaa2501bf22-jrqtjf", "FlsClientVersion": 1,
    "FlsFunctionVersion": 1, "PlatformName": "NULL",
    "PlayerId": FLS, "RegionId": "Europe",
}).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.state = os.path.join(self.base, "server", "state")

    def tearDown(self):
        self.tmp.cleanup()


class TestStore(Base):
    def test_ban_then_is_banned(self):
        ab.ban(self.base, FLS, name="Griefer#1234", reason="griefing", duration_secs=None, by="admin", now=NOW)
        b = ab.active_ban(self.state, FLS, NOW)
        self.assertIsNotNone(b)
        self.assertEqual(b["reason"], "griefing")
        self.assertIsNone(b["expires_at"])

    def test_ids_are_normalised(self):
        ab.ban(self.base, "de0bccaa2501bf22", name="", reason="", duration_secs=None, by="admin", now=NOW)
        self.assertIsNotNone(ab.active_ban(self.state, FLS, NOW))
        self.assertIsNotNone(ab.active_ban(self.state, " de0bccaa2501bf22 ", NOW))

    def test_invalid_id_is_refused(self):
        for bad in ("", "xyz", "DE0BCCAA2501BF2", "DE0BCCAA2501BF22Z", "G" * 16):
            with self.assertRaises(ValueError, msg=bad):
                ab.ban(self.base, bad, name="", reason="", duration_secs=None, by="admin", now=NOW)

    def test_temporary_ban_expires(self):
        ab.ban(self.base, FLS, name="", reason="", duration_secs=3600, by="admin", now=NOW)
        self.assertIsNotNone(ab.active_ban(self.state, FLS, NOW + timedelta(minutes=59)))
        self.assertIsNone(ab.active_ban(self.state, FLS, NOW + timedelta(minutes=61)))

    def test_reban_replaces_rather_than_duplicates(self):
        ab.ban(self.base, FLS, name="a", reason="first", duration_secs=60, by="admin", now=NOW)
        ab.ban(self.base, FLS, name="a", reason="second", duration_secs=None, by="admin", now=NOW)
        bans = ab.list_bans(self.state, NOW)
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0]["reason"], "second")

    def test_unban(self):
        ab.ban(self.base, FLS, name="", reason="", duration_secs=None, by="admin", now=NOW)
        self.assertTrue(ab.unban(self.base, FLS))
        self.assertIsNone(ab.active_ban(self.state, FLS, NOW))
        self.assertFalse(ab.unban(self.base, FLS))

    def test_list_hides_expired(self):
        ab.ban(self.base, FLS, name="", reason="", duration_secs=60, by="admin", now=NOW)
        ab.ban(self.base, "1A3628F67968807F", name="", reason="", duration_secs=None, by="admin", now=NOW)
        ids = [b["fls_id"] for b in ab.list_bans(self.state, NOW + timedelta(hours=1))]
        self.assertEqual(ids, ["1A3628F67968807F"])

    def test_non_positive_duration_is_refused(self):
        for d in (0, -5):
            with self.assertRaises(ValueError):
                ab.ban(self.base, FLS, name="", reason="", duration_secs=d, by="admin", now=NOW)


# A broken ban file must never lock the whole server out.
class TestFailOpen(Base):
    def write_raw(self, text):
        p = ab.bans_path(self.state)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def test_missing_file_bans_nobody(self):
        self.assertIsNone(ab.active_ban(self.state, FLS, NOW))

    def test_corrupt_file_bans_nobody(self):
        for raw in ("{not json", "[]", '{"bans": "x"}', '{"bans": [{"fls_id": 5}]}'):
            self.write_raw(raw)
            self.assertIsNone(ab.active_ban(self.state, FLS, NOW), raw)

    def test_bad_entry_does_not_hide_good_ones(self):
        self.write_raw(json.dumps({"bans": [
            {"fls_id": "nope"},
            {"fls_id": FLS, "expires_at": None, "reason": "ok"},
        ]}))
        self.assertIsNotNone(ab.active_ban(self.state, FLS, NOW))


class TestAuthorizationVerdict(Base):
    def test_real_body_of_unbanned_player_is_authorized(self):
        ok, ban = ab.authorization_verdict(self.state, REAL_BODY, NOW)
        self.assertTrue(ok)
        self.assertIsNone(ban)

    def test_real_body_of_banned_player_is_refused(self):
        ab.ban(self.base, FLS, name="", reason="griefing", duration_secs=None, by="admin", now=NOW)
        ok, ban = ab.authorization_verdict(self.state, REAL_BODY, NOW)
        self.assertFalse(ok)
        self.assertEqual(ban["reason"], "griefing")

    # The stub must keep authorizing when it cannot tell who is asking: a
    # changed request shape must not lock everybody out.
    def test_unreadable_body_is_authorized(self):
        ab.ban(self.base, FLS, name="", reason="", duration_secs=None, by="admin", now=NOW)
        for body in (b"", b"not json", b"[]", json.dumps({"PlayerId": 5}).encode()):
            ok, _ = ab.authorization_verdict(self.state, body, NOW)
            self.assertTrue(ok, body)


class TestHttpHandlers(Base):
    """The request logic behind POST /api/bans and /api/bans/<id>/lift."""

    def test_ban_request_bans_and_asks_for_kick(self):
        status, payload, kick = ab.handle_ban_request(self.base, {
            "fls_id": FLS.lower(), "name": "Griefer", "reason": "griefing", "duration_secs": 3600}, now=NOW)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["ban"]["fls_id"], FLS)
        self.assertEqual(payload["ban"]["expires_at"], "2026-09-26T09:00:00Z")
        self.assertEqual(kick, FLS)
        self.assertIsNotNone(ab.active_ban(self.state, FLS, NOW))

    def test_permanent_when_duration_missing_null_or_zero(self):
        for d in ({}, {"duration_secs": None}, {"duration_secs": 0}):
            status, payload, _ = ab.handle_ban_request(self.base, {"fls_id": FLS, **d}, now=NOW)
            self.assertEqual(status, 200, d)
            self.assertIsNone(payload["ban"]["expires_at"], d)

    def test_kick_can_be_skipped(self):
        _, _, kick = ab.handle_ban_request(self.base, {"fls_id": FLS, "kick": False}, now=NOW)
        self.assertIsNone(kick)

    def test_bad_requests_are_400_and_ban_nothing(self):
        for body in (None, [], {}, {"fls_id": "nope"}, {"fls_id": FLS, "duration_secs": -1},
                     {"fls_id": FLS, "duration_secs": "soon"}, {"fls_id": FLS, "duration_secs": 10**12}):
            status, payload, kick = ab.handle_ban_request(self.base, body, now=NOW)
            self.assertEqual(status, 400, body)
            self.assertFalse(payload["ok"], body)
            self.assertIsNone(kick, body)
        self.assertEqual(ab.list_bans(self.state, NOW), [])

    def test_lift(self):
        ab.ban(self.base, FLS, name="", reason="", duration_secs=None, by="panel", now=NOW)
        status, payload = ab.handle_lift_request(self.base, FLS.lower())
        self.assertEqual((status, payload["lifted"]), (200, True))
        status, payload = ab.handle_lift_request(self.base, FLS)
        self.assertEqual((status, payload["lifted"]), (200, False))
        status, _ = ab.handle_lift_request(self.base, "nope")
        self.assertEqual(status, 400)

    def test_audit_entry_shape(self):
        e = ab.audit_entry(["ban", FLS, "3600"], True, "banned", now=NOW)
        self.assertEqual(e["argv"], ["ban", FLS, "3600"])
        self.assertTrue(e["ok"])
        self.assertEqual(e["ts"], int(NOW.timestamp()))


if __name__ == "__main__":
    unittest.main()
