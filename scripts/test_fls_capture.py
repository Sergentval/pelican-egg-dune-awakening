#!/usr/bin/env python3
"""Tests for fls_capture: the redaction applied before an FLS body is logged."""
import base64
import json
import unittest

import fls_capture as cap


def jwt(claims):
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.c2lnbmF0dXJl"


class TestSanitize(unittest.TestCase):
    def test_identifiers_are_kept(self):
        body = {"PlayerMasterAccountId": "9f0a5b38253681847e735b208be73765",
                "BattlegroupId": "sh-de0bccaa2501bf22-jrqtjf", "FlsId": "DE0BCCAA2501BF22"}
        self.assertEqual(cap.sanitize(body), body)

    def test_secret_named_fields_are_redacted(self):
        out = cap.sanitize({"FlsServerToken": "abc", "ServiceAuthKey": "k",
                            "password": "p", "SessionTicket": "t", "nested": {"ApiSecret": "s"}})
        for key in ("FlsServerToken", "ServiceAuthKey", "password", "SessionTicket"):
            self.assertTrue(str(out[key]).startswith("<redacted"), key)
        self.assertTrue(out["nested"]["ApiSecret"].startswith("<redacted"))
        self.assertNotIn("abc", json.dumps(out))

    # A JWT's signature and any secret claim must never reach the log; its
    # identity claims are exactly what the capture is for.
    def test_jwt_is_replaced_by_its_sanitized_claims(self):
        token = jwt({"HostId": "DE0BCCAA2501BF22", "ServiceAuthKey": "1MRMe0i23gg", "exp": 1})
        out = cap.sanitize({"FlsServerToken": token, "free": token})
        dumped = json.dumps(out)
        self.assertNotIn("1MRMe0i23gg", dumped)
        self.assertNotIn("c2lnbmF0dXJl", dumped)
        self.assertEqual(out["FlsServerToken"]["jwt_claims"]["HostId"], "DE0BCCAA2501BF22")
        self.assertEqual(out["free"]["jwt_claims"]["HostId"], "DE0BCCAA2501BF22")

    def test_lists_and_scalars(self):
        self.assertEqual(cap.sanitize([1, "x", None, True]), [1, "x", None, True])

    def test_body_text_non_json_is_summarised_not_dumped(self):
        self.assertEqual(cap.sanitize_body(b"not json"), {"non_json_bytes": 8})
        self.assertEqual(cap.sanitize_body(b""), {})
        self.assertEqual(cap.sanitize_body(json.dumps({"a": 1}).encode()), {"a": 1})

    def test_query_string_code_is_never_logged(self):
        self.assertEqual(cap.safe_path("/api/Battlegroups_IsPlayerAuthorized?code=eyJ.secret"),
                         "/api/Battlegroups_IsPlayerAuthorized")


if __name__ == "__main__":
    unittest.main()
