#!/usr/bin/env python3
"""Tests for the shared Pelican client-API access.

Status codes here are not interchangeable — each points the operator
somewhere different — and they were measured against a live panel:

    unknown variable      -> 400 "The environment variable ... does not exist."
    value violates rules  -> 422 "The value must be a number."   (no change made)
    Application key       -> 403
    bad/quoted/empty key  -> 401
    unknown server id     -> 404

Run: python3 scripts/test_admin_pelican.py
"""
import http.client
import os
import unittest

import admin_pelican as pel

KEY = "pacc_verysecretvalue0000"
ENV_KEYS = ("DUNE_PELICAN_URL", "DUNE_PELICAN_CLIENT_KEY",
            "DUNE_PELICAN_SERVER_ID", "DUNE_PELICAN_RESOLVE")


class EnvCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def configure(self, url="https://panel.example.com", key=KEY, sid="abc12345", resolve=None):
        os.environ["DUNE_PELICAN_URL"] = url
        os.environ["DUNE_PELICAN_CLIENT_KEY"] = key
        os.environ["DUNE_PELICAN_SERVER_ID"] = sid
        if resolve:
            os.environ["DUNE_PELICAN_RESOLVE"] = resolve


class Hygiene(unittest.TestCase):
    def test_strips_whitespace_and_quotes(self):
        for raw in ('  abc  ', '"abc"', "'abc'", '  "abc"  '):
            with self.subTest(raw=raw):
                self.assertEqual(pel.clean_setting(raw), "abc")

    def test_none_and_empty(self):
        self.assertEqual(pel.clean_setting(None), "")
        self.assertEqual(pel.clean_setting('""'), "")

    def test_inner_quotes_survive(self):
        self.assertEqual(pel.clean_setting('ab"cd'), 'ab"cd')

    def test_redact(self):
        self.assertNotIn(KEY, pel.redact(f"boom Bearer {KEY}", KEY))
        self.assertEqual(pel.redact("plain", ""), "plain")


class UrlParsing(unittest.TestCase):
    def test_https_default_port(self):
        self.assertEqual(pel.split_url("https://panel.example.com"),
                         ("https", "panel.example.com", 443))

    def test_http_default_port(self):
        self.assertEqual(pel.split_url("http://10.0.0.5"), ("http", "10.0.0.5", 80))

    def test_explicit_port(self):
        self.assertEqual(pel.split_url("https://panel.example.com:8443"),
                         ("https", "panel.example.com", 8443))

    def test_no_host(self):
        self.assertEqual(pel.split_url("not-a-url")[1], "")


class Connections(unittest.TestCase):
    def test_resolve_ip_pins_the_tcp_target_only(self):
        conn = pel.open_conn("https", "panel.example.com", 443, "10.99.0.1", 5)
        self.assertIsInstance(conn, pel.ResolvingHTTPSConnection)
        self.assertEqual(conn.host, "panel.example.com", "SNI/Host must stay the hostname")
        self.assertEqual(conn._resolve_ip, "10.99.0.1")

    def test_plain_https(self):
        conn = pel.open_conn("https", "panel.example.com", 443, None, 5)
        self.assertIsInstance(conn, http.client.HTTPSConnection)
        self.assertNotIsInstance(conn, pel.ResolvingHTTPSConnection)

    def test_plain_http(self):
        conn = pel.open_conn("http", "10.0.0.5", 80, None, 5)
        self.assertNotIsInstance(conn, http.client.HTTPSConnection)


class PanelDetail(unittest.TestCase):
    def test_extracts_the_panels_own_explanation(self):
        body = ('{"errors":[{"code":"ValidationException","status":"422",'
                '"detail":"The value must be a number."}]}')
        self.assertEqual(pel._detail(body), "The value must be a number.")

    def test_tolerates_junk(self):
        for body in ("", "not json", "{}", '{"errors":[]}', "[1,2]"):
            with self.subTest(body=body):
                self.assertEqual(pel._detail(body), "")


class Hints(unittest.TestCase):
    def test_401_points_at_the_credential(self):
        self.assertIn("not a valid client key", pel.variable_hint(401))
        self.assertIn("not a valid client key", pel.restart_hint(401))

    def test_403_points_at_the_key_type(self):
        self.assertIn("Account API key", pel.variable_hint(403))

    def test_404_points_at_the_server_id(self):
        self.assertIn("DUNE_PELICAN_SERVER_ID", pel.variable_hint(404))

    def test_400_on_a_variable_points_at_the_egg_import(self):
        # Measured: this is what you get when the egg in the panel predates
        # the variable — a reinstall updates scripts/, not the egg.
        hint = pel.variable_hint(400)
        self.assertIn("re-import the egg", hint)

    def test_success_carries_no_hint(self):
        self.assertEqual(pel.variable_hint(200), "")


class SetVariable(EnvCase):
    def test_refuses_when_unconfigured(self):
        ok, detail = pel.set_variable("DUNE_MINING_OUTPUT", "3")
        self.assertFalse(ok)
        self.assertIn("DUNE_PELICAN_", detail)

    def test_control_character_in_the_key_never_echoes_it(self):
        self.configure(key=f"pacc_ab\ncd{KEY}")
        ok, detail = pel.set_variable("DUNE_MINING_OUTPUT", "3")
        self.assertFalse(ok)
        self.assertIn("control character", detail)
        self.assertNotIn(KEY, detail)

    def test_network_failure_is_reported_without_the_key(self):
        self.configure()  # panel.example.com does not resolve
        ok, detail = pel.set_variable("DUNE_MINING_OUTPUT", "3")
        self.assertFalse(ok)
        self.assertNotIn(KEY, detail)
        self.assertIn("failed", detail)

    def test_restart_is_skipped_when_unconfigured(self):
        ok, detail = pel.restart()
        self.assertFalse(ok)
        self.assertIn("skipped", detail)


if __name__ == "__main__":
    unittest.main()
