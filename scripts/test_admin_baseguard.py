"""Tests for admin_baseguard.py — the BaseBackup wipe-guard text engine.

The subject is pure text surgery on pg_get_functiondef output, so every case
runs on fixture strings; the live read/write path is publish.sh's job.
"""
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

import admin_baseguard as bg

# Mirrors the live definition's shape (verified 2026-08-20 on the test
# server): tab+space indentation, the three-state exclusion list, and the
# rest of Funcom's body that must survive the patch byte-for-byte.
FIXTURE = (
    "CREATE OR REPLACE FUNCTION dune.delete_actors_and_respawns_on_server("
    "in_server_info server_info, in_vehicle_classes_spawned_on_map text[], "
    "in_allow_vehicle_recovery boolean)\n"
    " RETURNS void\n"
    " LANGUAGE plpgsql\n"
    "AS $function$\n"
    "BEGIN\n"
    "    WITH actors_to_delete AS (\n"
    "\t    SELECT a.id\n"
    "        FROM actors a\n"
    "        LEFT JOIN actor_state s ON a.id = s.actor_id\n"
    "\t    WHERE owner_account_id IS NULL\n"
    "\t    AND s.state IS DISTINCT FROM 'Travel'\n"
    "\t    AND s.state IS DISTINCT FROM 'VehicleBackup'\n"
    "\t    AND s.state IS DISTINCT FROM 'VehicleRecovery'\n"
    "\t    AND server_info_match(a, in_server_info)\n"
    "\t    ORDER BY a.id FOR UPDATE OF a\n"
    "    )\n"
    "    DELETE FROM actors a WHERE a.id = ANY(SELECT id FROM actors_to_delete);\n"
    "END\n"
    "$function$\n"
)


class DetectTests(unittest.TestCase):
    def test_original_is_not_applied(self):
        self.assertFalse(bg.is_applied(FIXTURE))

    def test_patched_is_applied(self):
        self.assertTrue(bg.is_applied(bg.add_predicate(FIXTURE)["definition"]))

    def test_detection_is_whitespace_tolerant(self):
        self.assertTrue(bg.is_applied("x IS  DISTINCT\tFROM 'BaseBackup' y"))

    def test_empty_definition_is_not_applied(self):
        self.assertFalse(bg.is_applied(""))


class AddTests(unittest.TestCase):
    def test_patch_inserts_after_vehicle_recovery_with_same_indent(self):
        res = bg.add_predicate(FIXTURE)
        self.assertTrue(res["ok"])
        self.assertTrue(res["changed"])
        self.assertEqual(res["reason"], "patched")
        self.assertIn(
            "\t    AND s.state IS DISTINCT FROM 'VehicleRecovery'\n"
            "\t    AND s.state IS DISTINCT FROM 'BaseBackup'\n",
            res["definition"])

    def test_patch_preserves_everything_else(self):
        res = bg.add_predicate(FIXTURE)
        stripped = res["definition"].replace(
            "\t    AND s.state IS DISTINCT FROM 'BaseBackup'\n", "", 1)
        self.assertEqual(stripped, FIXTURE)

    def test_patch_is_idempotent(self):
        once = bg.add_predicate(FIXTURE)["definition"]
        res = bg.add_predicate(once)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(res["reason"], "already-applied")
        self.assertEqual(res["definition"], once)

    def test_empty_definition_fails(self):
        res = bg.add_predicate("")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "empty-definition")

    def test_missing_anchor_fails_closed(self):
        mutated = FIXTURE.replace("'VehicleRecovery'", "'SomethingElse'")
        res = bg.add_predicate(mutated)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "anchor-not-found")
        self.assertEqual(res["definition"], mutated)

    def test_crlf_definition_keeps_crlf_and_round_trips(self):
        crlf = FIXTURE.replace("\n", "\r\n")
        res = bg.add_predicate(crlf)
        self.assertTrue(res["ok"])
        self.assertIn("AND s.state IS DISTINCT FROM 'BaseBackup'\r\n",
                      res["definition"])
        back = bg.remove_predicate(res["definition"])
        self.assertTrue(back["ok"])
        self.assertEqual(back["definition"], crlf)


class RemoveTests(unittest.TestCase):
    def test_round_trip_is_byte_identical(self):
        patched = bg.add_predicate(FIXTURE)["definition"]
        res = bg.remove_predicate(patched)
        self.assertTrue(res["ok"])
        self.assertTrue(res["changed"])
        self.assertEqual(res["definition"], FIXTURE)

    def test_already_absent_is_a_no_op(self):
        res = bg.remove_predicate(FIXTURE)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(res["reason"], "already-absent")

    def test_predicate_not_on_its_own_line_fails(self):
        inline = FIXTURE.replace(
            "AND s.state IS DISTINCT FROM 'VehicleRecovery'",
            "AND s.state IS DISTINCT FROM 'VehicleRecovery' "
            "AND s.state IS DISTINCT FROM 'BaseBackup'")
        res = bg.remove_predicate(inline)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "predicate-not-on-its-own-line")

    def test_empty_definition_fails(self):
        res = bg.remove_predicate("")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "empty-definition")


class CliTests(unittest.TestCase):
    """main() is the contract publish.sh scripts against — exit codes are
    load-bearing: 0 = did it, 4 = nothing to do, 3 = refused, 2 = bad input."""

    def run_main(self, argv, stdin_text):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bg.main(argv, stdin_text)
        return code, out.getvalue(), err.getvalue()

    def test_check_not_applied(self):
        code, _, _ = self.run_main(["check"], FIXTURE)
        self.assertEqual(code, 1)

    def test_check_applied(self):
        patched = bg.add_predicate(FIXTURE)["definition"]
        code, _, _ = self.run_main(["check"], patched)
        self.assertEqual(code, 0)

    def test_check_empty_input(self):
        code, _, _ = self.run_main(["check"], "")
        self.assertEqual(code, 2)

    def test_patch_outputs_patched_definition(self):
        code, out, _ = self.run_main(["patch"], FIXTURE)
        self.assertEqual(code, 0)
        self.assertTrue(bg.is_applied(out))
        self.assertEqual(bg.remove_predicate(out)["definition"], FIXTURE)

    def test_patch_already_applied_exits_4(self):
        patched = bg.add_predicate(FIXTURE)["definition"]
        code, out, _ = self.run_main(["patch"], patched)
        self.assertEqual(code, 4)
        self.assertEqual(out, patched)

    def test_patch_anchor_missing_exits_3_with_reason(self):
        mutated = FIXTURE.replace("'VehicleRecovery'", "'SomethingElse'")
        code, out, err = self.run_main(["patch"], mutated)
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("anchor-not-found", err)

    def test_unpatch_round_trip(self):
        patched = bg.add_predicate(FIXTURE)["definition"]
        code, out, _ = self.run_main(["unpatch"], patched)
        self.assertEqual(code, 0)
        self.assertEqual(out, FIXTURE)

    def test_unpatch_already_absent_exits_4(self):
        code, out, _ = self.run_main(["unpatch"], FIXTURE)
        self.assertEqual(code, 4)
        self.assertEqual(out, FIXTURE)

    def test_unknown_subcommand_exits_2(self):
        code, _, err = self.run_main(["frobnicate"], FIXTURE)
        self.assertEqual(code, 2)
        self.assertIn("usage", err)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.base = tempfile.mkdtemp(prefix="baseguard-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def test_missing_config_reads_disabled(self):
        self.assertFalse(bg.load_enabled(self.base))

    def test_save_then_load_round_trips(self):
        self.assertTrue(bg.save_enabled(self.base, True))
        self.assertTrue(bg.load_enabled(self.base))
        self.assertTrue(bg.save_enabled(self.base, False))
        self.assertFalse(bg.load_enabled(self.base))

    def test_malformed_config_reads_disabled(self):
        import os
        path = bg.config_path(self.base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(bg.load_enabled(self.base))

    def test_truthy_but_not_true_reads_disabled(self):
        import os
        path = bg.config_path(self.base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"enabled": "yes"}')
        self.assertFalse(bg.load_enabled(self.base))

    def test_cli_enabled_exit_codes(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(bg.main(["enabled", self.base], ""), 1)
        bg.save_enabled(self.base, True)
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(bg.main(["enabled", self.base], ""), 0)

    def test_cli_enabled_without_base_exits_2(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(bg.main(["enabled"], ""), 2)


if __name__ == "__main__":
    unittest.main()
