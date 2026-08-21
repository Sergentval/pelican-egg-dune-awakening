"""Tests for admin_rewards.py — the shared reward spec + resume cursor.

Upstream (dune-admin) resumed partial grants by PARSING ERROR STRINGS
("grant item \"X\": …") — a load-bearing data format hidden in log text.
The port replaces it with a structured cursor {stage, index}; these tests
pin the delivery order (currency → items → xp), the resume semantics, and
the fail-safe (unknown cursor ⇒ replay everything).
"""
import unittest

import admin_rewards as rw


class SpecTests(unittest.TestCase):
    def test_parse_full_spec(self):
        spec = rw.parse_spec(
            '{"currency": 5000,'
            ' "items": [{"template": "Ammo", "qty": 5, "quality": 0}],'
            ' "xp": [{"track": "Combat", "amount": 10000}]}')
        self.assertEqual(spec["currency"], 5000)
        self.assertEqual(spec["items"][0]["template"], "Ammo")
        self.assertEqual(spec["xp"][0]["track"], "Combat")

    def test_empty_string_is_announce_only(self):
        self.assertEqual(rw.parse_spec(""), {"currency": 0, "items": [], "xp": []})

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            rw.parse_spec("{nope")

    def test_bad_shapes_raise(self):
        with self.assertRaises(ValueError):
            rw.parse_spec('{"currency": "lots"}')
        with self.assertRaises(ValueError):
            rw.parse_spec('{"items": [{"qty": 1}]}')  # missing template
        with self.assertRaises(ValueError):
            rw.parse_spec('{"items": [{"template": "A", "qty": 0}]}')  # qty < 1
        with self.assertRaises(ValueError):
            rw.parse_spec('{"xp": [{"track": "", "amount": 5}]}')
        with self.assertRaises(ValueError):
            rw.parse_spec('{"xp": [{"track": "Combat", "amount": -1}]}')

    def test_upstream_faction_scrip_is_rejected_not_silently_dropped(self):
        # Upstream's UI wrote reward.faction_scrip and NOTHING ever read it —
        # a silent no-op. We refuse unknown keys so a config error is loud.
        with self.assertRaises(ValueError):
            rw.parse_spec('{"faction_scrip": 100}')

    def test_is_empty(self):
        self.assertTrue(rw.is_empty(rw.parse_spec("")))
        self.assertFalse(rw.is_empty(rw.parse_spec('{"currency": 1}')))


class StepTests(unittest.TestCase):
    SPEC = rw.parse_spec(
        '{"currency": 100,'
        ' "items": [{"template": "A", "qty": 1, "quality": 0},'
        '           {"template": "B", "qty": 2, "quality": 0}],'
        ' "xp": [{"track": "Combat", "amount": 10},'
        '        {"track": "Crafting", "amount": 20}]}')

    def test_steps_in_delivery_order(self):
        steps = rw.steps(self.SPEC)
        self.assertEqual([(s["stage"], s["index"]) for s in steps],
                         [("currency", 0), ("items", 0), ("items", 1),
                          ("xp", 0), ("xp", 1)])

    def test_resume_from_none_replays_everything(self):
        self.assertEqual(len(rw.remaining(self.SPEC, None)), 5)
        self.assertEqual(len(rw.remaining(self.SPEC, "")), 5)

    def test_resume_from_items_index(self):
        rest = rw.remaining(self.SPEC, '{"stage": "items", "index": 1}')
        self.assertEqual([(s["stage"], s["index"]) for s in rest],
                         [("items", 1), ("xp", 0), ("xp", 1)])

    def test_resume_from_xp_skips_currency_and_items(self):
        rest = rw.remaining(self.SPEC, '{"stage": "xp", "index": 1}')
        self.assertEqual([(s["stage"], s["index"]) for s in rest],
                         [("xp", 1)])

    def test_resume_from_currency_replays_everything(self):
        rest = rw.remaining(self.SPEC, '{"stage": "currency", "index": 0}')
        self.assertEqual(len(rest), 5)

    def test_unrecognised_cursor_fails_safe_to_full_replay(self):
        self.assertEqual(len(rw.remaining(self.SPEC, "grant item \"B\": boom")), 5)
        self.assertEqual(len(rw.remaining(self.SPEC, '{"stage": "frobnicate"}')), 5)

    def test_cursor_round_trip(self):
        step = rw.steps(self.SPEC)[2]
        cur = rw.cursor_of(step)
        rest = rw.remaining(self.SPEC, cur)
        self.assertEqual(rest[0], step)


class DeliverTests(unittest.TestCase):
    """deliver() drives an injected runner; on failure it returns the cursor
    of the FAILED step so the retry resumes there (upstream's end-to-end
    truth: currency and already-granted items are never re-paid)."""

    SPEC = StepTests.SPEC

    def run_deliver(self, fail_on=None, cursor=None):
        calls = []

        def runner(step):
            calls.append((step["stage"], step["index"]))
            if fail_on is not None and (step["stage"], step["index"]) == fail_on:
                return "boom"
            return ""

        result = rw.deliver(self.SPEC, runner, cursor)
        return calls, result

    def test_happy_path_delivers_all_and_clears_cursor(self):
        calls, result = self.run_deliver()
        self.assertEqual(len(calls), 5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cursor"], "")

    def test_failure_stops_and_returns_cursor_of_failed_step(self):
        calls, result = self.run_deliver(fail_on=("items", 1))
        self.assertEqual(calls, [("currency", 0), ("items", 0), ("items", 1)])
        self.assertFalse(result["ok"])
        self.assertIn('"items"', result["cursor"])
        self.assertIn("boom", result["error"])

    def test_retry_with_cursor_never_repays_earlier_steps(self):
        _, first = self.run_deliver(fail_on=("items", 1))
        calls, second = self.run_deliver(cursor=first["cursor"])
        self.assertEqual(calls, [("items", 1), ("xp", 0), ("xp", 1)])
        self.assertTrue(second["ok"])


if __name__ == "__main__":
    unittest.main()


class SpecHashTests(unittest.TestCase):
    SPEC = rw.parse_spec('{"currency": 5, "items": [{"template": "A", "qty": 1, "quality": 0}]}')

    def test_cursor_carries_spec_hash_and_matches(self):
        cur = rw.cursor_of(rw.steps(self.SPEC)[1], self.SPEC)
        self.assertTrue(rw.cursor_matches_spec(cur, self.SPEC))

    def test_edited_spec_no_longer_matches(self):
        cur = rw.cursor_of(rw.steps(self.SPEC)[1], self.SPEC)
        edited = rw.parse_spec('{"currency": 5, "items": [{"template": "B", "qty": 1, "quality": 0}]}')
        self.assertFalse(rw.cursor_matches_spec(cur, edited))

    def test_legacy_cursor_without_hash_is_trusted(self):
        self.assertTrue(rw.cursor_matches_spec('{"stage": "items", "index": 0}', self.SPEC))
        self.assertTrue(rw.cursor_matches_spec("", self.SPEC))

    def test_xp_track_enum_enforced_at_parse(self):
        with self.assertRaises(ValueError):
            rw.parse_spec('{"xp": [{"track": "combat", "amount": 5}]}')  # lowercase
        with self.assertRaises(ValueError):
            rw.parse_spec('{"xp": [{"track": "Mining", "amount": 5}]}')
        rw.parse_spec('{"xp": [{"track": "Sabotage", "amount": 5}]}')  # valid
