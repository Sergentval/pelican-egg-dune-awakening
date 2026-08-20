#!/usr/bin/env python3
"""Chat-commands pure logic (ecosystem-survey port C5, from DST v13.4).

The broker plumbing (bounded copy-queue on chat.intercept, basic_get drain)
lives in admin-publish.sh; this tests the envelope parsing (outer {Type,
content} with an INNER JSON STRING), command extraction, per-player+command
cooldowns, and the config gate semantics (master OFF by default, per-command
opt-in). Run: python3 scripts/test_admin_chatcmd.py
"""
import json
import time
import unittest

from admin_chatcmd import (
    CooldownLedger,
    command_of,
    default_config,
    normalize_config,
    parse_chat_message,
)


def envelope(text, channel="Proximity", sender="Coastal#45066"):
    inner = {"m_ChannelType": channel, "m_FuncomIdFrom": sender,
             "m_Message": {"m_UnlocalizedMessage": text},
             "m_Timestamp": "2026.08.20-12.00.00"}
    return json.dumps({"Type": "TextChat", "content": json.dumps(inner)})


class TestParse(unittest.TestCase):
    def test_parses_double_encoded_envelope(self):
        msg = parse_chat_message(envelope("!ping"))
        self.assertEqual(msg, {"channel": "Proximity", "sender": "Coastal#45066",
                               "text": "!ping"})

    def test_non_textchat_is_none(self):
        self.assertIsNone(parse_chat_message(json.dumps({"Type": "Other", "content": "{}"})))

    def test_garbage_is_none(self):
        self.assertIsNone(parse_chat_message("not json"))
        self.assertIsNone(parse_chat_message(json.dumps({"Type": "TextChat", "content": "not json"})))

    def test_missing_fields_are_blank(self):
        raw = json.dumps({"Type": "TextChat", "content": json.dumps({"m_Message": {}})})
        msg = parse_chat_message(raw)
        self.assertEqual(msg, {"channel": "", "sender": "", "text": ""})


class TestCommandOf(unittest.TestCase):
    def test_bang_prefix_lowercased(self):
        self.assertEqual(command_of("!PING"), ("ping", ""))

    def test_args_preserved(self):
        self.assertEqual(command_of("!kit  starter pack "), ("kit", "starter pack"))

    def test_plain_chat_is_none(self):
        self.assertIsNone(command_of("hello there"))
        self.assertIsNone(command_of("! spaced"))
        self.assertIsNone(command_of(""))

    def test_command_charset_is_strict(self):
        # A command is short ascii letters only — anything else is chat.
        self.assertIsNone(command_of("!p@wn"))
        self.assertIsNone(command_of("!" + "a" * 33))


class TestCooldowns(unittest.TestCase):
    def test_first_use_allowed_then_blocked(self):
        led = CooldownLedger(cooldown_secs=60)
        self.assertTrue(led.allow("Coastal#45066", "kit", now=1000))
        self.assertFalse(led.allow("Coastal#45066", "kit", now=1030))
        self.assertTrue(led.allow("Coastal#45066", "kit", now=1061))

    def test_isolated_per_player_and_command(self):
        led = CooldownLedger(cooldown_secs=60)
        self.assertTrue(led.allow("A#1", "kit", now=1000))
        self.assertTrue(led.allow("B#2", "kit", now=1000))
        self.assertTrue(led.allow("A#1", "ping", now=1000))

    def test_state_roundtrip(self):
        led = CooldownLedger(cooldown_secs=60)
        led.allow("A#1", "kit", now=1000)
        led2 = CooldownLedger(cooldown_secs=60, state=led.state())
        self.assertFalse(led2.allow("A#1", "kit", now=1030))

    def test_state_is_pruned(self):
        led = CooldownLedger(cooldown_secs=60)
        led.allow("A#1", "kit", now=1000)
        led.allow("B#2", "kit", now=5000)
        # Entries older than the cooldown are dead weight; state() drops them.
        self.assertNotIn("A#1|kit", led.state())


class TestConfig(unittest.TestCase):
    def test_default_is_off(self):
        cfg = default_config()
        self.assertFalse(cfg["enabled"])
        self.assertFalse(any(c.get("enabled") for c in cfg["commands"].values()
                             if c is not cfg["commands"]["ping"]))

    def test_normalize_rejects_junk(self):
        cfg = normalize_config({"enabled": "yes", "cooldown_secs": "abc",
                                "commands": {"kit": {"enabled": 1, "items": "no"}}})
        self.assertIs(cfg["enabled"], True)
        self.assertEqual(cfg["cooldown_secs"], default_config()["cooldown_secs"])
        self.assertIs(cfg["commands"]["kit"]["enabled"], True)
        self.assertEqual(cfg["commands"]["kit"]["items"], [])

    def test_normalize_keeps_valid_items(self):
        cfg = normalize_config({"commands": {"kit": {
            "items": [{"template": "AAR1_Spice", "qty": 5},
                      {"template": "", "qty": 1},
                      {"nope": True}]}}})
        self.assertEqual(cfg["commands"]["kit"]["items"],
                         [{"template": "AAR1_Spice", "qty": 5}])


if __name__ == "__main__":
    unittest.main()
