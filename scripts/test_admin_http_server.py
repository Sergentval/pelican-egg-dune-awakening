#!/usr/bin/env python3
"""Regression tests for the admin-http listener itself (issue #89).

Two defects took a publicly-bound panel down after ~a day:

  1. The listener was a single-threaded `HTTPServer` and the handler had
     no socket timeout, so ONE connection that never finished sending a
     request line blocked `serve_forever()` permanently. A 0.0.0.0:8090
     bind is found by internet scanners within hours.
  2. `log_message` dereferenced `self.command` / `self.path`, which do
     not exist yet when `parse_request()` rejects a malformed request
     line. The AttributeError escaped through `send_error()`, so the
     client got a bare connection close instead of a 400 and every scan
     hit dumped a traceback into admin-http.log.

These tests drive the real Handler over a real loopback socket — the
failure mode only exists at the socket layer, so a mocked request object
would not have caught it.

Run: python3 scripts/test_admin_http_server.py
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("admin_http", _HERE / "admin-http.py")
assert _spec is not None and _spec.loader is not None
admin_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin_http)

HEALTHZ = b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"


def speak(port: int, payload: bytes, timeout: float = 3.0) -> bytes:
    """Send `payload`, return the first response chunk. b"" means the
    server closed without answering, b"<TIMEOUT>" means it never did."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(payload)
        try:
            return sock.recv(4096)
        except (socket.timeout, TimeoutError):
            return b"<TIMEOUT>"
    finally:
        sock.close()


class ServerCase(unittest.TestCase):
    """Boots the production listener on an ephemeral loopback port."""

    max_connections: int | None = None

    def setUp(self) -> None:
        kwargs = {}
        if self.max_connections is not None:
            kwargs["max_connections"] = self.max_connections
        self.srv = admin_http.build_server(("127.0.0.1", 0), **kwargs)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def assertStatus(self, response: bytes, code: int) -> None:
        self.assertTrue(
            response.startswith(b"HTTP/1."),
            f"expected an HTTP status line, got {response[:80]!r}",
        )
        self.assertIn(f" {code} ".encode(), response.split(b"\r\n", 1)[0])


class MalformedRequests(ServerCase):
    """Junk on the wire must get an error response, not an AttributeError.

    The error reply itself is body-only: parse_request() leaves
    request_version at the HTTP/0.9 default when it cannot read a version,
    and the stdlib then omits the status line by design. What issue #89
    was about is that send_error() never got that far.
    """

    def setUp(self) -> None:
        super().setUp()
        # socketserver.handle_error() prints the traceback here.
        self.stderr = io.StringIO()
        original = sys.stderr
        sys.stderr = self.stderr
        self.addCleanup(setattr, sys, "stderr", original)

    def assertErrored(self, response: bytes, code: int) -> None:
        self.assertIn(f"Error code: {code}".encode(), response)
        self.assertNotIn("Traceback", self.stderr.getvalue())

    def test_healthz_baseline(self) -> None:
        self.assertStatus(speak(self.port, HEALTHZ), 200)

    def test_bad_request_version(self) -> None:
        self.assertErrored(speak(self.port, b"GET /healthz HTTP/BLAH\r\n\r\n"), 400)

    def test_bad_request_syntax(self) -> None:
        self.assertErrored(speak(self.port, b"GET\r\n\r\n"), 400)

    def test_tls_client_hello_on_the_plaintext_port(self) -> None:
        # Exactly what a scanner probing for HTTPS sends. No spaces, so
        # parse_request() bails before self.path is ever assigned.
        hello = b"\x16\x03\x01\x00\xa5\x01\x00\x00\xa1\x03\x03abcdef\r\n\r\n"
        self.assertErrored(speak(self.port, hello), 400)

    def test_unsupported_http_version(self) -> None:
        self.assertErrored(speak(self.port, b"GET /healthz HTTP/2.0\r\n\r\n"), 505)

    def test_server_still_serves_after_a_malformed_request(self) -> None:
        speak(self.port, b"GET /healthz HTTP/BLAH\r\n\r\n")
        self.assertStatus(speak(self.port, HEALTHZ), 200)
        self.assertNotIn("Traceback", self.stderr.getvalue())


class StalledConnections(ServerCase):
    """The issue #89 outage: one silent client wedged the whole panel."""

    def test_idle_connection_does_not_block_other_clients(self) -> None:
        stalled = socket.create_connection(("127.0.0.1", self.port), timeout=3.0)
        self.addCleanup(stalled.close)
        # Nothing sent — the server is now blocked in readline() for this
        # peer if it is single-threaded.
        self.assertStatus(speak(self.port, HEALTHZ, timeout=3.0), 200)

    def test_slow_header_dribble_does_not_block_other_clients(self) -> None:
        slow = socket.create_connection(("127.0.0.1", self.port), timeout=3.0)
        self.addCleanup(slow.close)
        slow.sendall(b"GET /healthz HTTP/1.1\r\n")  # headers never terminated
        self.assertStatus(speak(self.port, HEALTHZ, timeout=3.0), 200)

    def test_handler_declares_a_connection_timeout(self) -> None:
        timeout = admin_http.Handler.timeout
        self.assertIsInstance(timeout, (int, float))
        self.assertGreater(timeout, 0)

    def test_idle_connection_is_reaped(self) -> None:
        original = admin_http.Handler.timeout
        admin_http.Handler.timeout = 1
        self.addCleanup(setattr, admin_http.Handler, "timeout", original)
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10.0)
        self.addCleanup(sock.close)
        started = time.monotonic()
        self.assertEqual(sock.recv(4096), b"", "server should have closed the idle peer")
        self.assertLess(time.monotonic() - started, 8.0)


class ConnectionCap(ServerCase):
    """A scanner storm must not spawn unbounded threads."""

    max_connections = 2

    def test_excess_connections_are_refused_not_queued_forever(self) -> None:
        for _ in range(ConnectionCap.max_connections or 0):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=3.0)
            self.addCleanup(sock.close)
        time.sleep(0.2)  # let the accept loop consume both slots
        self.assertEqual(speak(self.port, HEALTHZ, timeout=3.0), b"")

    def test_slots_are_returned_when_connections_close(self) -> None:
        for _ in range((ConnectionCap.max_connections or 0) * 3):
            self.assertStatus(speak(self.port, HEALTHZ, timeout=3.0), 200)


class PeerDisconnects(unittest.TestCase):
    """A client vanishing mid-response is routine, not a stack trace.
    Driven directly: whether a hangup beats the write to the socket is a
    race, and the branch under test is the same either way."""

    def _handle(self, exc: BaseException) -> tuple[str, str]:
        srv = admin_http.BoundedThreadingHTTPServer.__new__(
            admin_http.BoundedThreadingHTTPServer
        )
        out, err = io.StringIO(), io.StringIO()
        try:
            raise exc
        except BaseException:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                srv.handle_error(None, ("203.0.113.1", 4242))
        return out.getvalue(), err.getvalue()

    def test_hangup_is_a_single_log_line(self) -> None:
        for exc in (BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset")):
            with self.subTest(exc=type(exc).__name__):
                out, err = self._handle(exc)
                self.assertIn("203.0.113.1", out)
                self.assertIn("dropped", out)
                self.assertNotIn("Traceback", err)

    def test_a_real_bug_keeps_its_traceback(self) -> None:
        _, err = self._handle(ValueError("a genuine defect"))
        self.assertIn("Traceback", err)
        self.assertIn("a genuine defect", err)


class LogMessageResilience(unittest.TestCase):
    """log_message runs inside send_error — it may never raise."""

    def _stub(self):
        # __new__ with no __init__: no socket, and — like a handler whose
        # request line failed to parse — no .command / .path attributes.
        handler = admin_http.Handler.__new__(admin_http.Handler)
        handler.client_address = ("198.51.100.9", 1234)
        return handler

    def _capture(self, handler, *args) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            handler.log_message(*args)
        return buf.getvalue()

    def test_survives_missing_command_and_path(self) -> None:
        out = self._capture(self._stub(), "code %d, message %s", 400, "Bad request version")
        self.assertIn("400", out)

    def test_logs_the_peer_address(self) -> None:
        out = self._capture(self._stub(), "code %d, message %s", 400, "Bad request version")
        self.assertIn("198.51.100.9", out)

    def test_survives_a_missing_client_address(self) -> None:
        handler = admin_http.Handler.__new__(admin_http.Handler)
        self.assertIn("400", self._capture(handler, "code %d, message %s", 400, "x"))

    def test_reports_command_and_path_when_they_exist(self) -> None:
        handler = self._stub()
        handler.command = "GET"
        handler.path = "/api/status"
        out = self._capture(handler, "code %d, message %s", 200, "OK")
        self.assertIn("GET", out)
        self.assertIn("/api/status", out)

    def test_strips_control_characters_from_the_request_line(self) -> None:
        handler = self._stub()
        handler.command = "GET"
        handler.path = "/\x1b[2Jwiped\x00 \r\n[admin-http] [INFO] forged line"
        out = self._capture(handler, "code %d, message %s", 400, "Bad request")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x00", out)
        self.assertEqual(out.count("\n"), 1, "one log line, no injected extras")

    def test_keeps_printable_unicode(self) -> None:
        handler = self._stub()
        handler.command = "GET"
        handler.path = "/api/players/Tähti"
        self.assertIn("Tähti", self._capture(handler, "code %d, message %s", 200, "OK"))

    def test_survives_a_format_string_that_does_not_match_its_args(self) -> None:
        # http.server passes operator-visible strings through; a stray %
        # must not turn into a 500-by-exception on the error path.
        self.assertTrue(self._capture(self._stub(), "100% complete"))


class ClientIPResolution(unittest.TestCase):
    """Which address a request gets bucketed under. Getting this wrong in
    either direction is a security bug: too trusting hands every client a
    fresh rate-limit bucket per request, too strict collapses the whole
    internet into one bucket behind a proxy."""

    def trust(self, raw: str) -> None:
        original = admin_http.TRUSTED_PROXIES
        admin_http.TRUSTED_PROXIES = admin_http._parse_trusted_proxies(raw)
        self.addCleanup(setattr, admin_http, "TRUSTED_PROXIES", original)

    # --- default: nothing declared -------------------------------------
    def test_header_is_ignored_when_no_proxy_is_declared(self) -> None:
        self.trust("")
        self.assertEqual(
            admin_http.resolve_client_ip("10.99.0.1", "1.2.3.4"), "10.99.0.1",
            "an undeclared deployment must not inherit a spoofing surface",
        )

    def test_peer_wins_without_a_header(self) -> None:
        self.trust("10.99.0.1")
        self.assertEqual(admin_http.resolve_client_ip("10.99.0.1", ""), "10.99.0.1")

    # --- the deployment this exists for --------------------------------
    def test_declared_proxy_yields_the_real_client(self) -> None:
        self.trust("10.99.0.1")
        self.assertEqual(admin_http.resolve_client_ip("10.99.0.1", "203.0.113.9"), "203.0.113.9")

    def test_cidr_declaration(self) -> None:
        self.trust("10.99.0.0/24")
        self.assertEqual(admin_http.resolve_client_ip("10.99.0.7", "203.0.113.9"), "203.0.113.9")

    def test_chain_of_declared_proxies_collapses_to_the_client(self) -> None:
        self.trust("10.99.0.1, 172.20.0.5")
        self.assertEqual(
            admin_http.resolve_client_ip("10.99.0.1", "203.0.113.9, 172.20.0.5"),
            "203.0.113.9",
        )

    # --- spoofing attempts ---------------------------------------------
    def test_forged_prefix_cannot_win(self) -> None:
        # The client prepends whatever it likes; only the hop our proxy
        # appended is real, and it is the rightmost untrusted entry.
        self.trust("10.99.0.1")
        self.assertEqual(
            admin_http.resolve_client_ip("10.99.0.1", "1.1.1.1, 8.8.8.8, 203.0.113.9"),
            "203.0.113.9",
        )

    def test_header_from_an_undeclared_peer_is_ignored(self) -> None:
        self.trust("10.99.0.1")
        self.assertEqual(
            admin_http.resolve_client_ip("198.51.100.4", "203.0.113.9"), "198.51.100.4",
            "a direct client must not be able to pick its own bucket",
        )

    def test_malformed_hop_falls_back_to_the_peer(self) -> None:
        self.trust("10.99.0.1")
        for junk in ("not-an-ip", "203.0.113.9, junk", "<script>"):
            with self.subTest(junk=junk):
                self.assertEqual(admin_http.resolve_client_ip("10.99.0.1", junk), "10.99.0.1")

    def test_header_of_only_declared_proxies_falls_back(self) -> None:
        self.trust("10.99.0.1, 172.20.0.5")
        self.assertEqual(
            admin_http.resolve_client_ip("10.99.0.1", "172.20.0.5, 10.99.0.1"), "10.99.0.1"
        )

    def test_empty_peer(self) -> None:
        self.trust("10.99.0.1")
        self.assertEqual(admin_http.resolve_client_ip("", "203.0.113.9"), "unknown")

    # --- parsing --------------------------------------------------------
    def test_ipv6_declaration_and_resolution(self) -> None:
        self.trust("::1")
        self.assertEqual(admin_http.resolve_client_ip("::1", "2001:db8::5"), "2001:db8::5")

    def test_invalid_declarations_are_dropped_not_widened(self) -> None:
        nets = admin_http._parse_trusted_proxies("10.99.0.1, nonsense, 172.20.0.0/16, 999.1.1.1")
        self.assertEqual([str(n) for n in nets], ["10.99.0.1/32", "172.20.0.0/16"])

    def test_whitespace_and_empty_tokens(self) -> None:
        self.assertEqual(admin_http._parse_trusted_proxies("  "), [])
        self.assertEqual(
            [str(n) for n in admin_http._parse_trusted_proxies("10.0.0.1  10.0.0.2,,")],
            ["10.0.0.1/32", "10.0.0.2/32"],
        )


class CookieSecureDecision(unittest.TestCase):
    """Whether session cookies get `Secure`, per hosting topology.

    Wrong in one direction the cookie crosses plain HTTP; wrong in the
    other the browser discards it on arrival and the login silently never
    sticks — with no working URL at all, since nothing serves https.
    """

    def configure(self, *, tls="auto", domain="", listen="0.0.0.0") -> None:
        for name, value in (("UI_TLS", tls), ("UI_DOMAIN", domain), ("LISTEN_ADDR", listen)):
            original = getattr(admin_http, name)
            setattr(admin_http, name, value)
            self.addCleanup(setattr, admin_http, name, original)

    # --- the topology that was broken ----------------------------------
    def test_direct_hosting_with_a_domain_and_no_tls(self) -> None:
        # Operator points a DNS record at an exposed box and fills in the
        # domain. Nothing terminates TLS. Before DUNE_ADMIN_UI_TLS there
        # was no way to express this and the login could not be completed.
        self.configure(tls="off", domain="dune.example.com")
        self.assertFalse(admin_http.cookie_secure())

    def test_that_topology_without_the_escape_hatch_still_breaks(self) -> None:
        # Documents precisely what `auto` cannot know on its own.
        self.configure(tls="auto", domain="dune.example.com")
        self.assertTrue(admin_http.cookie_secure())

    # --- explicit beats inferred ----------------------------------------
    def test_explicit_on_overrides_everything(self) -> None:
        self.configure(tls="on", domain="", listen="127.0.0.1")
        self.assertTrue(admin_http.cookie_secure())
        self.assertTrue(admin_http.cookie_secure("http"))

    def test_explicit_off_overrides_a_proxy_claiming_https(self) -> None:
        self.configure(tls="off", domain="dune.example.com")
        self.assertFalse(admin_http.cookie_secure("https"))

    # --- auto, informed by a declared proxy ------------------------------
    def test_declared_proxy_reporting_https(self) -> None:
        self.configure(tls="auto", domain="", listen="0.0.0.0")
        self.assertTrue(admin_http.cookie_secure("https"),
                        "a trusted proxy saying https must win over an unset domain")

    def test_declared_proxy_reporting_http(self) -> None:
        self.configure(tls="auto", domain="dune.example.com")
        self.assertFalse(admin_http.cookie_secure("http"),
                         "a trusted proxy saying http is better evidence than the domain guess")

    # --- auto, no information: the historical heuristic -------------------
    def test_fallback_matches_previous_behaviour(self) -> None:
        for domain, listen, expected in (
            ("dune.example.com", "0.0.0.0", True),
            ("dune.example.com", "127.0.0.1", False),
            ("", "0.0.0.0", False),
            ("", "127.0.0.1", False),
        ):
            with self.subTest(domain=domain, listen=listen):
                self.configure(tls="auto", domain=domain, listen=listen)
                self.assertEqual(admin_http.cookie_secure(), expected)

    def test_set_and_clear_cookies_agree(self) -> None:
        # A logout whose attributes differ from the login's leaves the
        # original cookie in place, so the two must be decided identically.
        handler = admin_http.Handler.__new__(admin_http.Handler)
        handler.client_address = ("198.51.100.9", 1234)
        handler.headers = {}
        for tls in ("on", "off"):
            with self.subTest(tls=tls):
                self.configure(tls=tls, domain="dune.example.com")
                setters = handler._session_cookies("tok", "csrf")
                clearers = handler._clear_session_cookies()
                self.assertEqual(
                    ["Secure" in v for _, v in setters],
                    ["Secure" in v for _, v in clearers],
                )


class SettingsPinnedToTheEgg(unittest.TestCase):
    """apply-config.sh rewrites every env-backed setting from its egg
    variable at boot, so a panel edit that only touched the INI was undone
    by the next restart — including the scheduler's unattended one. The
    variable has to move with it, or the change must be refused."""

    ENV_KEYS = ("DUNE_PELICAN_URL", "DUNE_PELICAN_CLIENT_KEY",
                "DUNE_PELICAN_SERVER_ID", "DUNE_PELICAN_RESOLVE")

    def setUp(self) -> None:
        saved = {k: os.environ.get(k) for k in self.ENV_KEYS}

        def restore():
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def test_panel_only_settings_are_untouched(self):
        # 171 of the 195 carry no `env`; those always survive a boot and
        # must not be made to depend on panel credentials.
        self.assertIsNone(admin_http.pin_setting_to_egg(
            {"type": "bool", "key": "m_bSomething"}, True))

    def test_env_backed_setting_is_refused_when_the_panel_is_unreachable(self):
        result = admin_http.pin_setting_to_egg(
            {"env": "DUNE_MINING_OUTPUT", "type": "float", "key": "x"}, 5)
        self.assertIsNotNone(result)
        ok, detail = result
        self.assertFalse(ok, "a change that cannot be pinned must not be reported as applied")
        self.assertIn("DUNE_PELICAN_", detail)

    def test_the_value_sent_is_the_normalised_one(self):
        sent = {}

        def fake(key, value):
            sent["key"], sent["value"] = key, value
            return True, "ok"
        original = admin_http.admin_pelican.set_variable
        admin_http.admin_pelican.set_variable = fake
        self.addCleanup(setattr, admin_http.admin_pelican, "set_variable", original)

        admin_http.pin_setting_to_egg({"env": "DUNE_SANDWORMS_ENABLED", "type": "bool",
                                       "key": "sandworm.dune.Enabled"}, True)
        # The egg rule is in:True,False — the INI rendering and the panel
        # rule have to agree or every boolean write would be rejected.
        self.assertEqual(sent, {"key": "DUNE_SANDWORMS_ENABLED", "value": "True"})

        admin_http.pin_setting_to_egg({"env": "DUNE_MINING_OUTPUT", "type": "float",
                                       "key": "Dune.GlobalMiningOutputMultiplier"}, 5)
        self.assertEqual(sent["value"], "5.0")


class LoginGateAtomicity(unittest.TestCase):
    """The rate limiter is now reachable from many threads at once."""

    def setUp(self) -> None:
        admin_http.LOGIN_ATTEMPTS.clear()
        self.addCleanup(admin_http.LOGIN_ATTEMPTS.clear)

    def test_sequential_quota(self) -> None:
        ip = "203.0.113.4"
        for _ in range(admin_http.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(admin_http.login_attempt_gate(ip), 0)
        self.assertGreater(admin_http.login_attempt_gate(ip), 0)

    def test_clearing_the_bucket_restores_the_quota(self) -> None:
        ip = "203.0.113.5"
        for _ in range(admin_http.LOGIN_MAX_ATTEMPTS):
            admin_http.login_attempt_gate(ip)
        admin_http.clear_login_attempts(ip)
        self.assertEqual(admin_http.login_attempt_gate(ip), 0)

    def test_concurrent_attempts_cannot_exceed_the_quota(self) -> None:
        ip = "203.0.113.7"
        count = 40
        barrier = threading.Barrier(count)
        allowed: list[int] = []
        lock = threading.Lock()

        def hammer() -> None:
            barrier.wait()
            if admin_http.login_attempt_gate(ip) == 0:
                with lock:
                    allowed.append(1)

        threads = [threading.Thread(target=hammer) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(allowed), admin_http.LOGIN_MAX_ATTEMPTS)

    def test_expired_buckets_are_swept(self) -> None:
        stale = time.time() - admin_http.LOGIN_WINDOW_SECONDS - 60
        for i in range(admin_http.LOGIN_BUCKET_SWEEP_AT + 10):
            admin_http.LOGIN_ATTEMPTS[f"198.51.100.{i}"].append(stale)
        admin_http.login_attempt_gate("203.0.113.9")
        self.assertLessEqual(len(admin_http.LOGIN_ATTEMPTS), 1)


class RevocationConcurrency(unittest.TestCase):
    """json.dump over a dict another thread is mutating raises
    RuntimeError: dictionary changed size during iteration."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (
            ("STATE_DIR", self.tmp.name),
            ("REVOCATION_FILE", f"{self.tmp.name}/revoked.json"),
        ):
            original = getattr(admin_http, name)
            setattr(admin_http, name, value)
            self.addCleanup(setattr, admin_http, name, original)
        admin_http.REVOCATIONS.clear()
        self.addCleanup(admin_http.REVOCATIONS.clear)

    def test_parallel_revocations_do_not_corrupt_the_store(self) -> None:
        exp = int(time.time()) + 3600
        errors: list[BaseException] = []

        def revoke(worker: int) -> None:
            try:
                for i in range(300):
                    admin_http.revoke_token({"jti": f"w{worker}-{i}", "exp": exp})
            except BaseException as exc:  # noqa: BLE001 — the assertion is "nothing escapes"
                errors.append(exc)

        threads = [threading.Thread(target=revoke, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(len(admin_http.REVOCATIONS), 8 * 300)


class BlankSubmissionGuard(unittest.TestCase):
    """Issue #107 follow-up (review finding): normalize_value strips, so a
    whitespace-only submission collapses to blank — and for an empty_ok
    setting blank means a PUBLIC passwordless server. The old `required` egg
    rule caught that by accident; now the API accepts only the literal empty
    string as the explicit "clear the password" gesture."""

    PW = {"id": "server_password", "type": "string", "empty_ok": True}
    NAME = {"id": "server_display_name", "type": "string"}

    def test_whitespace_only_is_refused_on_empty_ok(self):
        msg = admin_http.blank_submission_error(self.PW, "   ")
        self.assertIsNotNone(msg)
        self.assertIn("empty", msg)

    def test_literal_empty_is_the_explicit_clear(self):
        self.assertIsNone(admin_http.blank_submission_error(self.PW, ""))

    def test_none_counts_as_the_explicit_clear_too(self):
        # JSON null from the UI is the same gesture as an empty field.
        self.assertIsNone(admin_http.blank_submission_error(self.PW, None))

    def test_real_password_passes(self):
        self.assertIsNone(admin_http.blank_submission_error(self.PW, "Sandworm"))

    def test_non_empty_ok_settings_keep_old_behaviour(self):
        # server_display_name has no empty_ok: whitespace still collapses to
        # blank via normalize_value, as it always has.
        self.assertIsNone(admin_http.blank_submission_error(self.NAME, "   "))


class CsvRows(unittest.TestCase):
    """_csv_rows must hand psql --csv output to the parser UNfiltered:
    RFC4180-quoted fields legally contain embedded (even blank) newlines,
    and a blank-line pre-filter corrupts them (review-caught with a live
    psql repro)."""

    def test_simple_table(self):
        rows = admin_http._csv_rows("a,b\n1,x\n2,y\n")
        self.assertEqual(rows, [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}])

    def test_header_only_is_empty(self):
        self.assertEqual(admin_http._csv_rows("a,b\n"), [])

    def test_blank_text_is_empty(self):
        self.assertEqual(admin_http._csv_rows("  \n"), [])

    def test_quoted_field_with_blank_line_survives(self):
        text = 'id,owner\n1,"Smith, ""The"" Great"\n2,"Multi\nLine\n\nName"\n'
        rows = admin_http._csv_rows(text)
        self.assertEqual(rows[0]["owner"], 'Smith, "The" Great')
        self.assertEqual(rows[1]["owner"], "Multi\nLine\n\nName")


if __name__ == "__main__":
    unittest.main()
