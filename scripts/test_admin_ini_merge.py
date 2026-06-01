#!/usr/bin/env python3
"""Golden tests for admin_ini_merge (Phase 5 INI engine).

These pin the EXACT behavior of apply-config.sh's apply_keyed/apply_kvp so the
refactor that makes apply-config delegate here is provably behavior-preserving,
plus the new render_value / normalize_value helpers used by PUT /api/settings.
"""
import unittest

from admin_ini_merge import (
    normalize_value,
    read_flat,
    read_keyed,
    render_value,
    upsert_flat,
    upsert_keyed,
)


class TestRead(unittest.TestCase):
    def test_read_keyed_present(self):
        text = "[/Script/X]\nFoo=9\nBar=2\n[/Other]\nFoo=nope\n"
        self.assertEqual(read_keyed(text, "/Script/X", "Foo"), "9")

    def test_read_keyed_wrong_section(self):
        text = "[/Other]\nFoo=nope\n"
        self.assertIsNone(read_keyed(text, "/Script/X", "Foo"))

    def test_read_keyed_absent(self):
        self.assertIsNone(read_keyed("[/Script/X]\nBar=2\n", "/Script/X", "Foo"))

    def test_read_keyed_commented_is_unset(self):
        self.assertIsNone(read_keyed("[/Script/X]\n;Foo=9\n", "/Script/X", "Foo"))

    def test_read_keyed_strips_quotes_left_to_caller(self):
        # read returns the raw RHS; quote-stripping is the caller's job
        self.assertEqual(read_keyed('[C]\nName="My Box"\n', "C", "Name"), '"My Box"')

    def test_read_flat(self):
        self.assertEqual(read_flat("A=1\nB=2\n", "B"), "2")
        self.assertIsNone(read_flat("A=1\n", "B"))


class TestRenderValue(unittest.TestCase):
    def test_unquoted_passthrough(self):
        self.assertEqual(render_value("1.5", False), "1.5")

    def test_quoted_wraps(self):
        self.assertEqual(render_value("My Server", True), '"My Server"')

    def test_quoted_rejects_embedded_quote(self):
        self.assertIsNone(render_value('he said "hi"', True))


class TestUpsertKeyed(unittest.TestCase):
    def test_replace_existing_key_in_section(self):
        text = "[/Script/X]\nFoo=1\nBar=2\n"
        out = upsert_keyed(text, "/Script/X", "Foo", "9")
        self.assertEqual(out, "[/Script/X]\nFoo=9\nBar=2\n")

    def test_append_key_to_existing_section(self):
        text = "[/Script/X]\nBar=2\n"
        out = upsert_keyed(text, "/Script/X", "Foo", "9")
        self.assertEqual(out, "[/Script/X]\nBar=2\nFoo=9\n")

    def test_new_section_appended(self):
        text = "[/Script/X]\nBar=2\n"
        out = upsert_keyed(text, "/Script/Y", "Foo", "9")
        self.assertEqual(out, "[/Script/X]\nBar=2\n\n[/Script/Y]\nFoo=9\n")

    def test_case_insensitive_section_and_key(self):
        text = "[/script/x]\nfoo=1\n"
        out = upsert_keyed(text, "/Script/X", "Foo", "9")
        # existing line is rewritten in place with the requested key casing
        self.assertEqual(out, "[/script/x]\nFoo=9\n")

    def test_commented_key_is_treated_as_the_key(self):
        text = "[/Script/X]\n;Foo=1\n"
        out = upsert_keyed(text, "/Script/X", "Foo", "9")
        self.assertEqual(out, "[/Script/X]\nFoo=9\n")

    def test_inject_before_next_header_keeps_trailing_blank(self):
        text = "[/Script/X]\nBar=2\n\n[/Script/Z]\nBaz=3\n"
        out = upsert_keyed(text, "/Script/X", "Foo", "9")
        self.assertEqual(out, "[/Script/X]\nBar=2\nFoo=9\n\n[/Script/Z]\nBaz=3\n")

    def test_idempotent(self):
        text = "[/Script/X]\nFoo=1\n"
        once = upsert_keyed(text, "/Script/X", "Foo", "9")
        twice = upsert_keyed(once, "/Script/X", "Foo", "9")
        self.assertEqual(once, twice)

    def test_unmanaged_content_preserved(self):
        text = "[/Script/X]\nKeep=yes\nFoo=1\n[/Other]\nHand=edit\n"
        out = upsert_keyed(text, "/Script/X", "Foo", "9")
        self.assertIn("Keep=yes", out)
        self.assertIn("[/Other]\nHand=edit", out)


class TestUpsertFlat(unittest.TestCase):
    def test_replace(self):
        self.assertEqual(upsert_flat("A=1\nB=2\n", "A", "9"), "A=9\nB=2\n")

    def test_append(self):
        self.assertEqual(upsert_flat("B=2\n", "A", "9"), "B=2\nA=9\n")

    def test_idempotent(self):
        once = upsert_flat("B=2\n", "A", "9")
        self.assertEqual(once, upsert_flat(once, "A", "9"))


class TestNormalizeValue(unittest.TestCase):
    def test_bool(self):
        for t in ("true", "True", "1", "yes", "on"):
            self.assertEqual(normalize_value(t, "bool"), "True")
        for f in ("false", "False", "0", "no", "off"):
            self.assertEqual(normalize_value(f, "bool"), "False")

    def test_bool_invalid(self):
        with self.assertRaises(ValueError):
            normalize_value("maybe", "bool")

    def test_int(self):
        self.assertEqual(normalize_value("42", "int"), "42")
        self.assertEqual(normalize_value(" -3 ", "int"), "-3")

    def test_int_invalid(self):
        with self.assertRaises(ValueError):
            normalize_value("1.5", "int")

    def test_float(self):
        self.assertEqual(normalize_value("1", "float"), "1.0")
        self.assertEqual(normalize_value("2.5", "float"), "2.5")

    def test_enum(self):
        self.assertEqual(normalize_value("None", "enum", ["UseAllowList", "None"]), "None")

    def test_enum_invalid(self):
        with self.assertRaises(ValueError):
            normalize_value("Nope", "enum", ["UseAllowList", "None"])

    def test_intlist(self):
        self.assertEqual(normalize_value("1, 2 ,3", "intlist"), "1,2,3")
        self.assertEqual(normalize_value("", "intlist"), "")

    def test_intlist_invalid(self):
        with self.assertRaises(ValueError):
            normalize_value("1,x", "intlist")

    def test_string_passthrough(self):
        self.assertEqual(normalize_value("hello world", "string"), "hello world")


if __name__ == "__main__":
    unittest.main()
