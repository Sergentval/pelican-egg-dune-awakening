#!/usr/bin/env python3
"""The command audit must outlive the process, end to end.

The panel restarts with the server — scheduler auto-restart, `panel
restart`, reinstall — so this is exercised by actually stopping
admin-http.py and starting it again against the same state directory,
which a unit test on the store alone cannot demonstrate.

Run: python3 scripts/test_admin_http_audit.py   (~10s)
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
PASSWORD = "audit-test-password"


class Audit(unittest.TestCase):
    port = 8890

    def setUp(self) -> None:
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        (self.base / "data").symlink_to(_HERE.parent / "data")
        (self.base / "scripts").symlink_to(_HERE)
        Audit.port += 1
        self.port = Audit.port

    def boot(self):
        env = {
            **os.environ,
            "DUNE_BASE_DIR": str(self.base),
            "DUNE_ADMIN_HTTP_ADDR": "127.0.0.1",
            "DUNE_ADMIN_HTTP_PORT": str(self.port),
            "DUNE_ADMIN_UI_ENABLED": "1",
            "DUNE_ADMIN_UI_PASSWORD": PASSWORD,
            "DUNE_ADMIN_UI_SESSION_SECRET": "0" * 64,
        }
        proc = subprocess.Popen([sys.executable, str(_HERE / "admin-http.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, proc)
        for _ in range(60):
            time.sleep(0.1)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1).read()
                return proc
            except (urllib.error.URLError, OSError):
                continue
        self.fail("admin-http did not start")

    @staticmethod
    def _stop(proc) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def token(self) -> str:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/login", method="POST",
            data=json.dumps({"password": PASSWORD}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)["token"]

    def history(self, tok: str, limit: int = 200) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/history?limit={limit}",
                                     headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def test_boot_is_recorded_and_survives_a_restart(self) -> None:
        proc = self.boot()
        first = self.history(self.token())
        self.assertEqual(first["total"], 1)
        self.assertEqual(first["entries"][0]["argv"], ["(admin panel started)"])

        # Restart the process against the same state directory.
        self._stop(proc)
        self.boot()
        after = self.history(self.token())

        self.assertEqual(after["total"], 2, "the first boot's entry did not survive")
        self.assertEqual([e["argv"][0] for e in after["entries"]],
                         ["(admin panel started)", "(admin panel started)"])

    def test_the_trail_is_readable_before_any_command_runs(self) -> None:
        self.boot()
        payload = self.history(self.token())
        self.assertIn("entries", payload)
        self.assertIn("total", payload)

    def test_limit_is_clamped_and_junk_is_tolerated(self) -> None:
        self.boot()
        tok = self.token()
        for raw in ("0", "-5", "99999", "abc", ""):
            with self.subTest(limit=raw):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/api/history?limit={raw}",
                    headers={"Authorization": f"Bearer {tok}"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(r.status, 200)

    def test_history_still_requires_auth(self) -> None:
        self.boot()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/history")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertIn(caught.exception.code, (401, 403))


if __name__ == "__main__":
    unittest.main()
