#!/usr/bin/env python3
"""Structural validation for data/admin/settings-schema.json (v3).

Guards the single-source-of-truth catalogue consumed by apply-config.sh and
admin-http /api/settings: unique ids, unique (file,section,key) sinks, valid
enumerations, and the v3 invariants (struct/array => advanced; UserGame
[/Script/...] sections; new UClass entries ship verified:false until live-tested).
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "data", "admin", "settings-schema.json")

VALID_TYPES = {"string", "float", "int", "bool", "cvarbool", "struct", "array"}
VALID_FILES = {"UserEngine", "UserGame", "UserOverrides", "ondemand"}
SECTIONED_FILES = {"UserGame", "UserOverrides"}
VALID_CLIENT_GATED = {"yes", "no", "unknown"}
REQUIRED = {"id", "file", "section", "key", "type", "default",
            "category", "label", "client_gated", "verified"}
SCALAR = {"string", "float", "int", "bool", "cvarbool"}


class TestSettingsSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCHEMA, encoding="utf-8") as f:
            cls.doc = json.load(f)
        cls.settings = cls.doc["settings"]

    def test_version_and_comment(self):
        self.assertEqual(self.doc["version"], 3)
        self.assertIn("DefaultGame.ini", self.doc["_comment"])

    def test_required_fields_present(self):
        for s in self.settings:
            missing = REQUIRED - set(s)
            self.assertFalse(missing, f"{s.get('id')} missing {missing}")

    def test_ids_unique(self):
        ids = [s["id"] for s in self.settings]
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertFalse(dupes, f"duplicate ids: {dupes}")

    def test_sink_unique(self):
        sinks = [(s["file"], s["section"], s["key"]) for s in self.settings]
        dupes = {t for t in sinks if sinks.count(t) > 1}
        self.assertFalse(dupes, f"duplicate (file,section,key) sinks: {dupes}")

    def test_enumerations_valid(self):
        for s in self.settings:
            self.assertIn(s["type"], VALID_TYPES, s["id"])
            self.assertIn(s["file"], VALID_FILES, s["id"])
            self.assertIn(s["client_gated"], VALID_CLIENT_GATED, s["id"])

    def test_env_unique(self):
        envs = [s["env"] for s in self.settings if s.get("env")]
        dupes = {e for e in envs if envs.count(e) > 1}
        self.assertFalse(dupes, f"duplicate env vars: {dupes}")

    def test_struct_array_are_advanced(self):
        for s in self.settings:
            if s["type"] in ("struct", "array"):
                self.assertTrue(s.get("advanced", False),
                                f"{s['id']} is {s['type']} but not advanced")

    def test_sectioned_files_have_paths(self):
        for s in self.settings:
            if s["file"] in SECTIONED_FILES:
                self.assertIsNotNone(s["section"], s["id"])
                self.assertTrue(s["section"].startswith("/"),
                                f"{s['id']} {s['file']} section must be a /Script path")

    def test_ondemand_has_no_section(self):
        for s in self.settings:
            if s["file"] == "ondemand":
                self.assertIsNone(s["section"], s["id"])

    def test_advanced_entries_not_boot_applied(self):
        # struct/array values can't be rendered as a single boot-applied INI line
        for s in self.settings:
            if s.get("advanced") and s["type"] in ("struct", "array"):
                self.assertFalse(s.get("env"),
                                 f"{s['id']} advanced struct/array must not be env/boot-applied")

    def test_catalogue_size(self):
        # 51 original (24 env + 27 cvar) + 144 verified UClass knobs
        self.assertEqual(len(self.settings), 195)
        # the 144 API-managed UClass knobs sink to UserOverrides, no env var
        uclass = [s for s in self.settings if s["file"] == "UserOverrides"]
        self.assertEqual(len(uclass), 144)
        for s in uclass:
            self.assertFalse(s.get("env"), f"{s['id']} UserOverrides knob must not be env/boot-applied")
        # untested knobs ship verified:false; only live-tested ones are flipped
        byid = {s["id"]: s for s in uclass}
        self.assertTrue(byid["hydrationsubsystem_m_bhydrationenabled"]["verified"],
                        "hydration toggle was live-confirmed working")
        self.assertEqual(byid["inventorysystemsettings_playerinventorystartingvolumecapacity"]["client_gated"], "yes",
                         "inventory volume was live-confirmed client-gated")


if __name__ == "__main__":
    unittest.main()
