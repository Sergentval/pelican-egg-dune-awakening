#!/usr/bin/env python3
"""Unit tests for the Phase-1 Database-tab helpers in admin-http.py:
the read-only SQL guard (ported from dune-admin) and the psql-CSV ->
{headers, rows, truncated} shaper. Pure-logic, no DB/RMQ/network needed.

Run: python3 scripts/test_admin_database.py
"""
import importlib.util
import pathlib
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("admin_http", _HERE / "admin-http.py")
assert _spec is not None and _spec.loader is not None
admin_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin_http)

is_read_only_sql = admin_http.is_read_only_sql
csv_to_table = admin_http.csv_to_table


class ReadOnlyGuard(unittest.TestCase):
    def test_accepts_read_only(self):
        for q in (
            "SELECT 1",
            "  select * from dune.actors",
            "select\n*\nfrom x",
            "WITH t AS (SELECT 1) SELECT * FROM t",
            "EXPLAIN SELECT 1",
            "SHOW search_path",
            "/* comment */ SELECT 1",
            "-- a comment\nselect 1",
            "EXPLAIN (ANALYZE) SELECT 1",
        ):
            self.assertTrue(is_read_only_sql(q), f"should accept: {q!r}")

    def test_rejects_mutations(self):
        for q in (
            "DELETE FROM dune.actors",
            "UPDATE dune.actors SET x=1",
            "INSERT INTO dune.items VALUES (1)",
            "DROP TABLE dune.actors",
            "TRUNCATE dune.items",
            "GRANT ALL ON x TO y",
            "CALL some_proc()",
            "select1",  # no separator after select -> not read-only keyword
            "selectfoo from x",
            "; DELETE FROM x",
            "(SELECT 1)",  # leading '(' is NOT a read-only keyword start — verbatim guard rejects (matches dune-admin)
            "",
            "   ",
            "/* select */ delete from x",  # comment stripped -> starts with delete
            "-- select\ndelete from x",
        ):
            self.assertFalse(is_read_only_sql(q), f"should reject: {q!r}")


class CsvShaper(unittest.TestCase):
    def test_header_and_rows(self):
        out = csv_to_table("table,rows\nactors,42\nitems,7\n")
        self.assertEqual(out["headers"], ["table", "rows"])
        self.assertEqual(out["rows"], [["actors", "42"], ["items", "7"]])
        self.assertFalse(out["truncated"])

    def test_null_is_empty_field(self):
        # psql --csv renders SQL NULL as an empty field.
        out = csv_to_table("a,b,c\n1,,3\n")
        self.assertEqual(out["rows"], [["1", "", "3"]])

    def test_empty_input(self):
        out = csv_to_table("")
        self.assertEqual(out["headers"], [])
        self.assertEqual(out["rows"], [])
        self.assertFalse(out["truncated"])

    def test_header_only(self):
        out = csv_to_table("a,b\n")
        self.assertEqual(out["headers"], ["a", "b"])
        self.assertEqual(out["rows"], [])

    def test_truncate(self):
        body = "n\n" + "".join(f"{i}\n" for i in range(10))
        out = csv_to_table(body, truncate=3)
        self.assertEqual(out["headers"], ["n"])
        self.assertEqual(len(out["rows"]), 3)
        self.assertTrue(out["truncated"])

    def test_truncate_not_exceeded(self):
        out = csv_to_table("n\n1\n2\n", truncate=5)
        self.assertEqual(len(out["rows"]), 2)
        self.assertFalse(out["truncated"])

    def test_quoted_value_with_comma_and_newline(self):
        # csv.reader handles embedded commas/quotes/newlines correctly.
        out = csv_to_table('a,b\n"x,y","line1\nline2"\n')
        self.assertEqual(out["rows"], [["x,y", "line1\nline2"]])


if __name__ == "__main__":
    unittest.main()
