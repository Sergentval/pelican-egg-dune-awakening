#!/usr/bin/env python3
"""Tests for direct TLS termination (admin_tls).

The panel has always left TLS to whatever fronts it, which leaves an
operator who points a DNS record straight at an exposed box with no
https:// to reach. These cover serving from a certificate on disk and,
above all, picking up a renewed one without a restart — automating
renewal is pointless if it still needs someone to restart the panel.

Run: python3 scripts/test_admin_tls.py
"""
import os
import pathlib
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest

import admin_tls


def make_cert(directory: pathlib.Path, cn: str) -> tuple[str, str]:
    """A self-signed pair, the way prestart.sh already makes one for RMQ."""
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "fullchain.pem", directory / "privkey.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", str(key), "-out", str(cert),
        "-subj", f"/CN={cn}", "-addext", f"subjectAltName=DNS:{cn}",
    ], check=True, capture_output=True)
    return str(cert), str(key)


class Paths(unittest.TestCase):
    def setUp(self):
        for k in ("DUNE_ADMIN_UI_TLS_CERT", "DUNE_ADMIN_UI_TLS_KEY"):
            self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)

    def test_defaults_live_under_server_state(self):
        cert, key = admin_tls.cert_paths("/home/container")
        # server/state/ is what a reinstall does not wipe — a certificate
        # anywhere else would vanish on the next egg update.
        self.assertIn("/server/state/tls/", cert)
        self.assertIn("/server/state/tls/", key)

    def test_environment_overrides(self):
        os.environ["DUNE_ADMIN_UI_TLS_CERT"] = "/etc/le/cert.pem"
        os.environ["DUNE_ADMIN_UI_TLS_KEY"] = "  /etc/le/key.pem  "
        self.assertEqual(admin_tls.cert_paths("/base"), ("/etc/le/cert.pem", "/etc/le/key.pem"))


class Context(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        for k in ("DUNE_ADMIN_UI_TLS_CERT", "DUNE_ADMIN_UI_TLS_KEY"):
            os.environ.pop(k, None)

    def test_no_certificate_is_not_an_error(self):
        self.assertFalse(admin_tls.available(str(self.base)))
        self.assertIsNone(admin_tls.build_context(str(self.base), log=lambda *_: None))

    def test_serves_from_a_certificate_on_disk(self):
        make_cert(self.base / "server/state/tls", "panel.example.com")
        self.assertTrue(admin_tls.available(str(self.base)))
        self.assertIsInstance(admin_tls.build_context(str(self.base)), ssl.SSLContext)

    def test_a_broken_pair_degrades_to_plain_http(self):
        """Refusing to boot would take the panel down over a bad file;
        serving plain HTTP is recoverable and says so."""
        d = self.base / "server/state/tls"
        d.mkdir(parents=True)
        (d / "fullchain.pem").write_text("not a certificate")
        (d / "privkey.pem").write_text("not a key")
        said = []
        self.assertIsNone(admin_tls.build_context(str(self.base), log=said.append))
        self.assertTrue(any("unusable" in m for m in said))

    def test_key_that_does_not_match_the_certificate(self):
        d = self.base / "server/state/tls"
        make_cert(d, "panel.example.com")
        make_cert(self.base / "other", "other.example.com")
        shutil.copy(self.base / "other/privkey.pem", d / "privkey.pem")
        said = []
        self.assertIsNone(admin_tls.build_context(str(self.base), log=said.append))
        self.assertTrue(any("unusable" in m for m in said))

    def test_describe_names_the_certificate(self):
        make_cert(self.base / "server/state/tls", "panel.example.com")
        described = admin_tls.describe(str(self.base))
        self.assertIn("panel.example.com", described)
        self.assertIn("expires", described)


class Renewal(unittest.TestCase):
    """The load-bearing case: a renewal must take effect without anyone
    restarting the panel."""

    def wait_for(self, said, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not said:
            time.sleep(0.05)
        self.assertTrue(said, "the watcher never noticed the change")

    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        for k in ("DUNE_ADMIN_UI_TLS_CERT", "DUNE_ADMIN_UI_TLS_KEY"):
            os.environ.pop(k, None)

    def test_a_renewed_certificate_is_picked_up(self):
        d = self.base / "server/state/tls"
        make_cert(d, "old.example.com")
        ctx = admin_tls.build_context(str(self.base))
        self.assertIsNotNone(ctx)
        said = []
        # Production order: serve, start watching, and only then a renewal
        # lands from outside — certbot, the ACME client, an operator's copy.
        admin_tls.watch(str(self.base), ctx, log=said.append, poll=0.05)
        time.sleep(0.15)
        make_cert(d, "new.example.com")
        self.wait_for(said)
        self.assertIn("reloaded", said[0])

    def test_a_half_written_renewal_keeps_the_old_certificate(self):
        """certbot writes the two files separately; catching the panel
        mid-write must not take TLS down."""
        d = self.base / "server/state/tls"
        make_cert(d, "good.example.com")
        ctx = admin_tls.build_context(str(self.base))
        said = []
        admin_tls.watch(str(self.base), ctx, log=said.append, poll=0.05)
        time.sleep(0.15)
        (d / "fullchain.pem").write_text("-----BEGIN CERTIFICATE-----\ntruncated\n")
        self.wait_for(said)
        self.assertIn("keeping the previous one", said[0])


if __name__ == "__main__":
    unittest.main()
