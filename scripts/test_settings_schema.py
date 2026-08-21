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

VALID_TYPES = {"string", "float", "int", "bool", "cvarbool", "intlist", "struct", "array"}
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

    def test_every_env_is_a_real_egg_variable(self):
        """A setting's `env` is what apply-config.sh reapplies at boot and
        what /api/settings pins so a restart stops reverting the change.
        An `env` with no matching egg variable is therefore a setting the
        operator can never set — the panel would answer HTTP 400, "the
        environment variable you are trying to edit does not exist". Both
        eggs must carry it, since they are shipped as one variable set."""
        for egg_name in ("egg-dune-awakening.json", "egg-dune-awakening-pterodactyl.json"):
            with open(os.path.join(ROOT, egg_name), encoding="utf-8") as f:
                egg_vars = {v["env_variable"] for v in json.load(f)["variables"]}
            for s in self.settings:
                if s.get("env"):
                    self.assertIn(s["env"], egg_vars,
                                  f"{s['id']} declares env {s['env']} but {egg_name} has no such variable")

    def test_catalogue_size(self):
        # 51 original (25 env + 26 cvar) + 144 UClass knobs
        # + pvp_enabled_partitions + DD picker routing (issue #106)
        # + 5 QoL keys from the 2026-08 ecosystem survey (reconnect grace ×2,
        #   ping system ×3) + m_BaseBackupToolMapRestriction (C3.5)
        self.assertEqual(len(self.settings), 202)
        # the API-managed UClass knobs sink to UserOverrides, no env var
        uclass = [s for s in self.settings if s["file"] == "UserOverrides"]
        self.assertEqual(len(uclass), 150)
        for s in uclass:
            self.assertFalse(s.get("env"), f"{s['id']} UserOverrides knob must not be env/boot-applied")
        # untested knobs ship verified:false; only live-tested ones are flipped
        byid = {s["id"]: s for s in uclass}
        self.assertTrue(byid["hydrationsubsystem_m_bhydrationenabled"]["verified"],
                        "hydration toggle was live-confirmed working")
        self.assertEqual(byid["inventorysystemsettings_playerinventorystartingvolumecapacity"]["client_gated"], "yes",
                         "inventory volume was live-confirmed client-gated")

    def test_blank_password_contract(self):
        """Issue #107: server_password declares empty_ok so a cleared panel
        variable is applied at boot (passwordless public server) instead of
        being skipped as "not configured"."""
        pw = next(s for s in self.settings if s["id"] == "server_password")
        self.assertIs(pw.get("empty_ok"), True)

    def test_empty_ok_only_on_env_backed_strings(self):
        for s in self.settings:
            if "empty_ok" in s:
                self.assertIsInstance(s["empty_ok"], bool, s["id"])
                self.assertTrue(s.get("env"),
                                f"{s['id']}: empty_ok is a boot-path (env) semantic")
                self.assertIn(s["type"], ("string", "intlist"),
                              f"{s['id']}: blank is only meaningful for strings and lists")

    def test_empty_ok_variables_accept_blank_in_both_eggs(self):
        """The egg rule is what the panel enforces: `required` makes a blank
        unsaveable in the startup tab (issue #107's first symptom), and
        pin_setting_to_egg then surfaces Pelican's rejection in the admin UI
        (the second symptom). Every empty_ok variable must be nullable."""
        empty_ok_envs = {s["env"] for s in self.settings if s.get("empty_ok")}
        self.assertTrue(empty_ok_envs, "at least server_password is empty_ok")
        for egg_name in ("egg-dune-awakening.json", "egg-dune-awakening-pterodactyl.json"):
            with open(os.path.join(ROOT, egg_name), encoding="utf-8") as f:
                for v in json.load(f)["variables"]:
                    if v["env_variable"] not in empty_ok_envs:
                        continue
                    rules = v["rules"]
                    rules = rules.split("|") if isinstance(rules, str) else rules
                    self.assertNotIn("required", rules, f"{egg_name}:{v['env_variable']}")
                    self.assertIn("nullable", rules, f"{egg_name}:{v['env_variable']}")

    def test_pvp_partitions_contract(self):
        """Issue #106: Funcom's per-instance PvP is `+m_PvpEnabledPartitions=<id>`
        lines (one per partition) in the SHARED UserGame.ini — documented as a
        commented example in Funcom's own template. Exposed as an intlist panel
        variable; verified stays False until a game client confirms the badge."""
        st = next(s for s in self.settings if s["id"] == "pvp_enabled_partitions")
        self.assertEqual(st["env"], "DUNE_PVP_PARTITIONS")
        self.assertEqual(st["file"], "UserGame")
        self.assertEqual(st["section"], "/Script/DuneSandbox.PvpPveSettings")
        self.assertEqual(st["key"], "m_PvpEnabledPartitions")
        self.assertEqual(st["type"], "intlist")
        self.assertIs(st.get("repeated"), True)
        self.assertIs(st.get("empty_ok"), True)
        # verified flipped 2026-08-20: the issue reporter confirmed the PvP
        # badge and rules in game on all four partitions.
        self.assertIs(st["verified"], True)
        self.assertFalse(st["advanced"])

    def test_repeated_only_on_sectioned_intlists(self):
        """`repeated` renders one +key= line per element — that only makes sense
        for an intlist sinking into a sectioned UE INI file."""
        for s in self.settings:
            if s.get("repeated"):
                self.assertEqual(s["type"], "intlist", s["id"])
                self.assertTrue(s.get("section"), f"{s['id']}: repeated needs a [section]")
                self.assertNotEqual(s["file"], "ondemand", s["id"])

    def test_repeated_settings_are_not_per_sietch(self):
        """m_PvpEnabledPartitions is a battlegroup-wide designation every UE5
        process must agree on; a per-sietch copy would silently desync it."""
        import admin_sietch
        capable_ids = {c["id"] for c in admin_sietch.capable(ROOT)}
        for s in self.settings:
            if s.get("repeated"):
                self.assertNotIn(s["id"], capable_ids)


if __name__ == "__main__":
    unittest.main()
