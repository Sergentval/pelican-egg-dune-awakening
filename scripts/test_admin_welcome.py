#!/usr/bin/env python3
"""Unit tests for admin_welcome (Phase 6 — welcome kits).

Ports the dune-admin welcome_store / welcome_package logic: a sqlite ledger
(idempotent per package version) + a scan that grants the active package to
online players not yet recorded.
"""
import unittest

from admin_welcome import (
    WelcomeLedger,
    parse_accounts_csv,
    scan_once,
    validate_items,
)


class TestValidateItems(unittest.TestCase):
    def test_ok(self):
        validate_items([{"template": "Stone", "qty": 5, "quality": 0}])

    def test_empty_list(self):
        with self.assertRaises(ValueError):
            validate_items([])

    def test_empty_template(self):
        with self.assertRaises(ValueError):
            validate_items([{"template": "  ", "qty": 1, "quality": 0}])

    def test_bad_qty(self):
        with self.assertRaises(ValueError):
            validate_items([{"template": "Stone", "qty": 0, "quality": 0}])

    def test_bad_quality(self):
        with self.assertRaises(ValueError):
            validate_items([{"template": "Stone", "qty": 1, "quality": -1}])


class TestParseAccountsCsv(unittest.TestCase):
    def test_normal(self):
        csv = "fls,account_id\nDE0BCCAA2501BF22,2\nAABBCCDD11223344,7\n"
        got = parse_accounts_csv(csv)
        self.assertEqual(got, [
            {"fls": "DE0BCCAA2501BF22", "account_id": 2},
            {"fls": "AABBCCDD11223344", "account_id": 7},
        ])

    def test_skips_header_blanks_crlf_and_bad_rows(self):
        csv = "fls,account_id\r\n\r\nDE0BCCAA2501BF22,2\r\n   \nBADROW\nX,notanint\n"
        got = parse_accounts_csv(csv)
        self.assertEqual(got, [{"fls": "DE0BCCAA2501BF22", "account_id": 2}])

    def test_empty(self):
        self.assertEqual(parse_accounts_csv(""), [])


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.led = WelcomeLedger(":memory:")

    def test_grant_lifecycle(self):
        self.assertFalse(self.led.granted_exists("FLS1", "v1", 2))
        self.led.record_granted("FLS1", "v1", 2, "Alice")
        self.assertTrue(self.led.granted_exists("FLS1", "v1", 2))
        # different version is a fresh grant
        self.assertFalse(self.led.granted_exists("FLS1", "v2", 2))

    def test_failed_blocks_then_retry_clears(self):
        self.led.record_failed("FLS2", "v1", 7, "Bob", "inventory full")
        self.assertTrue(self.led.granted_exists("FLS2", "v1", 7))  # failed row blocks re-grant
        self.assertEqual(self.led.delete_failed("FLS2", "v1", 7), 1)
        self.assertFalse(self.led.granted_exists("FLS2", "v1", 7))  # cleared -> retryable

    def test_stats_and_list(self):
        self.led.record_granted("A", "v1", 1, "")
        self.led.record_failed("B", "v1", 2, "", "boom")
        st = self.led.stats()
        self.assertEqual(st["granted"], 1)
        self.assertEqual(st["failed"], 1)
        self.assertEqual(len(self.led.list_grants(100)), 2)


class TestScanOnce(unittest.TestCase):
    def setUp(self):
        self.led = WelcomeLedger(":memory:")
        self.items = [{"template": "Stone", "qty": 5, "quality": 0}]
        self.calls = []

    def _grant_ok(self, account, items):
        self.calls.append(account["fls"])
        return []  # no skip reasons -> full success

    def _grant_fail(self, account, items):
        self.calls.append(account["fls"])
        return ["Stone: inventory full"]

    def test_grants_new_skips_missing_fls(self):
        accounts = [
            {"fls": "A", "account_id": 1},
            {"fls": "B", "account_id": 2},
            {"fls": "", "account_id": 3},  # no fls -> skipped silently
        ]
        r = scan_once("v1", self.items, accounts, self.led, self._grant_ok)
        self.assertEqual(r, {"granted": 2, "failed": 0, "skipped": 0})
        self.assertEqual(self.calls, ["A", "B"])

    def test_idempotent_second_scan_skips(self):
        accounts = [{"fls": "A", "account_id": 1}]
        scan_once("v1", self.items, accounts, self.led, self._grant_ok)
        self.calls.clear()
        r = scan_once("v1", self.items, accounts, self.led, self._grant_ok)
        self.assertEqual(r, {"granted": 0, "failed": 0, "skipped": 1})
        self.assertEqual(self.calls, [])  # grant not attempted again

    def test_failure_records_and_blocks_until_retry(self):
        accounts = [{"fls": "A", "account_id": 1}]
        r = scan_once("v1", self.items, accounts, self.led, self._grant_fail)
        self.assertEqual(r, {"granted": 0, "failed": 1, "skipped": 0})
        # blocked on next scan (failed row exists)
        r2 = scan_once("v1", self.items, accounts, self.led, self._grant_ok)
        self.assertEqual(r2["skipped"], 1)
        # after clearing the failure it retries and succeeds
        self.led.delete_failed("A", "v1", 1)
        r3 = scan_once("v1", self.items, accounts, self.led, self._grant_ok)
        self.assertEqual(r3["granted"], 1)


if __name__ == "__main__":
    unittest.main()
