#!/usr/bin/env python3
"""Pelican client-API access, shared by the scheduler and the admin panel.

Two callers need the panel: the scheduler restarts the server unattended,
and the admin panel writes a setting's egg variable back so a restart
stops silently reverting it. They were about to hold two copies of the
same connection handling, including the `curl --resolve` trick, so it
lives here once.

Status meanings below are measured against a live panel, not assumed —
they are not interchangeable and each points the operator somewhere
different. See the hint tables.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import urllib.parse

# Header values cannot carry control characters. A key pasted with a
# trailing newline used to raise ValueError out of conn.request(), and
# that exception's message quotes the whole Authorization header — so the
# API key reached logs/scheduler.log in cleartext.
_HEADER_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def clean_setting(raw) -> str:
    """Panel variables are typed and pasted. Strip whitespace and
    surrounding quotes: a quoted key travels as part of the credential and
    comes back 401, which reads as "my key is wrong"."""
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def redact(message: str, secret: str) -> str:
    """Last line of defence: no path may put the API key into a log."""
    return message.replace(secret, "<redacted>") if secret else message


def config() -> tuple[str, str, str, str | None]:
    """(url, key, server_id, resolve_ip) from the environment."""
    return (clean_setting(os.environ.get("DUNE_PELICAN_URL")).rstrip("/"),
            clean_setting(os.environ.get("DUNE_PELICAN_CLIENT_KEY")),
            clean_setting(os.environ.get("DUNE_PELICAN_SERVER_ID")),
            clean_setting(os.environ.get("DUNE_PELICAN_RESOLVE")) or None)


def configured() -> bool:
    url, key, sid, _ = config()
    return bool(url and key and sid)


class ResolvingHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that TCP-connects to a fixed IP while keeping TLS
    SNI, certificate validation and the Host header pinned to the original
    hostname — the Python equivalent of `curl --resolve host:port:ip`.

    Lets the in-container caller reach the panel straight over the wg
    tunnel instead of via public DNS, bypassing a CDN that may challenge
    the container's egress IP. The certificate still has to be valid for
    the real hostname, so this is a routing override, not a verification
    downgrade."""

    def __init__(self, host, resolve_ip, *, context=None, **kw):
        context = context or ssl.create_default_context()
        super().__init__(host, context=context, **kw)
        self._resolve_ip = resolve_ip
        self._ssl_ctx = context

    def connect(self):
        sock = socket.create_connection((self._resolve_ip, self.port), self.timeout)
        self.sock = self._ssl_ctx.wrap_socket(sock, server_hostname=self.host)


def split_url(url: str) -> tuple[str, str, int]:
    """Pure: (scheme, host, port) from the panel URL."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme or "https"
    return scheme, parts.hostname or "", parts.port or (443 if scheme == "https" else 80)


def open_conn(scheme, host, port, resolve_ip, timeout):
    if scheme == "https":
        ctx = ssl.create_default_context()
        if resolve_ip:
            return ResolvingHTTPSConnection(host, resolve_ip, port=port,
                                            timeout=timeout, context=ctx)
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=ctx)
    return http.client.HTTPConnection(resolve_ip or host, port=port, timeout=timeout)


def _detail(body: str) -> str:
    """The panel's own explanation, which is better than anything we could
    invent — "The value must be a number." beats "HTTP 422"."""
    try:
        errors = json.loads(body).get("errors") or []
        return str(errors[0].get("detail", "")).strip()
    except (ValueError, AttributeError, IndexError, TypeError):
        return ""


def call(method: str, path: str, payload=None, timeout: int = 20) -> tuple[int, str, str]:
    """(status, panel_detail, error). status is 0 when the request could
    not be made at all, and `error` then says why — never quoting the
    credential."""
    url, key, _sid, resolve_ip = config()
    if _HEADER_UNSAFE.search(key):
        return 0, "", ("DUNE_PELICAN_CLIENT_KEY contains a control character "
                       "(a stray newline from copy-paste?) — re-paste it as a single line")
    scheme, host, port = split_url(url)
    if not host:
        return 0, "", "DUNE_PELICAN_URL has no host"
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "Host": host}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = None
    try:
        conn = open_conn(scheme, host, port, resolve_ip, timeout)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, _detail(resp.read(4000).decode("utf-8", "replace")), ""
    except ValueError:
        # str(e) can quote the header; never report it.
        return 0, "", "the request could not be built"
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        return 0, "", redact(f"{type(exc).__name__}: {exc}", key)[:200]
    finally:
        if conn is not None:
            conn.close()


_AUTH_HINTS = {
    401: ("the panel rejected the credential: DUNE_PELICAN_CLIENT_KEY is not a valid "
          "client key. Check it was not truncated or revoked, and that you pasted the "
          "key itself rather than its description"),
    403: ("authenticated, but that key may not act on this server — an Application "
          "(admin) API key cannot drive the client API. Create an Account API key "
          "from the panel user that owns the server"),
    404: ("the credential is fine but the panel has no such server: check "
          "DUNE_PELICAN_SERVER_ID (the short id from the server's URL)"),
}


def restart_hint(code: int) -> str:
    if code in _AUTH_HINTS:
        return _AUTH_HINTS[code]
    if code == 409:
        return "the panel refused the power action — the server is probably already restarting"
    if code == 422:
        return "the panel rejected the request body — report this, the egg built it wrong"
    return ""


def variable_hint(code: int) -> str:
    if code in _AUTH_HINTS:
        return _AUTH_HINTS[code]
    if code == 400:
        return ("the panel has no such egg variable. If this setting was added by a newer "
                "egg, re-import the egg in the panel — a server reinstall updates scripts/ "
                "but not the egg definition")
    if code == 422:
        return "the panel's own validation rejected the value"
    return ""


def restart(timeout: int = 20) -> tuple[bool, str]:
    """POST power:restart. Returns (ok, human detail)."""
    if not configured():
        return False, "restart skipped: DUNE_PELICAN_{URL,CLIENT_KEY,SERVER_ID} not set"
    _url, _key, sid, resolve_ip = config()
    code, detail, error = call("POST", f"/api/client/servers/{sid}/power",
                               {"signal": "restart"}, timeout)
    if code == 0:
        return False, f"power restart error: {error}"
    via = f" via {resolve_ip}" if resolve_ip else ""
    hint = restart_hint(code)
    return 200 <= code < 300, (f"power restart HTTP {code}{via}"
                               + (f" — {detail or hint}" if (detail or hint) else ""))


def set_variable(key: str, value, timeout: int = 20) -> tuple[bool, str]:
    """PUT one startup variable. Returns (ok, human detail).

    This is what stops a restart silently reverting a setting changed in
    the admin panel: apply-config.sh rewrites every env-backed setting
    from its egg variable on boot, so the variable has to move too."""
    if not configured():
        return False, ("panel credentials not set — DUNE_PELICAN_{URL,CLIENT_KEY,SERVER_ID} "
                       "are what let the panel keep the egg variable in step")
    _url, _key, sid, _resolve = config()
    code, detail, error = call("PUT", f"/api/client/servers/{sid}/startup/variable",
                               {"key": key, "value": str(value)}, timeout)
    if code == 0:
        return False, f"panel variable write failed: {error}"
    if 200 <= code < 300:
        return True, f"{key} updated in the panel"
    hint = variable_hint(code)
    return False, f"panel refused {key} (HTTP {code})" + (f": {detail or hint}" if (detail or hint) else "")
