#!/usr/bin/env python3
"""Session-cookie behaviour per hosting topology, end to end.

The bug this guards against was invisible to unit tests and to the
operator alike: with DUNE_ADMIN_UI_DOMAIN set and no TLS in front,
/api/login answered 200 while handing the browser a `Secure` cookie it
discards on arrival. The SPA authenticates purely by that cookie
(web/src/api.ts uses credentials:"include" and deletes the legacy
localStorage token), so the login screen simply reappeared — no error
server-side, none client-side, and no working URL to fall back on since
nothing serves https.

Catching that needs the real process, a real login, and an inspection of
the Set-Cookie attributes a browser would act on, which is what this
does. Each case boots scripts/admin-http.py on an ephemeral port.

Run: python3 scripts/test_admin_http_topologies.py   (~15s)
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
PASSWORD = "topology-test-password"


class Topology(unittest.TestCase):
    port = 8990

    @classmethod
    def setUpClass(cls) -> None:
        # admin_progression loads catalogues from $BASE/data/admin; point at
        # the shipped data and keep generated state inside the tmpdir.
        cls.base = pathlib.Path(tempfile.mkdtemp())
        (cls.base / "data").symlink_to(_HERE.parent / "data")
        (cls.base / "scripts").symlink_to(_HERE)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.base, ignore_errors=True)

    def boot(self, **env_extra):
        """Start admin-http.py; returns (port, log_lines). Stopped on cleanup."""
        Topology.port += 1
        port = Topology.port
        env = {
            **os.environ,
            "DUNE_BASE_DIR": str(self.base),
            "DUNE_ADMIN_HTTP_ADDR": "0.0.0.0",
            "DUNE_ADMIN_HTTP_PORT": str(port),
            "DUNE_ADMIN_UI_ENABLED": "1",
            "DUNE_ADMIN_UI_PASSWORD": PASSWORD,
            "DUNE_ADMIN_UI_SESSION_SECRET": "0" * 64,
            **{k: v for k, v in env_extra.items() if v is not None},
        }
        proc = subprocess.Popen(
            [sys.executable, str(_HERE / "admin-http.py")], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        lines: list[str] = []
        if proc.stdout is not None:
            threading.Thread(target=lambda: [lines.append(x) for x in proc.stdout],
                             daemon=True).start()
        self.addCleanup(self._stop, proc)
        for _ in range(60):
            time.sleep(0.1)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
                return port, lines
            except (urllib.error.URLError, OSError):
                continue
        self.fail("admin-http did not start: " + "".join(lines)[:400])

    @staticmethod
    def _stop(proc) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()

    def login(self, port: int, headers: dict | None = None) -> list[str]:
        """Perform a real login; returns the Set-Cookie headers."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login", method="POST",
            data=json.dumps({"password": PASSWORD}).encode(),
            headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            return resp.headers.get_all("Set-Cookie") or []

    def assertSecure(self, cookies, expected: bool) -> None:
        session = [c for c in cookies if c.startswith("dune_session=")]
        self.assertTrue(session, "no session cookie issued")
        self.assertEqual(
            "Secure" in session[0], expected,
            f"Set-Cookie was {session[0]!r}; a browser on http:// "
            f"{'discards' if not expected else 'keeps'} this",
        )

    @staticmethod
    def waited(lines, needle, seconds=2.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if any(needle in x for x in lines):
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    def test_direct_hosting_without_a_domain(self) -> None:
        port, lines = self.boot()
        self.assertSecure(self.login(port), False)
        self.assertFalse(self.waited(lines, "WILL DISCARD", 0.5))

    def test_direct_hosting_with_a_domain_is_flagged_loudly(self) -> None:
        """The historical trap. `auto` cannot tell this apart from a
        proxied deployment, so it still errs toward Secure — but the
        operator now hears about it instead of bouncing off a login
        screen with no explanation anywhere."""
        port, lines = self.boot(DUNE_ADMIN_UI_DOMAIN="dune.example.com")
        self.assertSecure(self.login(port), True)
        self.assertTrue(self.waited(lines, "WILL DISCARD"),
                        "the one place this failure can be reported stayed silent")

    def test_the_escape_hatch_fixes_that_topology(self) -> None:
        port, lines = self.boot(DUNE_ADMIN_UI_DOMAIN="dune.example.com",
                                DUNE_ADMIN_UI_TLS="off")
        self.assertSecure(self.login(port), False)
        self.assertFalse(self.waited(lines, "WILL DISCARD", 0.5))

    def test_declared_proxy_reporting_https_self_configures(self) -> None:
        port, lines = self.boot(DUNE_ADMIN_UI_DOMAIN="dune.example.com",
                                DUNE_ADMIN_UI_TRUSTED_PROXIES="127.0.0.1")
        cookies = self.login(port, {"X-Forwarded-Proto": "https",
                                    "X-Forwarded-For": "203.0.113.9"})
        self.assertSecure(cookies, True)
        self.assertFalse(self.waited(lines, "WILL DISCARD", 0.5))

    def test_declared_proxy_reporting_http(self) -> None:
        port, _ = self.boot(DUNE_ADMIN_UI_DOMAIN="dune.example.com",
                            DUNE_ADMIN_UI_TRUSTED_PROXIES="127.0.0.1")
        cookies = self.login(port, {"X-Forwarded-Proto": "http",
                                    "X-Forwarded-For": "203.0.113.9"})
        self.assertSecure(cookies, False)

    def test_undeclared_proxy_keeps_working_and_gets_advice_not_an_alarm(self) -> None:
        """A working TLS deployment that simply hasn't declared its proxy
        must not be told its login is broken — that is how warnings get
        ignored. It gets the actionable hint instead."""
        port, lines = self.boot(DUNE_ADMIN_UI_DOMAIN="dune.example.com")
        cookies = self.login(port, {"X-Forwarded-Proto": "https",
                                    "X-Forwarded-For": "203.0.113.9"})
        self.assertSecure(cookies, True)
        self.assertFalse(self.waited(lines, "WILL DISCARD", 0.5))
        self.assertTrue(self.waited(lines, "no declared proxy vouched"))

    def test_logout_clears_what_login_set(self) -> None:
        """Mismatched attributes leave the original cookie in place."""
        port, _ = self.boot(DUNE_ADMIN_UI_DOMAIN="dune.example.com",
                            DUNE_ADMIN_UI_TLS="off")
        set_cookies = self.login(port)
        token = [c.split("=", 1)[1].split(";", 1)[0]
                 for c in set_cookies if c.startswith("dune_session=")][0]
        csrf = [c.split("=", 1)[1].split(";", 1)[0]
                for c in set_cookies if c.startswith("dune_csrf=")][0]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/logout", method="POST", data=b"",
            headers={"Cookie": f"dune_session={token}; dune_csrf={csrf}",
                     "X-CSRF-Token": csrf})
        with urllib.request.urlopen(req, timeout=5) as resp:
            cleared = resp.headers.get_all("Set-Cookie") or []
        self.assertEqual(
            sorted("Secure" in c for c in set_cookies),
            sorted("Secure" in c for c in cleared),
        )


if __name__ == "__main__":
    unittest.main()
