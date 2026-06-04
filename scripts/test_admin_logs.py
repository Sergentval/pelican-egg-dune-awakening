"""Unit tests for admin_logs — the Logs-tab path-safety + secret redaction.

These are a security boundary (logs go to a browser), so the traversal-rejection
and redaction cases are exhaustive rather than illustrative.
"""
import os
import tempfile
import unittest

import admin_logs


class TestValidLogSource(unittest.TestCase):
    def test_fixed_services_ok(self):
        for n in ("admin-http", "postgres", "market-bot", "mq-admin"):
            self.assertTrue(admin_logs.valid_log_source(n))

    def test_ue5_shape_ok(self):
        for n in ("ue5-Survival_1", "ue5-SH_Arrakeen", "ue5-DeepDesert_1"):
            self.assertTrue(admin_logs.valid_log_source(n))

    def test_rejects_junk(self):
        for n in ("", "nope", "../etc/passwd", "ue5-../x", "ue5-x/y",
                  "admin-http/../x", "/etc/passwd", "ue5-x;rm", "admin-http.log"):
            self.assertFalse(admin_logs.valid_log_source(n))


class TestValidRestartable(unittest.TestCase):
    def test_allowed(self):
        for n in ("admin-http", "scheduler", "market-bot", "mock-k8s", "director"):
            self.assertTrue(admin_logs.valid_restartable_service(n))

    def test_refused(self):
        # postgres + brokers cascade; ue5 has the grace risk -> never restartable.
        for n in ("postgres", "mq-admin", "mq-game", "ue5-Survival_1", "", "nope"):
            self.assertFalse(admin_logs.valid_restartable_service(n))


class TestResolveLogPath(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_valid_maps_under_logs(self):
        p = admin_logs.resolve_log_path(self.dir, "admin-http")
        self.assertIsNotNone(p)
        self.assertEqual(os.path.basename(str(p)), "admin-http.log")
        self.assertTrue(str(p).startswith(os.path.realpath(self.dir)))

    def test_traversal_rejected(self):
        for n in ("../../etc/passwd", "..", "/etc/passwd", "ue5-../x",
                  "admin-http/../../x", "nope"):
            self.assertIsNone(admin_logs.resolve_log_path(self.dir, n))

    def test_ue5_instance_resolves(self):
        self.assertIsNotNone(admin_logs.resolve_log_path(self.dir, "ue5-SH_Arrakeen"))


class TestTailFile(unittest.TestCase):
    def test_missing_file(self):
        lines, exists = admin_logs.tail_file("/no/such/file.log", 10)
        self.assertEqual((lines, exists), ([], False))

    def test_tail_last_n(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("\n".join(f"line{i}" for i in range(1000)) + "\n")
            path = f.name
        try:
            lines, exists = admin_logs.tail_file(path, 5)
            self.assertTrue(exists)
            self.assertEqual(lines, ["line995", "line996", "line997", "line998", "line999"])
        finally:
            os.unlink(path)

    def test_clamped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("a\nb\nc\n")
            path = f.name
        try:
            lines, _ = admin_logs.tail_file(path, 10_000)  # > LOG_TAIL_MAX, still ok
            self.assertEqual(lines, ["a", "b", "c"])
        finally:
            os.unlink(path)


class TestRedact(unittest.TestCase):
    def _redacted(self, line, *, must_not_contain):
        out = admin_logs.redact(line)
        for s in must_not_contain:
            self.assertNotIn(s, out, f"{s!r} leaked in: {out!r}")
        return out

    def test_password_kv(self):
        self._redacted("DUNE_ADMIN_UI_PASSWORD=Master117", must_not_contain=["Master117"])
        self._redacted("password: hunter2trustno1", must_not_contain=["hunter2trustno1"])

    def test_session_secret(self):
        self._redacted("DUNE_ADMIN_UI_SESSION_SECRET=deadbeef" * 1, must_not_contain=["deadbeef"])

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w"
        self._redacted(f"ServiceAuthToken={jwt}", must_not_contain=[jwt.split('.')[2]])

    def test_pelican_key(self):
        self._redacted("calling pelican with pacc_abcd1234efgh5678 header",
                       must_not_contain=["pacc_abcd1234efgh5678"])

    def test_bearer_header(self):
        self._redacted("Authorization: Bearer abc123tok", must_not_contain=["abc123tok"])

    def test_db_password_literal(self):
        self._redacted("-DatabasePassword=dune -other=1", must_not_contain=["=dune "])

    def test_amqp_conn_string(self):
        self._redacted("amqp://guest:supersecret@rabbit:5672", must_not_contain=["supersecret"])

    def test_long_base64_blob(self):
        blob = "A" * 90 + "=="
        self._redacted(f"RMQ_SECRET set to {blob}", must_not_contain=[blob])

    def test_keeps_normal_lines(self):
        line = "[admin-http] [INFO] GET /api/status 200 in 4ms"
        self.assertEqual(admin_logs.redact(line), line)


if __name__ == "__main__":
    unittest.main()
