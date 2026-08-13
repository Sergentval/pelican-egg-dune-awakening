#!/usr/bin/env python3
"""Golden tests for admin_ini_merge (Phase 5 INI engine).

These pin the EXACT behavior of apply-config.sh's apply_keyed/apply_kvp so the
refactor that makes apply-config delegate here is provably behavior-preserving,
plus the new render_value / normalize_value helpers used by PUT /api/settings.
"""
import unittest

from admin_ini_merge import (
    coerce_decimal_comma,
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

    def test_cvarbool(self):
        for t in ("true", "1", "yes", "on"):
            self.assertEqual(normalize_value(t, "cvarbool"), "1")
        for f in ("false", "0", "no", "off"):
            self.assertEqual(normalize_value(f, "cvarbool"), "0")
        with self.assertRaises(ValueError):
            normalize_value("maybe", "cvarbool")

    def test_int(self):
        self.assertEqual(normalize_value("42", "int"), "42")
        self.assertEqual(normalize_value(" -3 ", "int"), "-3")

    def test_int_invalid(self):
        with self.assertRaises(ValueError):
            normalize_value("1.5", "int")

    def test_float(self):
        self.assertEqual(normalize_value("1", "float"), "1.0")
        self.assertEqual(normalize_value("2.5", "float"), "2.5")

    def test_float_accepts_comma_decimal_separator(self):
        # Issue #82: on a German/French system the operator types 2,5.
        # UE5's atof stops at the comma and applies 2 — a silently wrong
        # value, not an error. Accept the comma and normalize it.
        self.assertEqual(normalize_value("2,5", "float"), "2.5")
        self.assertEqual(normalize_value(" -0,75 ", "float"), "-0.75")

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

    def test_struct_passthrough(self):
        # single-line struct values are written verbatim (no numeric coercion)
        v = "(Player=7.000000,Building=7.000000,Placeable=7.000000,Vehicle=7.000000)"
        self.assertEqual(normalize_value(v, "struct"), v)
        self.assertEqual(normalize_value("  (a=1)  ", "struct"), "(a=1)")

    def test_array_passthrough(self):
        self.assertEqual(normalize_value("(Name=\"Foo\")", "array"), '(Name="Foo")')

    def test_struct_array_reject_newline(self):
        # INI is line-based; a multi-line value cannot be a single key=value line
        for t in ("struct", "array"):
            with self.assertRaises(ValueError):
                normalize_value("line1\nline2", t)

    def test_struct_array_reject_empty(self):
        for t in ("struct", "array"):
            with self.assertRaises(ValueError):
                normalize_value("   ", t)


class TestCoerceDecimalComma(unittest.TestCase):
    """Issue #82: a German/French operator types 2,5 for a float setting.

    UE5 parses INI floats with atof, which stops at the comma: the server
    silently runs 2 instead of 2.5. The boot path (apply-config.sh) has no
    type coercion at all, so the comma reaches UserEngine.ini verbatim.
    """

    def test_rewrites_comma_on_float(self):
        self.assertEqual(coerce_decimal_comma("2,5", "float"), "2.5")
        self.assertEqual(coerce_decimal_comma("0,75", "float"), "0.75")
        self.assertEqual(coerce_decimal_comma("-1,25", "float"), "-1.25")
        self.assertEqual(coerce_decimal_comma("+3,5", "float"), "+3.5")

    def test_leaves_dot_form_untouched(self):
        for v in ("2.5", "1.0", "900.0", "0", "7200"):
            self.assertEqual(coerce_decimal_comma(v, "float"), v)

    def test_only_float_type_is_coerced(self):
        # intlist is legitimately comma-separated — never touch it.
        self.assertEqual(coerce_decimal_comma("1,2,3", "intlist"), "1,2,3")
        self.assertEqual(coerce_decimal_comma("2,5", "string"), "2,5")
        self.assertEqual(coerce_decimal_comma("2,5", "int"), "2,5")
        self.assertEqual(coerce_decimal_comma("2,5", "struct"), "2,5")

    def test_ambiguous_grouped_form_left_alone(self):
        # "1.000,5" mixes a group separator with a decimal comma. Guessing
        # here risks a 1000x error, so it passes through and fails validation
        # downstream instead.
        self.assertEqual(coerce_decimal_comma("1.000,5", "float"), "1.000,5")
        self.assertEqual(coerce_decimal_comma("2,5,1", "float"), "2,5,1")

    def test_non_numeric_left_alone(self):
        self.assertEqual(coerce_decimal_comma("abc", "float"), "abc")
        self.assertEqual(coerce_decimal_comma("", "float"), "")
        self.assertEqual(coerce_decimal_comma(",5", "float"), ",5")
        self.assertEqual(coerce_decimal_comma("5,", "float"), "5,")


if __name__ == "__main__":
    unittest.main()
