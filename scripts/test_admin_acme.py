#!/usr/bin/env python3
"""Tests for the ACME client and its DNS backends.

The parts that must be right and cannot be checked by running it once: the
JWK thumbprint and the dns-01 TXT value. Both are verified against the
worked example in RFC 8555 / RFC 7638 rather than against this code's own
output, because a wrong TXT does not fail loudly — the CA simply refuses to
validate, and Let's Encrypt rate-limits failures.

Run: python3 scripts/test_admin_acme.py
"""
import base64
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import admin_acme
import admin_acme_dns


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class ChallengeMaths(unittest.TestCase):
    """RFC 7638 §3.1 gives a JWK and its thumbprint; RFC 8555 §8.4 defines
    the dns-01 TXT as the base64url SHA-256 of the key authorization."""

    # The RSA key from RFC 7638 §3.1, whose thumbprint the RFC states.
    RFC7638_JWK = {
        "e": "AQAB",
        "kty": "RSA",
        # Transcribed from RFC 7638 section 3.1 and checked against the
        # thumbprint the RFC states — a truncated modulus makes this test
        # pass or fail for the wrong reason, which is how it first failed.
        "n": ("0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAt"
              "VT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn6"
              "4tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FD"
              "W2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n9"
              "1CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINH"
              "aQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"),
    }
    RFC7638_THUMBPRINT = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"

    def test_thumbprint_is_the_rfc_algorithm(self):
        """The exact serialisation matters — sorted keys, no whitespace.
        Get it wrong and every challenge silently fails to validate."""
        canonical = json.dumps(self.RFC7638_JWK, sort_keys=True, separators=(",", ":"))
        self.assertEqual(b64(hashlib.sha256(canonical.encode()).digest()),
                         self.RFC7638_THUMBPRINT)

    def test_txt_value_is_the_digest_not_the_key_authorization(self):
        token, thumb = "evaGxfADs6pSRb2LAv9IZf17Dt3juxGJ-PCt92wr-oA", self.RFC7638_THUMBPRINT
        expected = b64(hashlib.sha256(f"{token}.{thumb}".encode()).digest())
        self.assertEqual(admin_acme.txt_value(token, thumb), expected)
        # The classic mistake: publishing the key authorization itself.
        self.assertNotEqual(admin_acme.txt_value(token, thumb), f"{token}.{thumb}")

    def test_txt_value_is_43_chars_of_base64url(self):
        value = admin_acme.txt_value("tok", "thumb")
        self.assertEqual(len(value), 43)
        self.assertNotIn("=", value)
        self.assertNotIn("+", value)
        self.assertNotIn("/", value)


class KeysAndCsr(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_account_key_is_private_and_reused(self):
        path = str(self.dir / "sub" / "account.key")
        admin_acme.generate_account_key(path)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        first = pathlib.Path(path).read_bytes()
        admin_acme.generate_account_key(path)
        self.assertEqual(first, pathlib.Path(path).read_bytes(),
                         "regenerating would orphan the registered ACME account")

    def test_thumbprint_round_trips_through_openssl(self):
        path = str(self.dir / "account.key")
        admin_acme.generate_account_key(path)
        jwk, thumb = admin_acme.account_thumbprint(path)
        self.assertEqual(jwk["kty"], "RSA")
        canonical = json.dumps(jwk, sort_keys=True, separators=(",", ":"))
        self.assertEqual(b64(hashlib.sha256(canonical.encode()).digest()), thumb)

    def test_a_non_rsa_key_is_refused_clearly(self):
        path = self.dir / "ec.key"
        subprocess.run(["openssl", "ecparam", "-genkey", "-name", "prime256v1",
                        "-out", str(path)], check=True, capture_output=True)
        with self.assertRaises(admin_acme.AcmeError) as caught:
            admin_acme.account_thumbprint(str(path))
        self.assertIn("RSA", str(caught.exception))

    def test_csr_carries_the_domain_in_subject_and_san(self):
        key, csr = str(self.dir / "srv.key"), str(self.dir / "srv.csr")
        admin_acme.generate_csr(key, "panel.example.com", csr)
        self.assertEqual(oct(os.stat(key).st_mode & 0o777), "0o600")
        out = subprocess.run(["openssl", "req", "-in", csr, "-noout", "-text"],
                             capture_output=True, text=True).stdout
        self.assertIn("panel.example.com", out)
        self.assertIn("DNS:panel.example.com", out)

    def test_days_remaining(self):
        key, cert = self.dir / "k.pem", self.dir / "c.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "30", "-keyout", str(key), "-out", str(cert),
                        "-subj", "/CN=x"], check=True, capture_output=True)
        self.assertIn(admin_acme.days_remaining(str(cert)), (29, 30))

    def test_days_remaining_is_none_when_unreadable(self):
        # None must mean "needs attention", never be mistaken for "fine".
        self.assertIsNone(admin_acme.days_remaining(str(self.dir / "absent.pem")))


class BackendSelection(unittest.TestCase):
    ENV = ("DUNE_ACME_DNS_BACKEND", "DUNE_ACME_CLOUDFLARE_TOKEN",
           "DUNE_ACME_CLOUDFLARE_ZONE", "DUNE_ACME_DNS_URL")

    def setUp(self):
        saved = {k: os.environ.get(k) for k in self.ENV}

        def restore():
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)
        for k in self.ENV:
            os.environ.pop(k, None)

    def test_off_by_default(self):
        self.assertIsNone(admin_acme_dns.from_env("/base"))

    def test_cloudflare_needs_a_token(self):
        os.environ["DUNE_ACME_DNS_BACKEND"] = "cloudflare"
        with self.assertRaises(admin_acme_dns.PublishError):
            admin_acme_dns.from_env("/base")

    def test_cloudflare_selected(self):
        os.environ.update({"DUNE_ACME_DNS_BACKEND": "cloudflare",
                           "DUNE_ACME_CLOUDFLARE_TOKEN": "tok"})
        self.assertIsInstance(admin_acme_dns.from_env("/base"),
                              admin_acme_dns.CloudflarePublisher)

    def test_acme_dns_defaults_to_the_public_instance(self):
        os.environ["DUNE_ACME_DNS_BACKEND"] = "acme-dns"
        pub = admin_acme_dns.from_env("/base")
        self.assertIsInstance(pub, admin_acme_dns.AcmeDnsPublisher)
        self.assertEqual(pub.url, admin_acme_dns.ACME_DNS_PUBLIC)

    def test_acme_dns_self_hosted(self):
        os.environ.update({"DUNE_ACME_DNS_BACKEND": "acme-dns",
                           "DUNE_ACME_DNS_URL": "https://acme.internal/"})
        self.assertEqual(admin_acme_dns.from_env("/base").url, "https://acme.internal")

    def test_an_unknown_backend_names_the_valid_ones(self):
        os.environ["DUNE_ACME_DNS_BACKEND"] = "route53"
        with self.assertRaises(admin_acme_dns.PublishError) as caught:
            admin_acme_dns.from_env("/base")
        self.assertIn("cloudflare", str(caught.exception))
        self.assertIn("acme-dns", str(caught.exception))


class SecretsNeverLeak(unittest.TestCase):
    """These messages reach logs/admin-http.log, which the panel renders."""

    def test_redaction(self):
        self.assertNotIn("s3cr3t", admin_acme_dns._redact("Bearer s3cr3t failed", "s3cr3t"))

    def test_http_errors_are_redacted(self):
        pub = admin_acme_dns.CloudflarePublisher("tok-abcdef", "example.com")
        # Unroutable by RFC 5737, so this fails at connect without a token
        # ever being sent anywhere.
        pub.API = "https://192.0.2.1"
        with self.assertRaises(admin_acme_dns.PublishError) as caught:
            pub.publish("_acme-challenge.example.com", "value")
        self.assertNotIn("tok-abcdef", str(caught.exception))

    def test_acme_error_message_carries_no_key_path_contents(self):
        err = admin_acme.AcmeError("validation failed")
        self.assertNotIn("BEGIN", str(err))


class Propagation(unittest.TestCase):
    def test_wait_gives_up_rather_than_hanging(self):
        class Never(admin_acme_dns._Base):
            pass
        said = []
        ok = Never().wait_until_visible("_acme-challenge.example.invalid", "x",
                                        timeout=1, log=said.append)
        self.assertFalse(ok, "must return False so the caller reports a useful error")

    def test_a_real_txt_lookup_works(self):
        # Proves the DoH path parses a real answer — Google publishes SPF.
        self.assertTrue(admin_acme_dns.txt_visible("google.com", "v=spf1"))

    def test_absent_record_is_not_visible(self):
        self.assertFalse(admin_acme_dns.txt_visible(
            "_acme-challenge.nonexistent-zzz.example.invalid", "anything"))


if __name__ == "__main__":
    unittest.main()
