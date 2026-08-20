#!/usr/bin/env python3
"""HTTP backend for the Pelican Dune Awakening admin pipeline.

Two operating modes (selected at process start via env):

  Internal mode (DUNE_ADMIN_UI_ENABLED unset/0)
    Loopback-only HTTP wrapper around admin-publish.sh. Pre-existing
    behavior: optional Bearer auth via DUNE_ADMIN_HTTP_AUTH, no
    session management, no static file serving.

  UI mode (DUNE_ADMIN_UI_ENABLED=1)
    Public-facing API + SPA host. Binds DUNE_ADMIN_HTTP_ADDR:PORT.
    Session-token auth (issued by POST /api/login, validated by HMAC
    against DUNE_ADMIN_UI_SESSION_SECRET). Serves the React SPA from
    DUNE_ADMIN_UI_WEB_ROOT (defaults to $DUNE_BASE_DIR/data/web/dist).
    CORS allows DUNE_ADMIN_UI_DOMAIN when set.

In either mode the publish path (POST /admin/<sub>) shells out to
admin-publish.sh — that's the canonical pipeline used by the panel
console listener too, so we don't duplicate envelope-building logic.

All command invocations land in an in-memory ring buffer surfaced
via GET /api/history for the UI's persistent output panel.
"""
from __future__ import annotations

import base64
import collections
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# Ensure sibling modules (admin_progression) import regardless of CWD —
# whether this file is run as a script, via importlib in tests, or -m.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin_progression  # noqa: E402  # type: ignore[import-not-found]  (resolved at runtime via sys.path; pure XP/keystone math, no DB/network)
from admin_status import merge_status, parse_player_counts  # noqa: E402  # type: ignore[import-not-found]  (sys.path; pure status-grid merge, no DB/network)
import admin_ini_merge  # noqa: E402  # type: ignore[import-not-found]  (sys.path; pure INI read/upsert engine, no DB/network)
import admin_map  # noqa: E402  # type: ignore[import-not-found]  (sys.path; pure map-key validation + marker CSV parse, no DB/network)
import admin_locations  # noqa: E402  # type: ignore[import-not-found]  (sys.path; pure JSON locations store, no DB/network)
import admin_logs  # noqa: E402  # type: ignore[import-not-found]  (sys.path; pure log path-safety + secret redaction, no DB/network)
import admin_instances  # noqa: E402  # type: ignore[import-not-found]  (sys.path; CA-pinned mock-k8s transport + ServerSetScale key resolution, shared with the scheduler)
import admin_market  # noqa: E402  # type: ignore[import-not-found]  (sys.path; market config loader — single source for the persistent config path)
import admin_park  # noqa: E402  # type: ignore[import-not-found]  (sys.path; pure parked-sietch id set — fail-safe file read, no DB/network)
import admin_history  # noqa: E402  # type: ignore[import-not-found]  (sys.path; persisted command audit — SQLite under server/state/, no DB/network)
import admin_pelican  # noqa: E402  # type: ignore[import-not-found]  (sys.path; Pelican client-API access, shared with the scheduler)
import admin_tls  # noqa: E402  # type: ignore[import-not-found]  (sys.path; direct TLS termination + hot reload)


# --------------------------------------------------------------------------
# Config — env vars resolved once at process start.
# --------------------------------------------------------------------------
LISTEN_ADDR = os.environ.get("DUNE_ADMIN_HTTP_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("DUNE_ADMIN_HTTP_PORT", "8089"))
SHARED_AUTH = os.environ.get("DUNE_ADMIN_HTTP_AUTH", "")
BASE_DIR = os.environ.get("DUNE_BASE_DIR", "/home/container")
SCRIPTS_DIR = BASE_DIR + "/scripts"
LOGS_DIR = BASE_DIR + "/logs"

# Listener hardening (issue #89). In UI mode this socket is bound to
# 0.0.0.0 and reachable from the whole internet, so it is scanned within
# hours of coming up.
#
#   CONNECTION_TIMEOUT_SECONDS  Per-connection socket timeout. Without it
#       a peer that connects and never finishes a request line parks a
#       worker forever — on the previous single-threaded HTTPServer that
#       was the entire panel, permanently, until the container restarted.
#       Responses are HTTP/1.0 (no keep-alive), so this only ever bounds
#       how long we wait on a client that owes us bytes.
#   MAX_CONNECTIONS  Ceiling on connections being served at once. Threads
#       are what keeps one slow peer from starving the others, so a flood
#       must not be able to spawn them without bound.
CONNECTION_TIMEOUT_SECONDS = float(os.environ.get("DUNE_ADMIN_HTTP_TIMEOUT_SECS", "30"))
MAX_CONNECTIONS = int(os.environ.get("DUNE_ADMIN_HTTP_MAX_CONNS", "64"))

# The listener is threaded, but the helper scripts behind the run_*()
# wrappers below were written against the old serialised server: several
# read-modify-write JSON/INI state files with no locking of their own, so
# two overlapping invocations would silently lose an update. Serialising
# every out-of-process command here preserves the invariant they assume.
COMMAND_LOCK = threading.Lock()

# Guards the in-process auth state (REVOCATIONS, LOGIN_ATTEMPTS). Held
# only for dict/deque surgery and the small atomic revocation write, so
# contention is irrelevant. Reentrant because revoke_token() persists
# while holding it.
AUTH_STATE_LOCK = threading.RLock()
# mock-k8s instance primitives + constants now live in admin_instances.py (shared
# with the scheduler's scale-instance task); re-exported here so the routes below
# stay unchanged. SCALABLE_MAPS = on-demand maps only — never the always-warm
# Survival_1 / Overmap (stopping those breaks the base game loop). The backend
# re-validates; mirror in InstancesTab.tsx.
MOCK_K8S_PORT = admin_instances.MOCK_K8S_PORT
SA_MOUNT_PATH = admin_instances.SA_MOUNT_PATH
SCALABLE_MAPS = admin_instances.SCALABLE_MAPS
SCALE_REPLICAS_MAX = admin_instances.SCALE_REPLICAS_MAX
# Server-settings catalogue (shared with apply-config.sh) + the INI sinks it
# resolves to. GET/PUT /api/settings read/write these directly (local files,
# no PG). Logical sink name -> path, matching apply-config.sh's FILES.
SETTINGS_SCHEMA_PATH = BASE_DIR + "/data/admin/settings-schema.json"
INI_FILES = {
    "UserEngine": BASE_DIR + "/server/state/ue5-saved/UserSettings/UserEngine.ini",
    "UserGame": BASE_DIR + "/server/state/ue5-saved/UserSettings/UserGame.ini",
    # Operator UClass overrides. prestart.sh strips + re-appends this verbatim
    # into UserGame.ini on every boot, so it takes precedence and round-trips
    # cleanly — the designed sink for the [/Script/...] catalogue (no env vars).
    "UserOverrides": BASE_DIR + "/server/state/UserOverrides.ini",
    "ondemand": BASE_DIR + "/server/state/ondemand.ini",
}
STATE_DIR = BASE_DIR + "/state"
PUBLISH_SH = SCRIPTS_DIR + "/admin-publish.sh"
REVOCATION_FILE = STATE_DIR + "/admin-ui-revoked-jtis.json"

UI_ENABLED = os.environ.get("DUNE_ADMIN_UI_ENABLED", "0") == "1"
UI_PASSWORD = os.environ.get("DUNE_ADMIN_UI_PASSWORD", "")
UI_SESSION_SECRET = os.environ.get("DUNE_ADMIN_UI_SESSION_SECRET", "")
UI_DOMAIN = os.environ.get("DUNE_ADMIN_UI_DOMAIN", "")
UI_WEB_ROOT = Path(
    os.environ.get("DUNE_ADMIN_UI_WEB_ROOT", BASE_DIR + "/data/web/dist")
).resolve()

# Steam Web API key. Optional. When set, /api/steam-info?ids=... fetches
# personaname + avatar for a comma-separated list of 64-bit Steam IDs
# from api.steampowered.com. Results are cached in-memory for an hour
# to stay under Steam's rate limit (~100k calls/day per key).
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
STEAM_CACHE_TTL_SECONDS = 3600

SESSION_TTL_SECONDS = 7 * 24 * 3600  # 1 week; renew on each /api/me hit
# Ceiling on ?limit= for the audit trail. The store's own retention is
# admin_history.DEFAULT_KEEP.
HISTORY_PAGE_MAX = 200

# Cookie names + flags. We emit two cookies on login:
#   SESSION_COOKIE_NAME — HttpOnly so JS (and any XSS) can't read it.
#       Carries the HMAC session token. SameSite=Strict prevents
#       cross-origin browsers from attaching it.
#   CSRF_COOKIE_NAME    — NOT HttpOnly. The SPA reads this and echoes
#       it back as X-CSRF-Token on mutating requests. Cookie value is
#       also embedded in the session token's payload so the server can
#       validate without any server-side CSRF storage.
#
# `Secure` is auto-applied when we believe TLS is fronting the panel
# (UI_DOMAIN set + non-loopback bind). Operators on plain HTTP loopback
# dev would otherwise be locked out — browsers refuse Secure cookies
# over http://.
SESSION_COOKIE_NAME = "dune_session"
CSRF_COOKIE_NAME = "dune_csrf"

# Hard cap on POST body size. Admin payloads are tiny — even the largest
# legitimate request (broadcast title+body) is well under 4 KiB. 64 KiB
# leaves headroom for future fields without letting a hostile client
# allocate gigabytes via a forged Content-Length.
MAX_BODY_BYTES = 64 * 1024

# Login rate-limit. Sliding window per source IP: when N attempts land
# within W seconds, subsequent /api/login requests from that IP get
# 429 + Retry-After until the oldest attempt ages out. The deque has a
# fixed maxlen — older entries auto-evict, so we never grow unbounded
# per-IP. A successful login clears the bucket.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("DUNE_ADMIN_UI_LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("DUNE_ADMIN_UI_LOGIN_WINDOW_SECS", "900"))
LOGIN_ATTEMPTS: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque(maxlen=LOGIN_MAX_ATTEMPTS)
)
# Buckets are pruned when their IP comes back, which never happens for the
# one-shot source addresses of a distributed scan. Once the table passes
# this many entries, sweep the expired ones so a day of being scanned
# can't grow it without bound.
LOGIN_BUCKET_SWEEP_AT = int(os.environ.get("DUNE_ADMIN_UI_LOGIN_SWEEP_AT", "1024"))

# The command audit (GET /api/history) lives in admin_history now — a
# SQLite table under server/state/. It used to be an in-memory deque,
# which died with the process; since the panel restarts with the server
# (scheduler auto-restart, `panel restart`, reinstall), the trail an
# operator wants precisely when something went wrong was the first thing
# lost. admin_history also drops read-only traffic: MapTab polls
# /api/map/markers every 4s through run_publish, and 200 buffer slots of
# `map-markers` evicted every real action inside quarter of an hour.

# Steam personaname/avatar cache. {steam_id: (cached_at_ts, info_dict)}.
STEAM_CACHE: dict[str, tuple[float, dict]] = {}

# Revoked session token JTIs (HMAC tokens are stateless — without this
# set, a stolen / logged-out token stays valid until its exp). Each entry
# is (jti, exp_unix). We persist to REVOCATION_FILE so revocations
# survive a process restart, prune expired entries on every load, and
# write atomically (temp file + rename) to avoid corruption on crash.
REVOCATIONS: dict[str, int] = {}


def _load_revocations() -> None:
    """Read REVOCATIONS from disk and drop expired entries."""
    with AUTH_STATE_LOCK:
        REVOCATIONS.clear()
        try:
            with open(REVOCATION_FILE) as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log(f"WARN failed to load {REVOCATION_FILE}: {exc} — starting with empty revocation list")
            return
        if not isinstance(raw, dict):
            log(f"WARN {REVOCATION_FILE} has unexpected shape; resetting")
            return
        now = int(time.time())
        for jti, exp in raw.items():
            try:
                exp_int = int(exp)
            except (TypeError, ValueError):
                continue
            if exp_int > now:
                REVOCATIONS[str(jti)] = exp_int


def _save_revocations() -> None:
    """Atomic write of REVOCATIONS to disk. Best-effort — failures only
    log; in-memory revocations still apply for the current process.

    Callers hold AUTH_STATE_LOCK: json.dump iterates the dict, and a
    concurrent logout inserting into it mid-iteration raises RuntimeError
    ("dictionary changed size during iteration"). The fixed .tmp name is
    equally a shared resource between writers."""
    with AUTH_STATE_LOCK:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = REVOCATION_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(REVOCATIONS, fh, separators=(",", ":"))
            os.replace(tmp, REVOCATION_FILE)
        except OSError as exc:
            log(f"WARN failed to persist {REVOCATION_FILE}: {exc}")


def log(msg: str) -> None:
    print(f"[admin-http] [INFO] {msg}", flush=True)


# --------------------------------------------------------------------------
# Session-token helpers (UI mode). HMAC-signed `<payload_b64>.<sig_b64>`
# with the secret. Payload is JSON {iat, exp, sub}. Keeps the protocol
# simple — no JWT lib dependency, no rotation complexity.
# --------------------------------------------------------------------------
def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def issue_session_token(subject: str = "admin") -> tuple[str, str]:
    """Returns (token, csrf). The csrf value is also embedded in the
    token payload — for cookie-based callers, the server validates
    `X-CSRF-Token` header against the payload field. For Bearer-header
    callers the csrf field is unused (header-auth is CSRF-safe by
    construction)."""
    if not UI_SESSION_SECRET:
        raise RuntimeError("DUNE_ADMIN_UI_SESSION_SECRET not set — prestart.sh failed to generate it")
    now = int(time.time())
    csrf = secrets.token_urlsafe(16)
    payload = {
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
        "sub": subject,
        # Random per-session id so /api/logout can selectively revoke
        # THIS token without rotating the secret (which would invalidate
        # every session at once). 128 bits of entropy via secrets.
        "jti": secrets.token_urlsafe(16),
        "csrf": csrf,
    }
    payload_b64 = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(
        UI_SESSION_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).digest()
    return f"{payload_b64}.{_b64u(sig)}", csrf


def verify_session_token(token: str) -> dict | None:
    """Returns the decoded payload if the token is valid + unexpired and
    not in the revocation list, else None."""
    if not UI_SESSION_SECRET or not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(
            UI_SESSION_SECRET.encode(),
            payload_b64.encode(),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_sig, _b64u_decode(sig_b64)):
            return None
        payload = json.loads(_b64u_decode(payload_b64))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        jti = payload.get("jti")
        if isinstance(jti, str) and jti in REVOCATIONS:
            return None
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def revoke_token(payload: dict) -> bool:
    """Add the token's jti to the revocation list and persist. Returns
    True if revocation was recorded, False if the payload had no jti
    (legacy tokens issued before this field was added)."""
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not isinstance(jti, str) or not isinstance(exp, int):
        return False
    with AUTH_STATE_LOCK:
        REVOCATIONS[jti] = exp
        _save_revocations()
    return True


# --------------------------------------------------------------------------
# Client IP resolution.
#
# The socket peer is the only trustworthy source by default: honouring
# X-Forwarded-For unconditionally lets any client forge an arbitrary
# address and hand itself a fresh rate-limit bucket per request.
#
# But behind a TLS-terminating reverse proxy the socket peer is the PROXY
# for every request, so all logins share one bucket — 5 failures from
# anywhere on the internet then lock the operator out for the window. The
# rate limit stops protecting anyone and becomes a denial of service on
# the account it was meant to defend.
#
# So: trust the header only from peers the operator has declared, and
# resolve to the RIGHTMOST address that isn't one of those. Entries to
# the left of it are attacker-supplied — a client can prepend anything it
# likes, and only the hops we know about are allowed to vouch for what
# they appended. Empty declaration (the default) = peer only, i.e. the
# previous behaviour exactly, so nobody inherits a new trust surface.
# --------------------------------------------------------------------------
def _parse_trusted_proxies(raw: str) -> list:
    """Comma/space-separated IPs and CIDRs -> ip_network list. Invalid
    entries are logged and dropped rather than silently widening or
    narrowing trust."""
    nets = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            log(f"WARN ignoring invalid DUNE_ADMIN_UI_TRUSTED_PROXIES entry {token!r}")
    return nets


TRUSTED_PROXIES = _parse_trusted_proxies(os.environ.get("DUNE_ADMIN_UI_TRUSTED_PROXIES", ""))


def _is_trusted_proxy(ip: str) -> bool:
    if not TRUSTED_PROXIES:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def resolve_client_ip(peer: str, forwarded_for: str) -> str:
    """The address to bucket this request under.

    Returns `peer` unless it is a declared proxy, in which case the
    rightmost X-Forwarded-For entry that is not itself a declared proxy
    wins. Falls back to `peer` when the header is absent, unparseable, or
    contains nothing but declared proxies."""
    if not peer:
        return "unknown"
    if not _is_trusted_proxy(peer) or not forwarded_for:
        return peer
    for candidate in reversed([p.strip() for p in forwarded_for.split(",")]):
        if not candidate or _is_trusted_proxy(candidate):
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            # A malformed hop means we can no longer tell who appended
            # what; stop rather than trust anything further left.
            return peer
        return candidate
    return peer


# --------------------------------------------------------------------------
# Should the session cookies carry `Secure`?
#
# This used to be inferred from DUNE_ADMIN_UI_DOMAIN, which conflates two
# independent facts: the hostname browsers will use (needed for CORS) and
# whether TLS terminates in front of us (needed for this flag). They
# coincide behind a reverse proxy and diverge without one: an operator who
# points a DNS record straight at an exposed box and fills in the domain
# got a Secure cookie over plain HTTP, which the browser discards on
# arrival — /api/login answers 200, the SPA authenticates purely by that
# cookie, and the login screen simply reappears with no error. There was
# no working URL at all in that state: http:// drops the cookie and
# nothing serves https://.
#
# admin-http never speaks TLS itself, so the only truthful signals are an
# explicit operator statement or a declared proxy reporting the scheme.
# In precedence order:
#
#   1. DUNE_ADMIN_UI_TLS=on|off — explicit beats inferred, always.
#   2. X-Forwarded-Proto from a proxy the operator declared.
#   3. The old heuristic (domain set + non-loopback bind), so a
#      deployment that works today does not silently lose Secure.
# --------------------------------------------------------------------------
# Set by build_server() once it knows whether a usable certificate was found.
# Not a config value: it states what the socket is actually doing.
SERVING_TLS = False

_UI_TLS_RAW = os.environ.get("DUNE_ADMIN_UI_TLS", "auto").strip().lower() or "auto"
if _UI_TLS_RAW not in ("auto", "on", "off"):
    log(f"WARN DUNE_ADMIN_UI_TLS={_UI_TLS_RAW!r} is not auto/on/off — using auto")
    _UI_TLS_RAW = "auto"
UI_TLS = _UI_TLS_RAW


def cookie_secure(forwarded_proto: str = "") -> bool:
    """`forwarded_proto` is the scheme a DECLARED proxy reported, or ""
    when nobody trustworthy said anything."""
    # Serving TLS ourselves is not an inference: the browser reached us over
    # https or it did not reach us at all. It therefore outranks even an
    # explicit `off`, which would otherwise hand out a non-Secure cookie on
    # a connection that is demonstrably encrypted.
    if SERVING_TLS:
        return True
    if UI_TLS == "on":
        return True
    if UI_TLS == "off":
        return False
    if forwarded_proto == "https":
        return True
    if forwarded_proto == "http":
        return False
    return bool(UI_DOMAIN) and LISTEN_ADDR not in _LOOPBACK_ADDRS


# --------------------------------------------------------------------------
# Login rate-limit gate. One atomic check-and-consume rather than the
# separate "am I over quota?" / "record a failure" pair it replaced: the
# listener is threaded now, and a burst of concurrent guesses all read the
# old bucket before any of them wrote to it, so the quota could be
# overrun by however many requests an attacker managed to land at once.
# --------------------------------------------------------------------------
def _sweep_login_attempts(now: float) -> None:
    """Drop buckets whose newest attempt has aged out. Caller holds the lock."""
    cutoff = now - LOGIN_WINDOW_SECONDS
    stale = [ip for ip, bucket in LOGIN_ATTEMPTS.items() if not bucket or bucket[-1] < cutoff]
    for ip in stale:
        LOGIN_ATTEMPTS.pop(ip, None)
    if stale:
        log(f"swept {len(stale)} expired login-attempt bucket(s); {len(LOGIN_ATTEMPTS)} left")


def login_attempt_gate(ip: str) -> int:
    """Consume one login attempt for `ip`. Returns 0 when the attempt may
    proceed, else the Retry-After seconds until the window frees a slot.

    The attempt is charged up front, so concurrent requests can never
    exceed the quota. clear_login_attempts() refunds the whole bucket on
    a successful login, which keeps an operator who fat-fingered their
    password a few times from being locked out for the rest of the window."""
    with AUTH_STATE_LOCK:
        now = time.time()
        if len(LOGIN_ATTEMPTS) > LOGIN_BUCKET_SWEEP_AT:
            _sweep_login_attempts(now)
        bucket = LOGIN_ATTEMPTS[ip]
        cutoff = now - LOGIN_WINDOW_SECONDS
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= LOGIN_MAX_ATTEMPTS:
            return max(int(bucket[0] + LOGIN_WINDOW_SECONDS - now) + 1, 1)
        bucket.append(now)
        return 0


def login_attempts_in_window(ip: str) -> int:
    """How many attempts are currently charged against `ip` (for logging)."""
    with AUTH_STATE_LOCK:
        return len(LOGIN_ATTEMPTS.get(ip, ()))


def clear_login_attempts(ip: str) -> None:
    with AUTH_STATE_LOCK:
        LOGIN_ATTEMPTS.pop(ip, None)


# --------------------------------------------------------------------------
# Subcommand argv builder. POST /admin/<sub> body JSON -> admin-publish.sh argv.
# --------------------------------------------------------------------------
def build_argv(sub: str, body: dict) -> list[str]:
    if sub == "broadcast":
        return [
            sub,
            str(body["title"]),
            str(body["body"]),
            str(body.get("duration", 30)),
        ]
    if sub == "shutdown":
        argv = [sub, str(body["type"])]
        if "lead_secs" in body:
            argv.append(str(body["lead_secs"]))
        if "freq_secs" in body:
            argv.append(str(body["freq_secs"]))
        return argv
    if sub in ("kick", "clean", "reset"):
        return [sub, str(body["player_id"])]
    if sub == "water":
        argv = [sub, str(body["player_id"])]
        if "amount" in body:
            argv.append(str(body["amount"]))
        return argv
    if sub == "give":
        return [
            sub,
            str(body["player_id"]),
            str(body["item"]),
            str(body.get("qty", 1)),
            str(body.get("durability", 1.0)),
        ]
    if sub == "xp":
        return [sub, str(body["player_id"]), str(body["amount"])]
    if sub == "skill":
        return [sub, str(body["player_id"]), str(body["module"]), str(body["level"])]
    if sub == "points":
        return [sub, str(body["player_id"]), str(body["amount"])]
    if sub in ("teleport", "tpsafe"):
        argv = [
            sub,
            str(body["player_id"]),
            str(body["x"]),
            str(body["y"]),
            str(body["z"]),
        ]
        if "yaw" in body:
            argv.append(str(body["yaw"]))
        return argv
    if sub == "vehicle":
        argv = [
            sub,
            str(body["player_id"]),
            str(body["class"]),
            str(body["x"]),
            str(body["y"]),
            str(body["z"]),
            str(body["template"]),
        ]
        # Positional optional trailing args. Once any tail field is set we
        # need to pad the earlier ones with empty placeholders so position
        # discipline is preserved.
        if "rotation" in body or "persistent" in body or "faction" in body:
            argv.append(str(body.get("rotation", "")))
            argv.append(str(body.get("persistent", 1.0)))
            if "faction" in body and body["faction"]:
                argv.append(str(body["faction"]))
        return argv
    if sub == "cheat":
        return [sub, str(body["player_id"]), str(body["script"])]
    if sub == "exec":
        return [sub, str(body["command"])]
    # NOTE: the `raw` subcommand was deliberately removed. It accepted an
    # arbitrary ServerCommand JSON body and forwarded it untouched to
    # admin-publish.sh, which itself fed the value into a python -c
    # heredoc. That round-tripped attacker control into Python source —
    # any operator session compromise became container-root RCE. No
    # legitimate operator workflow needs this; protocol-debug callers
    # can still use DUNE_ADMIN_DRY_RUN=1 from a shell.
    raise ValueError(f"unknown subcommand: {sub}")


def _run_command(cmd: list[str], timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run one helper out-of-process, serialised against every other
    command via COMMAND_LOCK. Every run_*() below goes through here so a
    future runner cannot silently opt out of that serialisation."""
    with COMMAND_LOCK:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _run_helper(script: str, argv: list[str], timeout: int, env: dict | None = None) -> dict:
    """Run scripts/<script> and shape its JSON stdout into
    {ok, exit_code, data, stderr}. Non-JSON output lands in data.raw."""
    res = _run_command([sys.executable, os.path.join(SCRIPTS_DIR, script), *argv], timeout, env)
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"raw": res.stdout[:500]}
    ok = res.returncode == 0 and (data.get("ok", True) if isinstance(data, dict) else True)
    return {"ok": ok, "exit_code": res.returncode, "data": data, "stderr": res.stderr[:500]}


def _csv_rows(text: str) -> list[dict]:
    """First line = header, rest = rows — the shape every CSV-emitting
    subcommand (bases, base-water, db-*) prints. The text goes to the csv
    parser UNfiltered: RFC4180-quoted fields legally contain embedded (even
    blank) newlines, and psql --csv emits them that way — a blank-line
    pre-filter would corrupt those values (review-caught). Numeric-looking
    cells stay strings; the UI formats."""
    import csv as _csv
    import io as _io
    if not text.strip():
        return []
    reader = _csv.DictReader(_io.StringIO(text))
    return [dict(r) for r in reader]


def run_publish(argv: list[str], timeout: int = 30) -> dict:
    result = _run_command([PUBLISH_SH, *argv], timeout)
    ok = result.returncode == 0 and (
        "publish=ok" in result.stdout
        or "publish=db-delete" in result.stdout
        or "publish=db-write" in result.stdout
        or argv[0] in (
            "players", "resolve", "pos", "vehicles", "items", "skills", "items-json",
            "vehicle-list", "db-tables", "db-describe", "db-sample", "db-search", "db-sql",
            "player-state", "char-xp-read", "inventory-list", "tags-get",
            "server-status", "farm-player-count", "map-markers", "db-backup", "db-backup-list", "spice-list",
            "char-backup", "char-backup-list", "doctor", "bases", "base-water",
        )
    )
    entry = {
        "ts": int(time.time()),
        "argv": argv,
        "ok": ok,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    # Best-effort and read-only-filtered; never fails the command it audits.
    admin_history.record(BASE_DIR, entry)
    return entry


def run_welcome(sub: str, timeout: int = 60) -> dict:
    """Run scripts/admin_welcome.py <sub> <BASE_DIR> out-of-process (it shells to
    give-item, so keep it isolated + timeout-bounded). Returns {ok, data, ...}
    where `data` is the script's parsed JSON output."""
    return _run_helper("admin_welcome.py", [sub, BASE_DIR], timeout)


def run_schedule(sub: str, *args: str, timeout: int = 60) -> dict:
    """Run scripts/admin_schedule.py <sub> <BASE_DIR> [args] out-of-process
    (it shells to admin-publish for backups/broadcast). Returns {ok, data, ...}
    where `data` is the script's parsed JSON output."""
    return _run_helper("admin_schedule.py", [sub, BASE_DIR, *args], timeout)


def run_sietch(sub: str, *args: str, timeout: int = 20) -> dict:
    """Run scripts/admin_sietch.py <sub> [args] out-of-process (per-sietch config
    I/O; no DB/network). BASE via env so file/dir args aren't mistaken for it.
    Returns {ok, data, ...} where `data` is the script's parsed JSON output."""
    return _run_helper(
        "admin_sietch.py", [sub, *args], timeout,
        env={**os.environ, "DUNE_BASE_DIR": BASE_DIR},
    )


def run_market(sub: str, *args: str, timeout: int = 20) -> dict:
    """Run scripts/admin_market.py <sub> [args] <BASE_DIR> out-of-process.
    Returns {ok, data, ...} where `data` is the script's parsed JSON output.
    Mostly read-only (pricing + market summary); the 7b-3 buy-tick/set-config
    subcommands shell admin-publish.sh for the actual exchange writes."""
    return _run_helper("admin_market.py", [sub, *args, BASE_DIR], timeout)


def run_autoscaler(sub: str, *args: str, timeout: int = 30) -> dict:
    """Run scripts/admin_autoscaler.py <sub> <BASE_DIR> [args] out-of-process.
    Returns {ok, data, ...} where `data` is the script's parsed JSON output. The
    tick/status subcommands reach mock-k8s + server-status, so keep it timeout-bounded."""
    return _run_helper("admin_autoscaler.py", [sub, BASE_DIR, *args], timeout)


# CA-pinned mock-k8s transport — defined in admin_instances.py, re-exported so the
# GET /api/status + instance-scale routes below call them unchanged.
fetch_mock_status = admin_instances.fetch_mock_status
mock_k8s_request = admin_instances.mock_k8s_request


# --------------------------------------------------------------------------
# Server-settings helpers (Phase 5). The catalogue in settings-schema.json is
# the single source of truth shared with apply-config.sh; GET/PUT /api/settings
# read/write the INI sinks directly via the (unit-tested) admin_ini_merge engine.
# Changes take effect on the next server restart (UE5 reads the INIs at boot).
# --------------------------------------------------------------------------
def load_settings_schema() -> list[dict]:
    try:
        with open(SETTINGS_SCHEMA_PATH, encoding="utf-8") as f:
            return json.load(f).get("settings", [])
    except (OSError, ValueError):
        return []


_ITEM_NAMES: "dict[str, str] | None" = None


def load_item_names() -> "dict[str, str]":
    """template_id -> display name, lazily loaded + cached from item-data.json
    (the same catalogue admin_market uses). Empty dict if the file is missing."""
    global _ITEM_NAMES
    if _ITEM_NAMES is None:
        try:
            with open(os.path.join(BASE_DIR, "data", "admin", "item-data.json"), encoding="utf-8") as f:
                data = json.load(f)
            names = data.get("names") or {}
            if not names:
                names = {k: (v.get("name") or "") for k, v in (data.get("items") or {}).items()}
            _ITEM_NAMES = {str(k): str(v) for k, v in names.items()}
        except (OSError, ValueError):
            _ITEM_NAMES = {}
    return _ITEM_NAMES


def _read_ini_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_setting_value(st: dict) -> str | None:
    """Current INI value for a setting, or None if unset / file missing. For a
    quoted string setting the surrounding double quotes are stripped so the UI
    shows the bare value."""
    path = INI_FILES.get(st.get("file") or "")
    if not path:
        return None
    text = _read_ini_text(path)
    if text is None:
        return None
    section = st.get("section")
    if section is None:
        val = admin_ini_merge.read_flat(text, st["key"])
    else:
        val = admin_ini_merge.read_keyed(text, section, st["key"])
    if val is not None and st.get("quoted") and len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        val = val[1:-1]
    return val


def pin_setting_to_egg(st: dict, value):
    """Keep a setting's Pelican variable in step with the INI it writes.

    Returns None when the setting has no egg variable (171 of the 195 are
    panel-only and always survive a boot), else (ok, detail).

    apply-config.sh rewrites every env-backed setting from its variable at
    boot, so without this the INI write is undone by the next restart —
    including the scheduler's unattended one, where nobody is watching."""
    env = st.get("env")
    if not env:
        return None
    rendered = admin_ini_merge.normalize_value(value, st["type"], st.get("enum"))
    return admin_pelican.set_variable(env, rendered)


def blank_submission_error(st: dict, value) -> "str | None":
    """Refuse a whitespace-only value for a setting where blank is meaningful.

    normalize_value strips, so "   " collapses to "" — and for an empty_ok
    setting (issue #107: blank join password = PUBLIC server) that silent
    collapse would drop the password on a stray-space paste. The old
    `required` egg rule caught this by accident; now the API accepts only
    the literal empty field as the explicit "clear" gesture. Returns the
    refusal message, or None when the value is fine."""
    if not st.get("empty_ok"):
        return None
    s = "" if value is None else str(value)
    if s and not s.strip():
        return ("whitespace-only value — submit an empty field to "
                "intentionally clear it")
    return None


def write_setting(st: dict, value) -> None:
    """Validate + write one setting to its INI sink. Raises ValueError on an
    invalid value or a missing target file."""
    norm = admin_ini_merge.normalize_value(value, st["type"], st.get("enum"))
    rendered = admin_ini_merge.render_value(norm, st.get("quoted", False))
    if rendered is None:
        raise ValueError("value contains a double quote, which UE5 cannot parse")
    path = INI_FILES.get(st.get("file") or "")
    if not path or not os.path.isfile(path):
        raise ValueError(f"target INI sink {st.get('file')!r} is missing")
    # Read-modify-write of a file two operators can be editing at once —
    # take COMMAND_LOCK so the second save can't be computed from the text
    # the first one is already replacing.
    with COMMAND_LOCK:
        text = _read_ini_text(path) or ""
        section = st.get("section")
        if section is None:
            new = admin_ini_merge.upsert_flat(text, st["key"], rendered)
        else:
            new = admin_ini_merge.upsert_keyed(text, section, st["key"], rendered)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)


# --------------------------------------------------------------------------
# Database-tab helpers (Phase 1 port of Icehunter/dune-admin, MIT).
#
# is_read_only_sql is lifted verbatim from dune-admin
# cmd/dune-admin/handlers_database.go:11-22 — strip comments, lowercase,
# and only accept queries that start with select/explain/show/with so a
# mutating statement never reaches Postgres. The db-sql subcommand ALSO
# pins a read-only psql session as a second layer; this guard rejects
# early with a clear 400. See ATTRIBUTION.md.
# --------------------------------------------------------------------------
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_READONLY = re.compile(r"^(select|explain|show|with)[\s(]")


def is_read_only_sql(sql: str) -> bool:
    """True if sql is a read-only statement (SELECT/EXPLAIN/SHOW/WITH)."""
    s = _SQL_BLOCK_COMMENT.sub(" ", sql)
    s = _SQL_LINE_COMMENT.sub(" ", s)
    s = s.strip().lower()
    return bool(_SQL_READONLY.match(s))


def csv_to_table(text: str, truncate: "int | None" = None) -> dict:
    """Parse psql --csv output (header row + data rows) into the Database-tab
    response shape {headers, rows, truncated}. Values stay strings exactly as
    psql emits them (SQL NULL -> empty field), matching dune-admin's
    stringified table response."""
    parsed = list(csv.reader(io.StringIO(text)))
    headers = parsed[0] if parsed else []
    body = parsed[1:] if len(parsed) > 1 else []
    truncated = False
    if truncate is not None and len(body) > truncate:
        body = body[:truncate]
        truncated = True
    return {"headers": headers, "rows": body, "truncated": truncated}


# --------------------------------------------------------------------------
# Data-file readers (cached at module load). Used by /api/lookup/* so the
# UI doesn't re-shell python each time.
# --------------------------------------------------------------------------
def _read_data(name: str) -> list[dict]:
    path = Path(BASE_DIR) / "data" / "admin" / f"{name}.json"
    if not path.is_file():
        return []
    try:
        with path.open() as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log(f"WARN failed to load {path}: {exc}")
        return []


_DATA = {
    "vehicles": _read_data("vehicles"),
    "items": _read_data("items"),
    "skills": _read_data("skill-modules"),
}


def fetch_steam_info(steam_ids: list[str]) -> dict[str, dict]:
    """Look up Steam personanames + avatars for a batch of 64-bit ids.

    Returns {steam_id: {personaname, profileurl, avatar, avatarfull,
    realname?, lastlogoff?, personastate}} for ids we managed to fetch.
    Hits the in-memory cache first; only the cache-misses go out to
    api.steampowered.com (one call per up-to-100-id batch).
    """
    if not STEAM_API_KEY or not steam_ids:
        return {}
    now = time.time()
    out: dict[str, dict] = {}
    misses: list[str] = []
    for sid in steam_ids:
        cached = STEAM_CACHE.get(sid)
        if cached and (now - cached[0]) < STEAM_CACHE_TTL_SECONDS:
            out[sid] = cached[1]
        else:
            misses.append(sid)
    # Steam allows up to 100 ids per call; chunk for safety.
    for i in range(0, len(misses), 100):
        chunk = misses[i:i + 100]
        url = (
            "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
            f"?key={STEAM_API_KEY}&steamids={','.join(chunk)}"
        )
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                payload = json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            log(f"WARN Steam API fetch failed for {len(chunk)} ids: {exc}")
            continue
        for player in (payload.get("response") or {}).get("players") or []:
            sid = str(player.get("steamid", ""))
            if not sid:
                continue
            info = {
                "personaname": player.get("personaname"),
                "profileurl": player.get("profileurl"),
                "avatar": player.get("avatar"),
                "avatarmedium": player.get("avatarmedium"),
                "avatarfull": player.get("avatarfull"),
                "realname": player.get("realname"),
                "personastate": player.get("personastate"),
                "lastlogoff": player.get("lastlogoff"),
            }
            STEAM_CACHE[sid] = (now, info)
            out[sid] = info
    return out


def search_data(name: str, query: str, limit: int, category: str = "") -> list[dict]:
    """Search the bundled catalogue. `category` (when set) restricts to
    rows whose `category` field matches case-insensitively. `query`
    further narrows by substring across id+name+category."""
    rows = _DATA.get(name, [])
    if category:
        cat = category.lower()
        rows = [r for r in rows if r.get("category", "").lower() == cat]
    if not query:
        return rows[:limit]
    needle = query.lower()
    matches = [
        row
        for row in rows
        if needle in row.get("id", "").lower()
        or needle in row.get("name", "").lower()
        or needle in row.get("category", "").lower()
    ]
    return matches[:limit]


def list_categories(name: str) -> list[dict]:
    """Distinct (category, count) pairs from the bundled dataset, sorted
    by count descending. Used by the SPA's category-pill row."""
    rows = _DATA.get(name, [])
    buckets: dict[str, int] = {}
    for r in rows:
        cat = r.get("category") or ""
        if cat:
            buckets[cat] = buckets.get(cat, 0) + 1
    return sorted(
        ({"id": cat, "count": n} for cat, n in buckets.items()),
        key=lambda d: -d["count"],
    )


def armor_sets() -> list[dict]:
    """Group every wearable armor piece by its set base. Returns one
    entry per set with the constituent slot pieces inline, so the SPA
    can render each as its own kit without round-tripping per piece.

    Set base = item id with the trailing slot suffix stripped (_Top,
    _Bottom, _Helmet, _Gloves, _Boots, _Mask). Sets with fewer than 3
    slots are skipped — those are usually accessories or test rows.

    MTX_* and *Schematic rows are filtered out so the SPA never tries
    to grant a microtransaction skin or a recipe permit by mistake.
    """
    SLOTS = ["_Top", "_Bottom", "_Helmet", "_Gloves", "_Boots", "_Mask"]
    items = _DATA.get("items", [])
    sets: dict[str, list[dict]] = {}
    for it in items:
        if it.get("category") != "clothing":
            continue
        iid = it.get("id") or ""
        low = iid.lower()
        if "mtx_" in low or "schematic" in low or "recipe" in low:
            continue
        for slot in SLOTS:
            if iid.endswith(slot):
                base = iid[: -len(slot)]
                sets.setdefault(base, []).append({
                    "id": iid,
                    "name": it.get("name") or iid,
                    "slot": slot.lstrip("_"),
                })
                break
    out = [
        {"base": base, "pieces": sorted(pieces, key=lambda p: p["slot"])}
        for base, pieces in sets.items()
        if len(pieces) >= 3
    ]
    out.sort(key=lambda s: s["base"])
    return out


def search_skills(query: str, limit: int, category: str = "") -> list[dict]:
    """Skill lookup. Same filter shape as search_data but also matches
    the type segment of the skill id (e.g. 'Ability' / 'Attribute' /
    'Perk') against the query for convenience."""
    rows = _DATA.get("skills", [])
    if category:
        cat = category.lower()
        rows = [r for r in rows if r.get("category", "").lower() == cat]
    if not query:
        return rows[:limit]
    needle = query.lower()
    matches = [
        row
        for row in rows
        if needle in row.get("id", "").lower()
        or needle in row.get("name", "").lower()
        or needle in row.get("category", "").lower()
    ]
    return matches[:limit]


# --------------------------------------------------------------------------
# Handler.
# --------------------------------------------------------------------------
USAGE_PLAIN = (
    "admin-http — Pelican Dune Awakening admin HTTP backend\n"
    f"Mode: {'UI' if UI_ENABLED else 'internal'}\n"
    f"Listening on {LISTEN_ADDR}:{LISTEN_PORT}\n\n"
    "When UI mode is enabled, browse the SPA at /; the JSON API lives at /api/* and /admin/*.\n"
    "When in internal mode, POST /admin/<sub> with a JSON body — see scripts/admin-publish.sh for subcommands.\n"
)


# C0/C1 control characters and DEL. The request line reaches the log
# verbatim, so a probe can otherwise plant terminal escape sequences in
# admin-http.log — which the operator reads with tail, and the SPA's log
# viewer renders. Printable Unicode is left alone (player names in URLs).
_LOG_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class Handler(BaseHTTPRequestHandler):
    server_version = "DuneAdminHTTP/1.1"

    # socketserver.StreamRequestHandler applies this to the connection.
    # Without it a peer that connects and then says nothing owns its
    # worker until the container restarts (issue #89).
    timeout = CONNECTION_TIMEOUT_SECONDS

    # ------ low-level write helpers --------------------------------------
    def _cors_headers(self) -> None:
        # CORS is OPT-IN by DUNE_ADMIN_UI_DOMAIN. The previous behaviour of
        # reflecting any Origin with Access-Control-Allow-Credentials: true
        # was a classic open-CORS bug — any malicious site the operator
        # visited could read /api/* responses cross-origin. We now ONLY
        # echo the origin when it matches one of the operator-declared
        # domains, and the same-origin SPA needs no CORS headers anyway.
        origin = self.headers.get("Origin", "")
        if UI_DOMAIN and origin:
            wanted = {f"https://{UI_DOMAIN}", f"http://{UI_DOMAIN}"}
            if origin in wanted:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _write(
        self,
        status: int,
        payload,
        content_type: str = "application/json",
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload, indent=2)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(content_type)
        self._cors_headers()
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self, content_type: str) -> None:
        """Attach OWASP-recommended response headers to every reply.

        CSP applies to HTML responses (the SPA); JSON / static assets
        get the cheaper subset of headers since CSP on them is moot.
        HSTS is only set when we believe TLS is terminating in front
        (UI_DOMAIN set + non-loopback bind) — browsers would otherwise
        pin a plain-HTTP loopback to HTTPS and lock the operator out.
        """
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
        )
        if UI_DOMAIN and LISTEN_ADDR not in _LOOPBACK_ADDRS:
            self.send_header(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        if content_type.startswith("text/html"):
            # The SPA loads Steam avatars + the Awakening wiki for icons;
            # tailwind generates inline style for utility classes so we
            # allow 'unsafe-inline' there only. No inline scripts.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: "
                "https://awakening.wiki https://media.awakening.wiki "
                "https://steamcdn-a.akamaihd.net https://avatars.steamstatic.com "
                "https://steamcommunity-a.akamaihd.net; "
                "connect-src 'self' https://api.awakening.wiki; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'",
            )

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        """Every log line the base class emits funnels through here —
        including log_error() from inside send_error(), which runs BEFORE
        the response is written.

        So this must not raise. `command` and `path` only exist once
        parse_request() has accepted the request line; a malformed one
        (scanner junk, a TLS ClientHello onto the plaintext port, a read
        timeout) reaches send_error() while they are still unset, and
        dereferencing them there aborted the 400 the client should have
        received and dumped a traceback into admin-http.log for every
        probe. Hence getattr fallbacks, a guarded %-format, and the peer
        address so the operator can see who is knocking."""
        command = getattr(self, "command", None) or "-"
        path = getattr(self, "path", None) or "-"
        peer = self.client_address[0] if getattr(self, "client_address", None) else "-"
        try:
            detail = format % args
        except (TypeError, ValueError):
            detail = f"{format} {args}"
        log(_LOG_CONTROL_CHARS.sub("?", f"{peer} {command} {path} -> {detail}"))

    # ------ auth ---------------------------------------------------------
    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        return header[7:].strip()

    def _cookie_value(self, name: str) -> str:
        """Parse a single cookie value out of the Cookie header. No
        external dep on http.cookies — that module mutates state and
        we want a pure read."""
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        for piece in raw.split(";"):
            piece = piece.strip()
            if "=" not in piece:
                continue
            k, v = piece.split("=", 1)
            if k.strip() == name:
                return v.strip()
        return ""

    def _session_token_from_request(self) -> tuple[str, str]:
        """Returns (token, source) where source is 'cookie' or 'bearer'
        depending on which path supplied the session. The source matters
        for CSRF — cookie-borne sessions need the X-CSRF-Token header
        check; Bearer-borne sessions are inherently CSRF-safe (cross-
        origin JS can't set Authorization headers on credentialed
        fetches that the browser would attach automatically)."""
        cookie_tok = self._cookie_value(SESSION_COOKIE_NAME)
        if cookie_tok:
            return cookie_tok, "cookie"
        return self._bearer(), "bearer"

    def _auth_ok(self) -> bool:
        token, _ = self._session_token_from_request()
        # UI mode: session token issued by /api/login
        if UI_ENABLED:
            return verify_session_token(token) is not None
        # Internal mode: optional shared-secret Bearer
        if not SHARED_AUTH:
            return True
        return token == SHARED_AUTH

    def _csrf_ok(self) -> bool:
        """Validate CSRF for cookie-based callers. Returns True if:
          - request is Bearer-authenticated (no CSRF needed)
          - OR request is cookie-authenticated AND X-CSRF-Token header
            matches the csrf field embedded in the session payload.
        Internal mode (non-UI) is always OK — its auth surface is
        either loopback-only or a shared-secret Bearer.
        """
        if not UI_ENABLED:
            return True
        token, source = self._session_token_from_request()
        if source == "bearer":
            return True
        payload = verify_session_token(token)
        if not payload:
            return False
        expected = payload.get("csrf")
        if not isinstance(expected, str) or not expected:
            # Legacy token without csrf field. Reject mutating cookie-
            # auth requests so an attacker can't ride a pre-rollout
            # session past the CSRF gate.
            return False
        supplied = self.headers.get("X-CSRF-Token", "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _client_ip(self) -> str:
        """Source IP for rate-limit bucketing — see resolve_client_ip()."""
        try:
            peer = self.client_address[0]
        except (AttributeError, IndexError):
            return "unknown"
        return resolve_client_ip(peer, self.headers.get("X-Forwarded-For", ""))

    def _forwarded_proto(self) -> str:
        """The scheme a DECLARED proxy says the browser used, or "" when
        nobody we trust has told us. An undeclared peer's header is
        ignored for the same reason its X-Forwarded-For is."""
        try:
            peer = self.client_address[0]
        except (AttributeError, IndexError):
            return ""
        if not _is_trusted_proxy(peer):
            return ""
        proto = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        return proto if proto in ("http", "https") else ""

    def _cookie_secure(self) -> bool:
        return cookie_secure(self._forwarded_proto())

    def _has_forwarding_headers(self) -> bool:
        """Whether SOMETHING proxied this request. Used only to tell an
        undeclared-proxy deployment apart from a browser talking to us
        directly — the difference between 'declare your proxy' and 'this
        login cannot possibly work'."""
        return any(self.headers.get(h) for h in
                   ("X-Forwarded-Proto", "X-Forwarded-For", "X-Forwarded-Host", "Forwarded"))

    def _warn_if_cookie_will_be_dropped(self) -> None:
        """A Secure cookie handed to a browser that reached us over plain
        HTTP is discarded on arrival: /api/login answers 200, the SPA
        never sees a session, and the operator bounces back to the login
        screen with no error anywhere. Say so, since the failure is
        otherwise completely silent."""
        if not self._cookie_secure() or self._forwarded_proto() == "https":
            return
        peer = self._client_ip()
        if self._has_forwarding_headers():
            log(f"WARN login from {peer}: issuing a Secure cookie, but no declared proxy "
                "vouched for https. If a reverse proxy fronts this panel, add its address "
                "to DUNE_ADMIN_UI_TRUSTED_PROXIES so the scheme can be trusted.")
        else:
            log(f"WARN login from {peer} arrived over plain HTTP with no proxy in front, "
                "and DUNE_ADMIN_UI_DOMAIN is set — the browser WILL DISCARD the session "
                "cookie and the login will not stick. Serve the panel over https, or set "
                "DUNE_ADMIN_UI_TLS=off if this panel is genuinely plain HTTP.")

    def _cookie_flags(self) -> str:
        """Cookie attribute string shared by session + csrf cookies."""
        bits = [
            "Path=/",
            f"Max-Age={SESSION_TTL_SECONDS}",
            "SameSite=Strict",
        ]
        if self._cookie_secure():
            bits.append("Secure")
        return "; ".join(bits)

    def _session_cookies(self, token: str, csrf: str) -> list[tuple[str, str]]:
        """Two Set-Cookie headers to emit on /api/login success."""
        common = self._cookie_flags()
        return [
            ("Set-Cookie", f"{SESSION_COOKIE_NAME}={token}; HttpOnly; {common}"),
            ("Set-Cookie", f"{CSRF_COOKIE_NAME}={csrf}; {common}"),
        ]

    def _clear_session_cookies(self) -> list[tuple[str, str]]:
        """Set-Cookie headers that expire the session + csrf cookies. The
        attributes must match the ones the cookie was set with or the
        browser keeps the original and logout does nothing."""
        bits = ["Path=/", "Max-Age=0", "SameSite=Strict"]
        if self._cookie_secure():
            bits.append("Secure")
        common = "; ".join(bits)
        return [
            ("Set-Cookie", f"{SESSION_COOKIE_NAME}=; HttpOnly; {common}"),
            ("Set-Cookie", f"{CSRF_COOKIE_NAME}=; {common}"),
        ]

    # ------ OPTIONS preflight --------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ------ GET ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)

        # Public endpoints (no auth required).
        if path in ("/healthz", "/api/healthz"):
            self._write(200, {"ok": True, "mode": "ui" if UI_ENABLED else "internal"})
            return
        if path == "/usage":
            self._write(200, USAGE_PLAIN, content_type="text/plain")
            return
        if path == "/api/config":
            # Lets the SPA learn what mode the backend is in + whether it
            # needs to show a login screen. No secrets returned.
            self._write(
                200,
                {
                    "ui_enabled": UI_ENABLED,
                    "domain": UI_DOMAIN or None,
                    "session_ttl_seconds": SESSION_TTL_SECONDS,
                    "version": "1.1",
                },
            )
            return

        # Static SPA assets must stay public — they're the login page itself,
        # plus its bundled CSS/JS. The token isn't issued until the user
        # POSTs /api/login, so we can't gate the HTML that hosts that form.
        # The history-router fallback inside _serve_static also keeps deep
        # links like /dashboard working.
        if UI_ENABLED and not path.startswith("/api/") and not path.startswith("/admin/"):
            self._serve_static(path)
            return

        # Anything else requires auth.
        if not self._auth_ok():
            self._write(401, {"error": "auth required"})
            return

        # Live map: player markers (positions) on one map. Map key is
        # whitelisted in admin_map; the position read runs in admin-publish.
        if path == "/api/map/markers":
            map_key = (query.get("map") or [""])[0]
            if not admin_map.validate_map_key(map_key):
                self._write(400, {"error": "map query param required: " + "|".join(admin_map.MAP_KEYS)})
                return
            entry = run_publish(["map-markers", map_key], timeout=10)
            if entry["exit_code"] == 3:  # tables missing — same capability-gap shape as the player reads
                self._write(200, {"ok": True, "map": map_key, "markers": [],
                                  "available": False, "detail": (entry.get("stderr") or "").strip()[:300]})
                return
            if entry["exit_code"] != 0:
                self._write(502, {"error": "map markers read failed",
                                  "detail": (entry.get("stderr") or "").strip()[:300]})
                return
            self._write(200, {"ok": True, "map": map_key,
                              "markers": admin_map.parse_markers(entry["stdout"])})
            return

        # Saved teleport locations (operator-defined named coords).
        if path == "/api/map/locations":
            self._write(200, {"ok": True, "locations": admin_locations.load_locations(BASE_DIR)})
            return

        # Unattended scheduler: config + status + run history.
        if path == "/api/schedule":
            self._write(200, run_schedule("status", timeout=15).get("data", {}))
            return
        if path == "/api/tasks/runs":
            limit = (query.get("limit") or ["50"])[0]
            self._write(200, run_schedule("runs", limit if limit.isdigit() else "50", timeout=15).get("data", {}))
            return

        # Phase 4: live server status grid — mock-k8s per-map instance/scale
        # status merged with per-map LIVE connected-player counts (farm_state via
        # admin-publish farm-player-count; admin-http holds no PG connection). Uses
        # farm-player-count, not server-status, so the counts key on the same map
        # names as mock-k8s (SH_HarkoVillage etc.) — server-status keys on the
        # character's actor map ("HarkoVillage") and would show a stray orphan row.
        # A missing source degrades gracefully and is flagged in `sources`.
        if path == "/api/status":
            mock = fetch_mock_status(MOCK_K8S_PORT)
            entry = run_publish(["farm-player-count"], timeout=10)
            counts = parse_player_counts(entry["stdout"]) if entry["ok"] else {}
            grid = merge_status(mock or {}, counts)
            grid["ok"] = True
            grid["sources"] = {"mockK8s": mock is not None, "playerCounts": entry["ok"]}
            if mock is None:
                grid["warning"] = "mock-k8s /status unreachable; showing player counts only"
            self._write(200, grid)
            return

        # Connection doctor: read-only diagnosis of the "boots fine, nobody
        # can join" family (advertised IP drift, port collisions, stuck
        # READY, silent heartbeat). Runs on demand — it curls the public-IP
        # service, so it is not polled.
        if path == "/api/doctor":
            entry = run_publish(["doctor"], timeout=30)
            data = {}
            try:
                data = json.loads(entry.get("stdout") or "{}")
            except ValueError:
                pass
            self._write(200, {"ok": bool(entry.get("ok")) and bool(data.get("ok")),
                              "summary": data.get("summary", {}),
                              "checks": data.get("checks", []),
                              "facts": data.get("facts", {}),
                              "error": data.get("error", ""),
                              "context": data.get("context", ""),
                              "stderr": entry.get("stderr", "")[:300]})
            return

        # Claimed bases (Red-Blink listBases port): owner, map, piece and
        # placeable counts. ?q= filters on owner name or map.
        if path == "/api/bases":
            q = (query.get("q", [""])[0] if isinstance(query, dict) else "") or ""
            entry = run_publish(["bases", q] if q else ["bases"], timeout=20)
            if entry.get("exit_code") == 3:
                self._write(200, {"ok": True, "available": False,
                                  "reason": "bases tables not present on this build"})
                return
            self._write(200, {"ok": bool(entry.get("ok")), "available": True,
                              "bases": _csv_rows(entry.get("stdout") or ""),
                              "stderr": entry.get("stderr", "")[:300]})
            return

        # Per-type water storage of one base.
        if path.startswith("/api/bases/") and path.endswith("/water"):
            bid = path[len("/api/bases/"):-len("/water")]
            if not bid.isdigit():
                self._write(400, {"error": "base id must be numeric"})
                return
            entry = run_publish(["base-water", bid], timeout=15)
            if entry.get("exit_code") == 3:
                self._write(200, {"ok": True, "available": False,
                                  "reason": "bases tables not present on this build"})
                return
            self._write(200, {"ok": bool(entry.get("ok")), "available": True,
                              "water": _csv_rows(entry.get("stdout") or ""),
                              "stderr": entry.get("stderr", "")[:300]})
            return

        # World-partition topology (warm dim=0 + dimensional dim>0) joined to
        # farm_state liveness. Powers the Instances tab alongside /api/status.
        if path == "/api/partitions":
            entry = run_publish(["world-partition-list"], timeout=15)
            if not entry["ok"]:
                self._write(200, {"ok": False, "error": (entry.get("stderr") or "").strip() or "world-partition-list failed"})
                return

            def _b(v: str) -> bool:
                return v.strip().lower() in ("t", "true")

            # Parked sietches: an offline (server_id NULL) Survival_1 dim>0 row that is in
            # the parked set is intentionally PAUSED (data kept) vs a plain offline/cold row.
            parked = admin_park.parked_ids(BASE_DIR)
            parts = []
            for line in (entry.get("stdout") or "").splitlines():
                line = line.strip()
                if not line or line.startswith("publish=") or line.startswith("["):
                    continue
                f = line.split("|")
                if len(f) < 10 or not f[0].isdigit():
                    continue
                pid, mp, dim, label, blocked, sid, gport, ready, alive, players = f[:10]
                parts.append({
                    "partition_id": int(pid), "map": mp,
                    "dimension": int(dim) if dim.isdigit() else 0,
                    "label": label, "blocked": _b(blocked),
                    "server_id": sid or None,
                    "game_port": int(gport) if gport.isdigit() else None,
                    "ready": _b(ready), "alive": _b(alive),
                    "players": int(players) if players.isdigit() else 0,
                    "parked": int(pid) in parked,
                })
            self._write(200, {"ok": True, "partitions": parts})
            return

        # Per-sietch config: current overrides + the catalogue of overridable
        # settings (for the editor). GET /api/sietches/<pid>/config
        if path.startswith("/api/sietches/") and path.endswith("/config"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            pid_str = path[len("/api/sietches/"):-len("/config")]
            if not pid_str.isdigit():
                self._write(200, {"ok": False, "error": "sietch partition id must be numeric"})
                return
            self._write(200, {
                "ok": True,
                "overrides": run_sietch("get", pid_str).get("data", {}).get("overrides", {}),
                "settings": run_sietch("capable").get("data", {}).get("settings", []),
            })
            return

        # Phase 5: server-settings catalogue + current values, grouped by
        # category. `verified:false` entries are mapped on the proven
        # UserGame.ini mechanism but not yet game-effect-confirmed.
        if path == "/api/settings":
            categories: dict[str, list] = {}
            schema = load_settings_schema()
            for st in schema:
                cur = read_setting_value(st)
                cat = st.get("category", "Other")
                categories.setdefault(cat, []).append({
                    "id": st["id"],
                    "label": st.get("label", st["id"]),
                    "type": st["type"],
                    "default": st.get("default"),
                    "enum": st.get("enum"),
                    "value": cur,
                    "isDefault": cur is None,
                    "verified": st.get("verified", False),
                    "section": st.get("section"),
                    "key": st.get("key"),
                    "clientGated": st.get("client_gated", "no") == "yes",
                    "advanced": bool(st.get("advanced", False)),
                })
            self._write(200, {
                "ok": True,
                "count": len(schema),
                "categories": categories,
                "note": "changes take effect on the next server restart",
            })
            return

        # Phase 6: welcome-kit config + ledger status + recent grants.
        if path == "/api/welcome":
            status = run_welcome("status", timeout=15)
            grants = run_welcome("list", timeout=15)
            self._write(200, {
                "ok": True,
                "config": status.get("data", {}),
                "grants": grants.get("data", []),
            })
            return

        # Phase 7a: market-bot config + catalog stats + live order summary
        # (read-only — pricing engine, no economy writes). Optional price
        # preview via ?template=<id>&grade=<n>.
        if path == "/api/market":
            # via admin_market.load_config so the (persistent server/state/) config
            # path stays single-sourced — never re-hardcode data/admin here.
            cfg = admin_market.load_config(BASE_DIR)
            cfg.pop("_comment", None)
            out = {
                "ok": True,
                "config": cfg,
                "catalog": run_market("catalog-stats").get("data", {}),
                "orders": run_market("orders").get("data", {}),
            }
            tmpl = (query.get("template") or [""])[0]
            if tmpl:
                grade = (query.get("grade") or ["0"])[0]
                out["price"] = run_market("price", tmpl, grade if grade.isdigit() else "0").get("data", {})
            self._write(200, out)
            return

        # Market-bot (7b) status: bot / NPC / player order counts + bot identity.
        if path == "/api/market/bot":
            entry = run_publish(["market-bot-status"], timeout=15)
            out: dict = {"ok": entry["ok"], "bot_orders": 0, "npc_orders": 0, "player_orders": 0}
            for ln in (entry.get("stdout") or "").splitlines():
                ln = ln.strip()
                if ln.startswith("market-bot "):
                    for kv in ln.split()[1:]:
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            out[k] = v
                elif "," in ln:
                    parts = [p.strip() for p in ln.split(",")]
                    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                        out["bot_orders"], out["npc_orders"], out["player_orders"] = int(parts[0]), int(parts[1]), int(parts[2])
            self._write(200, out)
            return

        # Demand-based autoscaler status: config + recent scale actions + per-map
        # timers + the director.log read offset. Read-only.
        if path == "/api/autoscaler":
            r = run_autoscaler("status", timeout=20)
            self._write(200, r.get("data", {"ok": False, "error": "status failed"}))
            return

        # Player-editor: progression preset catalog (journey-node bundles).
        # Read-only static catalog; POST .../progression-unlock|-lock applies it.
        if path == "/api/progression/presets":
            presets = []
            try:
                with open(os.path.join(BASE_DIR, "data", "admin", "progression-presets.json"), encoding="utf-8") as f:
                    presets = json.load(f).get("presets", [])
            except (OSError, ValueError):
                pass
            self._write(200, {"ok": True, "presets": presets})
            return

        # Spicefield economy snapshot: one row per (field_type, map, dimension)
        # with spawn toggle + live active/primed vs caps. Server-wide economy.
        if path == "/api/spice":
            entry = run_publish(["spice-list"], timeout=10)
            if not entry["ok"]:
                self._write(200, {"ok": False, "error": (entry.get("stderr") or "").strip() or "spice-list failed"})
                return
            table = csv_to_table(entry.get("stdout") or "")

            def _i(r: dict, k: str) -> int:
                try:
                    return int(r.get(k) or 0)
                except (ValueError, TypeError):
                    return 0

            fields = []
            for raw in table["rows"]:
                r = dict(zip(table["headers"], raw))
                fields.append({
                    "id": _i(r, "spicefield_type_id"),
                    "field_type": r.get("field_type", ""),
                    "map": r.get("map_name", ""),
                    "dimension": _i(r, "dimension_index"),
                    "spawning": (r.get("is_spawning_active", "").lower() in ("t", "true")),
                    "active": _i(r, "current_globally_active"),
                    "max_active": _i(r, "max_globally_active"),
                    "primed": _i(r, "current_globally_primed"),
                    "max_primed": _i(r, "max_globally_primed"),
                    "weight": r.get("global_spawn_weight", ""),
                })
            self._write(200, {"ok": True, "fields": fields})
            return

        if path == "/api/me":
            if UI_ENABLED:
                token, _ = self._session_token_from_request()
                payload = verify_session_token(token)
            else:
                payload = {"sub": "internal"}
            # Strip the csrf field from the response — the SPA reads
            # csrf from its own cookie, and we don't want to broadcast
            # the value in every /api/me poll.
            if isinstance(payload, dict):
                payload = {k: v for k, v in payload.items() if k != "csrf"}
            self._write(200, {"authenticated": True, "session": payload})
            return

        if path == "/api/history":
            try:
                requested = int(query.get("limit", ["50"])[0])
            except ValueError:
                requested = 50
            limit = max(1, min(HISTORY_PAGE_MAX, requested))
            entries, total = admin_history.recent(BASE_DIR, limit)
            self._write(200, {"entries": entries, "total": total})
            return

        # List the log sources the SPA may tail (fixed services + globbed ue5-*).
        # Operator-only — explicit auth even though the shared gate above already
        # applies, because logs can carry secrets.
        if path == "/api/logs/sources":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            self._write(200, {"ok": True, "sources": admin_logs.list_sources(LOGS_DIR)})
            return

        # Tail a single log, redacted. GET /api/logs?source=<svc>&tail=N. The
        # source is allowlist+shape validated and resolved under LOGS_DIR (no path
        # traversal); every returned line passes admin_logs.redact (secrets out).
        if path == "/api/logs":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            source = (query.get("source") or [""])[0]
            log_path = admin_logs.resolve_log_path(LOGS_DIR, source)
            if log_path is None:
                self._write(400, {"error": "unknown log source",
                                  "sources": [s["name"] for s in admin_logs.list_sources(LOGS_DIR)]})
                return
            try:
                tail_n = int((query.get("tail") or [str(admin_logs.LOG_TAIL_DEFAULT)])[0])
            except ValueError:
                tail_n = admin_logs.LOG_TAIL_DEFAULT
            lines, exists = admin_logs.tail_file(log_path, tail_n)
            self._write(200, {
                "ok": True, "source": source, "exists": exists,
                "lines": admin_logs.redact_lines(lines), "count": len(lines),
                "tail": max(1, min(admin_logs.LOG_TAIL_MAX, tail_n)),
            })
            return

        if path == "/api/lookup/vehicles":
            self._write(200, {"vehicles": _DATA["vehicles"]})
            return
        if path == "/api/lookup/item-categories":
            # Distinct categories + counts so the SPA can render
            # category-pill filters without shipping items.json
            # client-side.
            self._write(200, {"categories": list_categories("items")})
            return
        if path == "/api/lookup/items":
            q = query.get("q", [""])[0]
            cat = query.get("category", [""])[0]
            # Higher cap when browsing a category — Funcom ships ~300
            # weapons, ~280 garments etc., we want the full list.
            limit = max(1, min(500, int(query.get("limit", ["40"])[0])))
            self._write(200, {"items": search_data("items", q, limit, cat)})
            return
        if path == "/api/lookup/skill-categories":
            self._write(200, {"categories": list_categories("skills")})
            return
        if path == "/api/lookup/armor-sets":
            self._write(200, {"sets": armor_sets()})
            return
        if path == "/api/lookup/skills":
            q = query.get("q", [""])[0]
            cat = query.get("category", [""])[0]
            # 145 modules total — 200 is enough to show every category
            # in one go without pagination.
            limit = max(1, min(200, int(query.get("limit", ["50"])[0])))
            self._write(200, {"skills": search_skills(q, limit, cat)})
            return

        if path == "/api/players":
            sub_filter = query.get("filter", ["all"])[0]
            if sub_filter not in ("all", "online"):
                sub_filter = "all"
            entry = run_publish(["players", sub_filter], timeout=10)
            self._write(200, entry)
            return

        if path.startswith("/api/pos/"):
            # URL-decode so `name:Sergentval` survives a fetch() call from
            # the SPA (which encodeURIComponent's the colon to %3A).
            player = unquote(path[len("/api/pos/"):])
            entry = run_publish(["pos", player], timeout=10)
            self._write(200, entry)
            return

        # Per-player reads: /api/players/<id>/{state,inventory,char-xp,tags}.
        # <id> is URL-encoded (e.g. name%3AFoo); the online list stays on the
        # existing /api/players?filter=online. These sit after the auth gate.
        if path.startswith("/api/players/"):
            rest = path[len("/api/players/"):]
            if "/" in rest:
                pid_enc, sub = rest.rsplit("/", 1)
                player = unquote(pid_enc)
                if player and sub == "state":
                    self._handle_player_state(player)
                    return
                if player and sub == "inventory":
                    self._handle_player_inventory(player)
                    return
                if player and sub == "char-xp":
                    self._handle_player_char_xp(player)
                    return
                if player and sub == "tags":
                    self._handle_player_tags(player)
                    return
                if player and sub == "summary":
                    self._handle_player_summary(player)
                    return
                if player and sub == "char-backups":
                    # Newest-first backup list for this player. The subcommand
                    # resolves name:/steam: forms; the JSON is the helper's own
                    # (parsed out of stdout so the UI gets a typed list).
                    entry = run_publish(["char-backup-list", player], timeout=15)
                    data = {}
                    try:
                        data = json.loads(entry.get("stdout") or "{}")
                    except ValueError:
                        pass
                    self._write(200, {"ok": bool(entry.get("ok")) and bool(data.get("ok")),
                                      "backups": data.get("backups", []),
                                      "stderr": entry.get("stderr", "")[:300]})
                    return
            self._write(404, {"error": "unknown player sub-resource"})
            return

        if path == "/api/vehicles/list":
            entry = run_publish(["vehicle-list"], timeout=10)
            self._write(200, entry)
            return

        if path == "/api/steam-info":
            # Comma-separated 64-bit Steam IDs.
            raw_ids = query.get("ids", [""])[0]
            ids = [s.strip() for s in raw_ids.split(",") if s.strip().isdigit()]
            if not ids:
                self._write(400, {"error": "ids query param required (comma-separated SteamIDs)"})
                return
            if not STEAM_API_KEY:
                self._write(
                    200,
                    {
                        "enabled": False,
                        "players": {},
                        "hint": "Set STEAM_API_KEY in the Pelican panel to enable Steam persona/avatar lookup.",
                    },
                )
                return
            players = fetch_steam_info(ids)
            self._write(200, {"enabled": True, "players": players, "cached_entries": len(STEAM_CACHE)})
            return

        # Legacy text-plain players endpoint (kept for back-compat).
        if path == "/players":
            sub_filter = "online" if query.get("filter", [""])[0] == "online" else "all"
            entry = run_publish(["players", sub_filter], timeout=10)
            self._write(
                200,
                entry["stdout"] + entry["stderr"],
                content_type="text/plain",
            )
            return

        # Database tab (read-only). These sit after the do_GET auth gate.
        if path == "/api/database/tables":
            self._handle_db_tables()
            return
        if path == "/api/database/describe":
            self._handle_db_describe(query.get("table", [""])[0])
            return
        if path == "/api/database/sample":
            self._handle_db_sample(query.get("table", [""])[0], query.get("limit", ["20"])[0])
            return
        if path == "/api/database/search":
            self._handle_db_search(query.get("term", [""])[0])
            return
        if path == "/api/database/backups":
            self._db_reply(run_publish(["db-backup-list"], timeout=15))
            return

        if path == "/":
            self._write(200, USAGE_PLAIN, content_type="text/plain")
            return
        self._write(404, {"error": "not found", "path": path})

    def _serve_static(self, path: str) -> None:
        # SPA history-router fallback: any URL that doesn't map to an
        # existing file resolves to index.html so client-side routing works.
        # Normalize to prevent directory traversal.
        rel = path.lstrip("/") or "index.html"
        candidate = (UI_WEB_ROOT / rel).resolve()
        try:
            candidate.relative_to(UI_WEB_ROOT)
        except ValueError:
            self._write(403, {"error": "forbidden"})
            return
        if not candidate.is_file():
            # Fallback to index.html so React Router handles the URL.
            candidate = UI_WEB_ROOT / "index.html"
            if not candidate.is_file():
                self._write(
                    503,
                    {
                        "error": "SPA not built",
                        "hint": f"DUNE_ADMIN_UI_WEB_ROOT={UI_WEB_ROOT} contains no index.html. Build the React app and copy dist/ here, or deploy a fresh egg install.",
                    },
                )
                return
        ctype, _ = mimetypes.guess_type(str(candidate))
        ctype = ctype or "application/octet-stream"
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            self._write(500, {"error": "io", "detail": str(exc)})
            return
        self._write(200, data, content_type=ctype)

    # ------ Database tab (read-only inspection) --------------------------
    # Ported from Icehunter/dune-admin handlers_database.go (MIT). Each
    # handler shells out to a db-* subcommand (CSV out) via run_publish and
    # returns {headers, rows, truncated}. The GET routes sit after the
    # do_GET auth gate; the db-sql POST route is behind auth+csrf.
    def _db_reply(self, entry: dict, truncate: "int | None" = None) -> None:
        if entry["ok"]:
            self._write(200, csv_to_table(entry["stdout"], truncate))
            return
        detail = (entry.get("stderr") or "").strip()[:400]
        self._write(502, {"error": "db query failed", "detail": detail})

    def _handle_db_tables(self) -> None:
        self._db_reply(run_publish(["db-tables"], timeout=15))

    def _handle_db_describe(self, table: str) -> None:
        if not table:
            self._write(400, {"error": "table required"})
            return
        self._db_reply(run_publish(["db-describe", table], timeout=15))

    def _handle_db_sample(self, table: str, limit: str) -> None:
        if not table:
            self._write(400, {"error": "table required"})
            return
        self._db_reply(run_publish(["db-sample", table, limit], timeout=15))

    def _handle_db_search(self, term: str) -> None:
        if not term:
            self._write(400, {"error": "term required"})
            return
        self._db_reply(run_publish(["db-search", term], timeout=15))

    def _handle_db_sql(self, sql: str) -> None:
        sql = (sql or "").strip()
        if not sql:
            self._write(400, {"error": "sql required"})
            return
        if not is_read_only_sql(sql):
            self._write(400, {"error": "only SELECT, EXPLAIN, SHOW, and WITH statements are allowed"})
            return
        # dune-admin truncates the result set at 200 rows for the UI.
        self._db_reply(run_publish(["db-sql", sql], timeout=20), truncate=200)

    # ------ Players-tab reads -------------------------------------------
    # Player-read subcommands signal via exit code: 0 = data (CSV on stdout),
    # 3 = the targeted Funcom table isn't present on this build (a capability
    # gap, not an error), 1/2 = no such player / offline / usage. _player_reply
    # maps those to responses and returns the parsed table only on success.
    def _player_reply(self, entry: dict, truncate: "int | None" = None) -> "dict | None":
        code = entry["exit_code"]
        if code == 0:
            return csv_to_table(entry["stdout"], truncate)
        detail = (entry.get("stderr") or "").strip()[:400]
        if code == 3:
            self._write(200, {
                "available": False, "detail": detail,
                "headers": [], "rows": [], "truncated": False,
            })
        elif code in (1, 2):
            self._write(404, {"error": "player not found or unavailable", "detail": detail})
        else:
            self._write(502, {"error": "player read failed", "detail": detail})
        return None

    def _handle_player_state(self, player: str) -> None:
        table = self._player_reply(run_publish(["player-state", player], timeout=10))
        if table is not None:
            self._write(200, table)

    def _handle_player_inventory(self, player: str) -> None:
        table = self._player_reply(run_publish(["inventory-list", player], timeout=15))
        if table is None:
            return
        # Enrich each row with a human item name from the catalogue, inserted
        # right after template_id, so the SPA can show names without bundling
        # the 1658-item catalogue client-side.
        headers = table.get("headers", [])
        names = load_item_names()
        if "template_id" in headers and "name" not in headers and names:
            ti = headers.index("template_id")
            table["headers"] = headers[:ti + 1] + ["name"] + headers[ti + 1:]
            table["rows"] = [
                (r[:ti + 1] + [names.get(str(r[ti]), "")] + r[ti + 1:]) if ti < len(r) else r
                for r in table.get("rows", [])
            ]
        self._write(200, table)

    def _handle_player_tags(self, player: str) -> None:
        table = self._player_reply(run_publish(["tags-get", player], timeout=10))
        if table is not None:
            self._write(200, table)

    def _handle_player_char_xp(self, player: str) -> None:
        table = self._player_reply(run_publish(["char-xp-read", player], timeout=15))
        if table is None:
            return
        # Enrich the single raw FLevelComponent row with derived level/intel.
        summary = None
        if table["rows"]:
            row = dict(zip(table["headers"], table["rows"][0]))
            try:
                summary = admin_progression.char_xp_summary(int(row.get("total_xp") or 0))
                summary["totalSkillPoints"] = int(row.get("total_skill_points") or 0)
                summary["spentSkillPoints"] = int(row.get("spent_skill_points") or 0)
            except (ValueError, TypeError):
                summary = None
        self._write(200, {
            "headers": table["headers"], "rows": table["rows"], "summary": summary,
        })

    def _handle_player_summary(self, player: str) -> None:
        """Current-state roll-up for the Player Editor: solaris, XP (level/
        to-next/max), faction (alignment + per-house rep/tier), and per-preset
        journey completion. Parses the player-summary KV output + enriches with
        admin_progression (pure)."""
        entry = run_publish(["player-summary", player], timeout=15)
        if not entry["ok"]:
            code = entry.get("exit_code")
            msg = (entry.get("stderr") or "").strip() or "summary failed"
            if code == 3:
                self._write(200, {"ok": False, "schema_gap": True, "error": msg})
            else:
                self._write(404 if code in (1, 2) else 200, {"ok": False, "error": msg})
            return
        kv: dict[str, str] = {}
        journey_raw = ""
        for line in (entry.get("stdout") or "").splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k == "journey":
                journey_raw = v
            else:
                kv[k] = v

        def _int(key: str) -> int:
            try:
                return int(kv.get(key) or 0)
            except (ValueError, TypeError):
                return 0

        def _house(fid: int, rep: int) -> dict:
            tier = admin_progression.rep_to_tier(rep)
            return {
                "faction_id": fid,
                "name": admin_progression.faction_display_name(fid),
                "rep": rep,
                "tier": tier,
                "tier_name": admin_progression.faction_tier_name(fid, tier),
            }

        align = _int("align")
        journey = []
        for part in journey_raw.split(","):
            bits = part.split(":")
            if len(bits) == 3 and bits[0]:
                preset = admin_progression.progression_preset(bits[0])
                try:
                    journey.append({
                        "id": bits[0],
                        "name": preset["name"] if preset else bits[0],
                        "complete": int(bits[1]),
                        "total": int(bits[2]),
                    })
                except ValueError:
                    pass
        self._write(200, {
            "ok": True,
            "solaris": _int("solaris"),
            "xp": admin_progression.char_xp_summary(_int("char_xp")),
            "faction": {
                "alignment": align,
                "alignment_name": admin_progression.faction_display_name(align) if align else "None",
                "atreides": _house(1, _int("rep_1")),
                "harkonnen": _house(2, _int("rep_2")),
            },
            "journey": journey,
        })

    # ------ POST ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._write(400, {"error": "invalid Content-Length"})
            return
        if length < 0:
            self._write(400, {"error": "invalid Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._write(413, {"error": "request body too large", "max_bytes": MAX_BODY_BYTES})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            self._write(400, {"error": "invalid JSON body", "detail": str(exc)})
            return

        # Phase 5: apply server-setting changes. Body: {"settings": {id: value}}.
        # Each value is validated + normalized against the schema, then written
        # to its INI sink via the engine; takes effect on the next restart.
        if path == "/api/settings":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            updates = body.get("settings") if isinstance(body, dict) else None
            if not isinstance(updates, dict) or not updates:
                self._write(400, {"error": 'body must be {"settings": {"<id>": <value>, ...}}'})
                return
            schema = {st["id"]: st for st in load_settings_schema()}
            applied: list[str] = []
            errors: list[dict] = []
            for sid, value in updates.items():
                st = schema.get(sid)
                if st is None:
                    errors.append({"id": sid, "error": "unknown setting id"})
                    continue
                refusal = blank_submission_error(st, value)
                if refusal:
                    errors.append({"id": sid, "error": refusal})
                    continue
                # A setting backed by an egg variable is rewritten from that
                # variable by apply-config.sh on EVERY boot. Writing only the
                # INI meant the restart this endpoint asks for was the very
                # thing that reverted the change — silently. So the variable
                # moves first: if the panel refuses it, nothing is written and
                # the operator is told why, rather than watching the value
                # evaporate at the next restart (which the scheduler may do
                # unattended).
                pinned = pin_setting_to_egg(st, value)
                if pinned is not None:
                    ok_pin, detail = pinned
                    if not ok_pin:
                        errors.append({"id": sid, "error": detail})
                        continue
                try:
                    write_setting(st, value)
                    applied.append(sid)
                except (ValueError, OSError) as exc:
                    errors.append({"id": sid, "error": str(exc)})
            self._write(200, {
                "ok": not errors,
                "applied": applied,
                "errors": errors,
                "restartRequired": bool(applied),
            })
            return

        # Phase 6: run one welcome-kit scan now (grants the active package to
        # online players not yet recorded). No-op while the kit is disabled.
        if path == "/api/welcome/scan":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_welcome("scan", timeout=120)
            self._write(200, r.get("data", r))
            return

        # Phase 6: clear failed grant rows so the next scan retries them.
        if path == "/api/welcome/retry-failed":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_welcome("retry-failed", timeout=15)
            self._write(200, r.get("data", r))
            return

        if path == "/api/vehicles/delete":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            actor_id = body.get("actor_id") if isinstance(body, dict) else None
            if not isinstance(actor_id, int) or actor_id <= 0:
                self._write(400, {"error": "actor_id (positive int) required"})
                return
            entry = run_publish(["vehicle-delete", str(actor_id)], timeout=10)
            self._write(200, entry)
            return

        # Fill one base's water devices to capacity. The subcommand FAILS
        # CLOSED unless the base's map has zero live instances (a running map
        # rewrites base state from memory on flush).
        # POST /api/bases/<id>/water-refill  body {"force"?: bool}
        if path.startswith("/api/bases/") and path.endswith("/water-refill"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            bid = path[len("/api/bases/"):-len("/water-refill")]
            if not bid.isdigit():
                self._write(400, {"error": "base id must be numeric"})
                return
            forced = isinstance(body, dict) and bool(body.get("force"))
            if not forced:
                self._write(200, {"ok": False, "requiresConfirmation": True,
                                  "message": "Refilling writes to player property and requires the "
                                             "base's map to be fully stopped. Re-send with force:true."})
                return
            entry = run_publish(["base-water-refill", bid], timeout=30)
            self._write(200, entry)
            return

        # Character backup (native transfer export). Offline-gated by the
        # subcommand; the export proc refuses online players too.
        # POST /api/players/<id>/char-backup  body {"reason"?: str}
        if path.startswith("/api/players/") and path.endswith("/char-backup"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/char-backup")])
            reason = body.get("reason") if isinstance(body, dict) else ""
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["char-backup", player, "manual", str(reason or "")], timeout=120)
            self._write(200, entry)
            return

        # Character restore — FULL REPLACE of the backup's FLS id from a
        # backup file (import proc enforces offline + patch checksum).
        # POST /api/char-backups/restore  body {"file": str, "force"?: bool}
        if path == "/api/char-backups/restore":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            fname = body.get("file") if isinstance(body, dict) else None
            if not isinstance(fname, str) or not fname:
                self._write(400, {"error": "file (backup name) required"})
                return
            if not bool(body.get("force")):
                self._write(200, {"ok": False, "requiresConfirmation": True,
                                  "message": f"Restoring {fname} REPLACES the character's current "
                                             "state entirely. Re-send with force:true."})
                return
            entry = run_publish(["char-restore", fname], timeout=180)
            self._write(200, entry)
            return

        # Delete ONE character backup file (data + sidecar).
        # POST /api/char-backups/delete  body {"file": str}
        if path == "/api/char-backups/delete":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            fname = body.get("file") if isinstance(body, dict) else None
            if not isinstance(fname, str) or not fname:
                self._write(400, {"error": "file (backup name) required"})
                return
            entry = run_publish(["char-backup-delete", fname], timeout=15)
            self._write(200, entry)
            return

        # Player WRITE: adjust Solaris balance. Offline-gated + reversible.
        # POST /api/players/<id>/give-currency  body {"amount": <int>}
        if path.startswith("/api/players/") and path.endswith("/give-currency"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/give-currency")])
            amount = body.get("amount") if isinstance(body, dict) else None
            # bool is an int subclass — reject it explicitly.
            if not isinstance(amount, int) or isinstance(amount, bool):
                self._write(400, {"error": "amount (integer) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["give-currency", player, str(amount)], timeout=15)
            self._write(200, entry)
            return

        # Player WRITE: rename character. Offline-gated + reversible.
        # POST /api/players/<id>/rename  body {"name": "<new>"}
        if path.startswith("/api/players/") and path.endswith("/rename"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/rename")])
            name = body.get("name") if isinstance(body, dict) else None
            if not isinstance(name, str) or not name.strip():
                self._write(400, {"error": "name (non-empty string) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["rename", player, name], timeout=15)
            self._write(200, entry)
            return

        # Player WRITE: add/remove progression tags. Offline-gated + reversible.
        # POST /api/players/<id>/tags  body {"add": [...], "remove": [...]}
        if path.startswith("/api/players/") and path.endswith("/tags"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/tags")])
            add = body.get("add", []) if isinstance(body, dict) else []
            remove = body.get("remove", []) if isinstance(body, dict) else []
            if not isinstance(add, list) or not isinstance(remove, list) \
                    or not all(isinstance(t, str) for t in (*add, *remove)):
                self._write(400, {"error": "add/remove must be arrays of strings"})
                return
            if not add and not remove:
                self._write(400, {"error": "nothing to add or remove"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            # CLI takes comma-separated lists; tags contain no commas.
            entry = run_publish(["tags-update", player, ",".join(add), ",".join(remove)], timeout=15)
            self._write(200, entry)
            return

        # Player WRITE: award/deduct character XP (recomputes level/SP/intel).
        # Offline-gated. POST /api/players/<id>/award-char-xp  body {"amount": <int>}
        if path.startswith("/api/players/") and path.endswith("/award-char-xp"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/award-char-xp")])
            amount = body.get("amount") if isinstance(body, dict) else None
            if not isinstance(amount, int) or isinstance(amount, bool):
                self._write(400, {"error": "amount (integer) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["award-char-xp", player, str(amount)], timeout=20)
            self._write(200, entry)
            return

        # Player WRITE: grant all 205 keystones + recompute SP. Offline-gated.
        # POST /api/players/<id>/grant-keystones  (no body)
        if path.startswith("/api/players/") and path.endswith("/grant-keystones"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/grant-keystones")])
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["grant-keystones", player], timeout=20)
            self._write(200, entry)
            return

        # Offline-gated, PAWN-keyed, reversible. Flip every discovered recipe's
        # UnlockedState in TechKnowledgePlayerComponent.
        # POST /api/players/<id>/tech-unlock  body {"mode": "unlock-all"|"lock-all"}
        if path.startswith("/api/players/") and path.endswith("/tech-unlock"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/tech-unlock")])
            if not player:
                self._write(400, {"error": "player id required"})
                return
            mode = body.get("mode") if isinstance(body, dict) else None
            if mode not in ("unlock-all", "lock-all"):
                self._write(200, {"ok": False, "error": "mode must be unlock-all or lock-all"})
                return
            entry = run_publish(["tech-unlock", player, mode], timeout=30)
            self._write(200, entry)
            return

        # Player WRITE: grant items. Online+quality0 -> RMQ AddItemToInventory;
        # offline -> direct dune.items INSERT (stack/slot/volume planned). The
        # bash layer enforces the offline gate for the DB path.
        # POST /api/players/<id>/give-item  body {"template": str, "qty"?: int, "quality"?: int}
        if path.startswith("/api/players/") and path.endswith("/give-item"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/give-item")])
            template = body.get("template") if isinstance(body, dict) else None
            qty = body.get("qty", 1) if isinstance(body, dict) else 1
            quality = body.get("quality", 0) if isinstance(body, dict) else 0
            if not isinstance(template, str) or not template.strip():
                self._write(400, {"error": "template (non-empty string) required"})
                return
            # bool is an int subclass — reject it explicitly for both numbers.
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                self._write(400, {"error": "qty (positive integer) required"})
                return
            if not isinstance(quality, int) or isinstance(quality, bool) or quality < 0:
                self._write(400, {"error": "quality (non-negative integer) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(
                ["give-item", player, template, str(qty), str(quality)], timeout=20
            )
            self._write(200, entry)
            return

        # Player WRITE: adjust Great-House reputation by a signed delta.
        # Offline-gated; rebuilds the controller's m_FactionDataArray jsonb.
        # POST /api/players/<id>/faction-rep  body {"faction": "atreides"|"harkonnen"|1|2, "amount": <int>}
        if path.startswith("/api/players/") and path.endswith("/faction-rep"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/faction-rep")])
            faction = body.get("faction") if isinstance(body, dict) else None
            amount = body.get("amount") if isinstance(body, dict) else None
            # faction may be a name string or an int id; bash maps + validates it.
            if isinstance(faction, bool) or not isinstance(faction, (str, int)) or \
                    (isinstance(faction, str) and not faction.strip()):
                self._write(400, {"error": "faction (atreides|harkonnen|1|2) required"})
                return
            if not isinstance(amount, int) or isinstance(amount, bool):
                self._write(400, {"error": "amount (integer) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(
                ["faction-rep", player, str(faction), str(amount)], timeout=20
            )
            self._write(200, entry)
            return

        # Player WRITE: align to a Great House AND set the reputation TIER directly
        # (0-20). Offline-gated; calls change_player_faction (alignment) then writes
        # the tier's rep + rebuilds m_FactionDataArray on the controller.
        # POST /api/players/<id>/set-faction-tier  body {"faction": "atreides"|"harkonnen"|1|2, "tier": <0-20>}
        if path.startswith("/api/players/") and path.endswith("/set-faction-tier"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/set-faction-tier")])
            faction = body.get("faction") if isinstance(body, dict) else None
            tier = body.get("tier") if isinstance(body, dict) else None
            if isinstance(faction, bool) or not isinstance(faction, (str, int)) or \
                    (isinstance(faction, str) and not faction.strip()):
                self._write(400, {"error": "faction (atreides|harkonnen|1|2) required"})
                return
            if not isinstance(tier, int) or isinstance(tier, bool) or tier < 0 or tier > 20:
                self._write(400, {"error": "tier (integer 0-20) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(
                ["set-faction-tier", player, str(faction), str(tier)], timeout=20
            )
            self._write(200, entry)
            return

        # Player WRITE: remove the player from their Great House entirely
        # (alignment -> None, clear both houses' rep + the m_FactionDataArray
        # jsonb). Offline-gated. POST /api/players/<id>/remove-faction
        if path.startswith("/api/players/") and path.endswith("/remove-faction"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/remove-faction")])
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["remove-faction", player], timeout=20)
            self._write(200, entry)
            return

        # Economy WRITE: toggle spawning for one spicefield type (live, server-wide).
        # POST /api/spice/<type_id>/spawning  body {"active": true|false}
        if path.startswith("/api/spice/") and path.endswith("/spawning"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            tid = path[len("/api/spice/"):-len("/spawning")]
            active = body.get("active") if isinstance(body, dict) else None
            if not tid.isdigit():
                self._write(400, {"error": "spicefield type id (integer) required"})
                return
            if not isinstance(active, bool):
                self._write(400, {"error": "active (boolean) required"})
                return
            entry = run_publish(["spice-set", tid, "on" if active else "off"], timeout=10)
            self._write(200, entry)
            return

        # Economy WRITE: set the global active/primed caps for one spicefield type.
        # POST /api/spice/<type_id>/caps  body {"max_active": int, "max_primed": int}
        if path.startswith("/api/spice/") and path.endswith("/caps"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            tid = path[len("/api/spice/"):-len("/caps")]
            ma = body.get("max_active") if isinstance(body, dict) else None
            mp = body.get("max_primed") if isinstance(body, dict) else None
            if not tid.isdigit():
                self._write(400, {"error": "spicefield type id (integer) required"})
                return
            if not isinstance(ma, int) or isinstance(ma, bool) or ma < 0 or \
               not isinstance(mp, int) or isinstance(mp, bool) or mp < 0:
                self._write(400, {"error": "max_active and max_primed (non-negative integers) required"})
                return
            entry = run_publish(["spice-caps", tid, str(ma), str(mp)], timeout=10)
            self._write(200, entry)
            return

        # Economy WRITE: seed the exchange with NPC sell orders from the catalog.
        # POST /api/market/post  body {"limit": <int, default 50>}
        if path == "/api/market/post":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            limit = body.get("limit", 50) if isinstance(body, dict) else 50
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 2000:
                self._write(400, {"error": "limit (integer 1-2000) required"})
                return
            entry = run_publish(["market-bot-post", str(limit)], timeout=120)
            self._write(200, entry)
            return

        # Economy WRITE: remove all market-bot NPC orders (off switch).
        # POST /api/market/clear
        if path == "/api/market/clear":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            entry = run_publish(["market-bot-clear"], timeout=60)
            self._write(200, entry)
            return

        # Economy config: arm/disarm the autonomous loop + tune buy/gamble fields.
        # POST /api/market/config  body {enabled?, max_listings?, buy_threshold?,
        # gamble_die?, gamble_target?, max_buys_per_tick?, scan_interval_secs?,
        # disabled_items?}. Validated + persisted to market-bot.json by admin_market.
        # Returns 200 + {ok, config|error} (CF-safe: errors carried in the body).
        if path == "/api/market/config":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_market("set-config", json.dumps(body if isinstance(body, dict) else {}), timeout=15)
            self._write(200, r.get("data", {"ok": False, "error": "set-config failed"}))
            return

        # Economy WRITE: run ONE gamble-buy pass right now (manual trigger). Buys
        # cheap player orders (price gate + d-die gamble), bounded by
        # max_buys_per_tick. POST /api/market/buy. Runs regardless of enabled.
        if path == "/api/market/buy":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_market("buy-tick", timeout=120)
            self._write(200, r.get("data", {"ok": False, "error": "buy-tick failed"}))
            return

        # POST /api/autoscaler/config  body = the full validated autoscaler config
        # {enabled, scan_interval_secs, idle_drain_secs, demand_grace_secs,
        # players_per_instance, maps:[{map,enabled,min_replicas,max_replicas}]}.
        # Persisted to autoscaler.json by admin_autoscaler. 200 + {ok, config|error}.
        if path == "/api/autoscaler/config":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_autoscaler("set-config", json.dumps(body if isinstance(body, dict) else {}), timeout=15)
            self._write(200, r.get("data", {"ok": False, "error": "set-config failed"}))
            return

        # Run ONE autoscaler tick right now (manual trigger; ignores enabled). Reads
        # demand + online counts and scales the managed maps. POST /api/autoscaler/tick.
        if path == "/api/autoscaler/tick":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_autoscaler("tick", timeout=60)
            self._write(200, r.get("data", {"ok": False, "error": "tick failed"}))
            return

        # Restart ONE supervised background service (kill + relaunch via its
        # start-<svc>.sh, env recovered from its own /proc/<pid>/environ). Allowlist
        # is enforced HERE and again in admin-publish.sh; ue5-*/postgres/mq-* are
        # not restartable. POST /api/svc/restart  body {"service": "<name>"}.
        if path == "/api/svc/restart":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            service = body.get("service") if isinstance(body, dict) else None
            if not isinstance(service, str) or not admin_logs.valid_restartable_service(service):
                self._write(400, {"error": "service must be one of the restartable services",
                                  "services": list(admin_logs.RESTARTABLE_SERVICES)})
                return
            entry = run_publish(["svc-restart", service], timeout=60)
            self._write(200, entry)
            return

        # Instance control (phase 1): scale a map's instance count via mock-k8s
        # ServerSetScale replicas — the exact, self-healing path the Director uses.
        # Repair the in-game browser: sweep orphan farm_state + resync the Director,
        # fixing deleted sietches/dimensions that linger in the browser. POST /api/instances/repair
        if path == "/api/instances/repair":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            self._write(200, run_publish(["repair-browser"], timeout=60))
            return

        # Allowlisted to the on-demand maps; player-online guard on scale-down
        # (returns requiresConfirmation -> client re-POSTs with force:true).
        # POST /api/instances/<map>/scale  body {"replicas": 0..N, "force"?: bool}
        if path.startswith("/api/instances/") and path.endswith("/scale"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            mp = unquote(path[len("/api/instances/"):-len("/scale")])
            if mp not in SCALABLE_MAPS:
                self._write(200, {"ok": False, "error": f"map not scalable (allowed: {', '.join(SCALABLE_MAPS)})"})
                return
            replicas = body.get("replicas") if isinstance(body, dict) else None
            if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 0 or replicas > SCALE_REPLICAS_MAX:
                self._write(200, {"ok": False, "error": f"replicas (integer 0-{SCALE_REPLICAS_MAX}) required"})
                return
            force = bool(body.get("force")) if isinstance(body, dict) else False
            mock = fetch_mock_status(MOCK_K8S_PORT)
            if not mock:
                self._write(200, {"ok": False, "error": "mock-k8s /status unreachable"})
                return
            key = admin_instances.resolve_scale_key(mock, mp)
            if key is None:
                self._write(200, {"ok": False, "error": f"{mp} not tracked by mock-k8s"})
                return
            ns, name, current = key
            # Player-online guard on scale-down (DST parity): refuse unless force.
            # farm-player-count keys on the mock-k8s map name (SH_*), so a hub
            # VISITOR is counted here — server-status keys on actor map and would
            # miss them, letting a populated hub be scaled down without confirmation.
            if replicas < current:
                pe = run_publish(["farm-player-count"], timeout=10)
                counts = parse_player_counts(pe["stdout"]) if pe["ok"] else {}
                online = int(counts.get(mp, 0) or 0)
                if online > 0 and not force:
                    self._write(200, {"ok": False, "requiresConfirmation": True, "players": online,
                                      "map": mp, "message": f"{online} player(s) online on {mp}"})
                    return
            code, resp = mock_k8s_request(
                "PATCH",
                f"/apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}",
                {"spec": {"replicas": replicas}},
            )
            if code is None or code >= 300:
                self._write(200, {"ok": False, "error": f"mock-k8s scale failed (http {code})"})
                return
            applied = (resp or {}).get("spec", {}).get("replicas", replicas)
            self._write(200, {"ok": True, "map": mp, "replicas": applied, "previous": current})
            return

        # Instance control (phase 2): spin a single DeepDesert/Arrakeen/Harko
        # DIMENSION partition down (offline, reversible) or up (respawn). down =
        # kill UE5 + NULL world_partition.server_id + drop farm_state (Director
        # stops routing, graceful); up = background spawn-dimension.sh. Player
        # guard on down (confirm -> force). POST /api/instances/dimension/<pid>/{up,down}
        if path.startswith("/api/instances/dimension/") and (path.endswith("/up") or path.endswith("/down")):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            action = "up" if path.endswith("/up") else "down"
            pid_str = path[len("/api/instances/dimension/"):-(len(action) + 1)]
            if not pid_str.isdigit():
                self._write(200, {"ok": False, "error": "partition id must be numeric"})
                return
            if action == "up":
                self._write(200, run_publish(["dimension-up", pid_str], timeout=30))
                return
            # down: player-online guard from the partition topology read.
            force = bool(body.get("force")) if isinstance(body, dict) else False
            pe = run_publish(["world-partition-list"], timeout=15)
            players, found = 0, False
            if pe["ok"]:
                for line in (pe.get("stdout") or "").splitlines():
                    f = line.strip().split("|")
                    if len(f) >= 10 and f[0] == pid_str:
                        found = True
                        players = int(f[9]) if f[9].isdigit() else 0
                        break
            if not found:
                self._write(200, {"ok": False, "error": f"partition {pid_str} not found"})
                return
            if players > 0 and not force:
                self._write(200, {"ok": False, "requiresConfirmation": True, "players": players,
                                  "partition": int(pid_str), "message": f"{players} player(s) on partition {pid_str}"})
                return
            self._write(200, run_publish(["dimension-down", pid_str], timeout=60))
            return

        # Multi-Sietch (player-chosen Survival_1 instances). Survival_1 runs
        # Dimension mode, so each sietch = a Survival_1 dimension partition.
        # POST /api/sietches            {label?}  -> add + spawn a new sietch
        # POST /api/sietches/<pid>/remove {force?} -> tear down + delete (player-guarded)
        if path == "/api/sietches":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            label = body.get("label") if isinstance(body, dict) else None
            argv = ["sietch-add"]
            if isinstance(label, str) and label.strip():
                argv.append(label.strip()[:48])
            self._write(200, run_publish(argv, timeout=30))
            return
        if path.startswith("/api/sietches/") and path.endswith("/remove"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            pid_str = path[len("/api/sietches/"):-len("/remove")]
            if not pid_str.isdigit():
                self._write(200, {"ok": False, "error": "sietch partition id must be numeric"})
                return
            # Player-online guard from the partition topology read (same as dim-down).
            force = bool(body.get("force")) if isinstance(body, dict) else False
            pe = run_publish(["world-partition-list"], timeout=15)
            players, found = 0, False
            if pe["ok"]:
                for line in (pe.get("stdout") or "").splitlines():
                    f = line.strip().split("|")
                    if len(f) >= 10 and f[0] == pid_str:
                        found = True
                        players = int(f[9]) if f[9].isdigit() else 0
                        break
            if not found:
                self._write(200, {"ok": False, "error": f"sietch {pid_str} not found"})
                return
            if players > 0 and not force:
                self._write(200, {"ok": False, "requiresConfirmation": True, "players": players,
                                  "partition": int(pid_str), "message": f"{players} player(s) in sietch {pid_str}"})
                return
            self._write(200, run_publish(["sietch-remove", pid_str], timeout=60))
            return

        # POST /api/sietches/<pid>/park   {force?} -> pause + KEEP data (player-guarded)
        # POST /api/sietches/<pid>/unpark          -> respawn a parked sietch (no guard)
        if path.startswith("/api/sietches/") and path.endswith("/park"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            pid_str = path[len("/api/sietches/"):-len("/park")]
            if not pid_str.isdigit():
                self._write(200, {"ok": False, "error": "sietch partition id must be numeric"})
                return
            # Parking takes the sietch down — player-online guard (same as remove).
            force = bool(body.get("force")) if isinstance(body, dict) else False
            pe = run_publish(["world-partition-list"], timeout=15)
            players, found = 0, False
            if pe["ok"]:
                for line in (pe.get("stdout") or "").splitlines():
                    f = line.strip().split("|")
                    if len(f) >= 10 and f[0] == pid_str:
                        found = True
                        players = int(f[9]) if f[9].isdigit() else 0
                        break
            if not found:
                self._write(200, {"ok": False, "error": f"sietch {pid_str} not found"})
                return
            if players > 0 and not force:
                self._write(200, {"ok": False, "requiresConfirmation": True, "players": players,
                                  "partition": int(pid_str),
                                  "message": f"{players} player(s) in sietch {pid_str} — parking disconnects them (data kept)"})
                return
            self._write(200, run_publish(["sietch-park", pid_str], timeout=60))
            return
        if path.startswith("/api/sietches/") and path.endswith("/unpark"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            pid_str = path[len("/api/sietches/"):-len("/unpark")]
            if not pid_str.isdigit():
                self._write(200, {"ok": False, "error": "sietch partition id must be numeric"})
                return
            # Unpark only respawns — it never disconnects anyone, so no player guard.
            self._write(200, run_publish(["sietch-unpark", pid_str], timeout=60))
            return

        # Per-sietch config: set this sietch's name + gameplay overrides, then
        # restart it to apply (settings are read at UE5 startup). Player-online
        # guard (restart disconnects them). POST /api/sietches/<pid>/config
        #   body {name?: str, overrides?: {schema_id: value}, force?: bool}
        if path.startswith("/api/sietches/") and path.endswith("/config"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            pid_str = path[len("/api/sietches/"):-len("/config")]
            if not pid_str.isdigit():
                self._write(200, {"ok": False, "error": "sietch partition id must be numeric"})
                return
            name = body.get("name") if isinstance(body, dict) else None
            overrides = body.get("overrides") if isinstance(body, dict) else None
            force = bool(body.get("force")) if isinstance(body, dict) else False
            # Player-online guard — applying restarts the sietch's UE5.
            pe = run_publish(["world-partition-list"], timeout=15)
            players = 0
            if pe["ok"]:
                for line in (pe.get("stdout") or "").splitlines():
                    f = line.strip().split("|")
                    if len(f) >= 10 and f[0] == pid_str:
                        players = int(f[9]) if f[9].isdigit() else 0
                        break
            if players > 0 and not force:
                self._write(200, {"ok": False, "requiresConfirmation": True, "players": players,
                                  "partition": int(pid_str), "message": f"applying restarts sietch {pid_str} ({players} player(s) online)"})
                return
            if isinstance(name, str) and name.strip():
                rn = run_publish(["sietch-rename", pid_str, name.strip()[:48]], timeout=15)
                if not rn["ok"]:
                    self._write(200, {"ok": False, "error": (rn.get("stderr") or "rename failed").strip()})
                    return
            rs = run_sietch("set", pid_str, json.dumps(overrides if isinstance(overrides, dict) else {}), timeout=15)
            if not rs["ok"]:
                self._write(200, {"ok": False, "error": "set overrides failed"})
                return
            # Restart the sietch so the new per-instance config is materialized.
            run_publish(["dimension-down", pid_str], timeout=60)
            up = run_publish(["dimension-up", pid_str], timeout=30)
            d = rs.get("data", {})
            self._write(200, {"ok": True, "applied": d.get("applied", []), "skipped": d.get("skipped", []),
                              "restarted": up.get("ok", False)})
            return

        # Player WRITE: apply (unlock) a journey-progression preset. Offline-gated;
        # gathers root+descendants and calls complete_journey_story_nodes_for_player.
        # POST /api/players/<id>/progression-unlock  body {"preset": "<id>"}
        if path.startswith("/api/players/") and path.endswith("/progression-unlock"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/progression-unlock")])
            preset = body.get("preset") if isinstance(body, dict) else None
            if not isinstance(preset, str) or not preset.strip():
                self._write(400, {"error": "preset (id string) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["progression-unlock", player, preset], timeout=30)
            self._write(200, entry)
            return

        # Player WRITE: reverse (lock) a journey-progression preset. Offline-gated;
        # calls reset_journey_story_nodes_for_player (no dune-admin equivalent).
        # POST /api/players/<id>/progression-lock  body {"preset": "<id>"}
        if path.startswith("/api/players/") and path.endswith("/progression-lock"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/progression-lock")])
            preset = body.get("preset") if isinstance(body, dict) else None
            if not isinstance(preset, str) or not preset.strip():
                self._write(400, {"error": "preset (id string) required"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["progression-lock", player, preset], timeout=30)
            self._write(200, entry)
            return

        # DESTRUCTIVE: hard-delete one item stack by dune.items.id. Offline-gated
        # on the owner + existence-checked in the bash layer.
        # POST /api/items/<item_id>/delete
        if path.startswith("/api/items/") and path.endswith("/delete"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            item_id = unquote(path[len("/api/items/"):-len("/delete")])
            if not item_id.isdigit() or int(item_id) < 1:
                self._write(400, {"error": "item_id (positive integer) required"})
                return
            entry = run_publish(["item-delete", item_id], timeout=15)
            self._write(200, entry)
            return

        # Saved teleport locations: add/replace or remove. Body is one of:
        #   {"action":"add","location":{"name","map","x","y","z"}}
        #   {"action":"remove","name":"..."}
        if path == "/api/map/locations":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            action = body.get("action") if isinstance(body, dict) else None
            if action == "add":
                locs, err = admin_locations.upsert_location(BASE_DIR, body.get("location"))
                if err:
                    self._write(400, {"error": err})
                    return
                self._write(200, {"ok": True, "locations": locs})
                return
            if action == "remove":
                name = body.get("name") or ""
                if not name:
                    self._write(400, {"error": "name required"})
                    return
                self._write(200, {"ok": True, "locations": admin_locations.remove_location(BASE_DIR, name)})
                return
            self._write(400, {"error": 'action must be "add" or "remove"'})
            return

        # Teleport a player to a saved location (snap-to-safe). The player must
        # be online (teleport is an RMQ command). Body: {"player","location"}.
        if path == "/api/map/teleport":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = (body.get("player") if isinstance(body, dict) else "") or ""
            loc = admin_locations.find_location(BASE_DIR, body.get("location") if isinstance(body, dict) else "")
            if not player:
                self._write(400, {"error": "player required"})
                return
            if loc is None:
                self._write(404, {"error": "unknown location"})
                return
            entry = run_publish(["tpsafe", player, str(loc["x"]), str(loc["y"]), str(loc["z"])], timeout=15)
            self._write(200, entry)
            return

        # Unattended scheduler config update (auth+csrf). Body: {restart:{...}, backup:{...}}.
        if path == "/api/schedule":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_schedule("set-config", json.dumps(body if isinstance(body, dict) else {}), timeout=15)
            self._write(200 if r["ok"] else 400, r.get("data", r))
            return

        # Ad-hoc restart: arm one at now+delay, with the in-game countdown
        # opening `warn_window_secs` before it rather than immediately.
        # `admin shutdown` alone only banners players — verified, it stops
        # nothing — so this goes through the scheduler's pending-restart
        # mechanism, which is what actually calls the panel's power API.
        if path == "/api/schedule/restart-in":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            try:
                delay = int(body["delay_secs"])
                window = int(body.get("warn_window_secs", min(delay, 600)))
                freq = int(body.get("warn_freq_secs", 60))
            except (KeyError, TypeError, ValueError):
                self._write(400, {"error": "delay_secs required; warn_window_secs and "
                                           "warn_freq_secs optional, all integers"})
                return
            if delay < 0 or window < 0 or freq < 1:
                self._write(400, {"error": "delay_secs/warn_window_secs must be >= 0, "
                                           "warn_freq_secs >= 1"})
                return
            r = run_schedule("arm-restart", str(delay), str(window), str(freq), timeout=30)
            self._write(200 if r["ok"] else 400, r.get("data", r))
            return

        if path == "/api/schedule/cancel-restart":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            r = run_schedule("cancel-restart", timeout=45)
            self._write(200 if r["ok"] else 400, r.get("data", r))
            return

        # Manual task trigger: /api/tasks/trigger/<backup|restart|task-id> (auth+csrf).
        # backup/restart hit their bespoke runners; any other id is a generic
        # scheduled task run on demand (run-task ignores the schedule/enabled gate).
        if path.startswith("/api/tasks/trigger/"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            task = unquote(path[len("/api/tasks/trigger/"):])
            sub = {"backup": "run-backup", "restart": "run-restart"}.get(task)
            if sub is not None:
                r = run_schedule(sub, timeout=600 if task == "backup" else 60)
            elif task:
                r = run_schedule("run-task", task, timeout=120)
            else:
                self._write(400, {"error": "task id required"})
                return
            self._write(200, r.get("data", r))
            return

        # DESTRUCTIVE: wipe all specialization tracks + purchased keystones.
        # Offline-gated; controller-keyed. POST /api/players/<id>/reset-spec
        if path.startswith("/api/players/") and path.endswith("/reset-spec"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/reset-spec")])
            if not player:
                self._write(400, {"error": "player id required"})
                return
            entry = run_publish(["reset-spec", player], timeout=20)
            self._write(200, entry)
            return

        # IRREVERSIBLE: delete the whole account/character + cascade. Requires a
        # `confirm` field that the bash layer checks equals the resolved FLS id,
        # and the character to be offline. POST /api/players/<id>/account-delete
        #   body {"confirm": "<exact 16-hex FLS id>", "reason"?: "<text>"}
        if path.startswith("/api/players/") and path.endswith("/account-delete"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            player = unquote(path[len("/api/players/"):-len("/account-delete")])
            confirm = body.get("confirm") if isinstance(body, dict) else None
            reason = body.get("reason", "admin delete via admin-http") if isinstance(body, dict) else "admin delete via admin-http"
            if not isinstance(confirm, str) or not confirm.strip():
                self._write(400, {"error": "confirm (the exact 16-hex FLS id) required"})
                return
            if not isinstance(reason, str) or not reason.strip():
                self._write(400, {"error": "reason must be a non-empty string"})
                return
            if not player:
                self._write(400, {"error": "player id required"})
                return
            # Takes a verified pre-delete character backup first (v0.46
            # semantics) — the export of a big character needs headroom.
            entry = run_publish(["account-delete", player, confirm, reason], timeout=120)
            self._write(200, entry)
            return

        # /api/login is the only POST that bypasses Bearer auth.
        if path == "/api/login":
            if not UI_ENABLED:
                self._write(400, {"error": "login not available in internal mode"})
                return
            if not UI_PASSWORD:
                self._write(503, {"error": "DUNE_ADMIN_UI_PASSWORD not set on the server"})
                return
            client_ip = self._client_ip()
            # Charges the attempt before checking the password, so a burst
            # of parallel guesses can't all slip through on the same
            # pre-burst bucket reading. A correct password refunds it.
            retry_after = login_attempt_gate(client_ip)
            if retry_after:
                log(f"WARN login rate-limited for {client_ip} (retry_after={retry_after}s)")
                self._write(
                    429,
                    {
                        "error": "too many login attempts",
                        "retry_after_seconds": retry_after,
                    },
                    extra_headers=[("Retry-After", str(retry_after))],
                )
                return
            supplied = str(body.get("password", ""))
            if not hmac.compare_digest(supplied, UI_PASSWORD):
                # Small fixed delay to slow down brute-force guessing.
                time.sleep(0.3)
                log(
                    f"WARN failed login from {client_ip} "
                    f"({login_attempts_in_window(client_ip)}/{LOGIN_MAX_ATTEMPTS} in window)"
                )
                self._write(401, {"error": "invalid password"})
                return
            # Successful login — refund this attempt and clear any prior
            # failure history for this IP so a legitimate operator who
            # fat-fingered a few times isn't punished for the next 15 minutes.
            clear_login_attempts(client_ip)
            # The password was right; if the cookie is about to be thrown
            # away by the browser, this is the only place anyone will ever
            # hear about it.
            self._warn_if_cookie_will_be_dropped()
            token, csrf = issue_session_token()
            self._write(
                200,
                {
                    # Token returned in body for legacy Bearer-header
                    # callers (internal scripts, debug curl). The SPA
                    # ignores this and uses the session cookie below.
                    "token": token,
                    "csrf": csrf,
                    "expires_in": SESSION_TTL_SECONDS,
                    "type": "Bearer",
                },
                extra_headers=self._session_cookies(token, csrf),
            )
            return

        # POST /api/logout revokes the current session's jti and clears
        # the cookies. Idempotent: repeated calls or unauthenticated
        # calls succeed without effect, to avoid leaking whether a
        # token was valid.
        if path == "/api/logout":
            if UI_ENABLED:
                # Cookie-auth callers must clear CSRF — otherwise a
                # stale cross-origin link could trigger logout. But
                # legacy Bearer callers don't need CSRF.
                if not self._csrf_ok():
                    self._write(403, {"error": "csrf token missing or invalid"})
                    return
                token, _ = self._session_token_from_request()
                payload = verify_session_token(token) if token else None
                if payload:
                    if revoke_token(payload):
                        log(f"INFO session revoked jti={payload.get('jti')}")
            self._write(200, {"ok": True}, extra_headers=self._clear_session_cookies())
            return

        if not self._auth_ok():
            self._write(401, {"error": "auth required"})
            return
        if not self._csrf_ok():
            self._write(403, {"error": "csrf token missing or invalid"})
            return

        # Database tab read-only SQL (auth + csrf already enforced above).
        if path == "/api/database/sql":
            self._handle_db_sql(body.get("sql", "") if isinstance(body, dict) else "")
            return

        # Trigger a DB backup now (pg_dump of the dune DB). Auth+csrf; restore
        # stays CLI-only (destructive). pg_dump can take a while -> generous timeout.
        if path == "/api/database/backup":
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            entry = run_publish(["db-backup"], timeout=300)
            self._write(200, entry)
            return

        if not path.startswith("/admin/"):
            self._write(404, {"error": "unknown route", "path": path})
            return
        sub = path[len("/admin/"):]
        try:
            argv = build_argv(sub, body)
        except KeyError as exc:
            self._write(400, {"error": "missing field", "field": str(exc)})
            return
        except ValueError as exc:
            self._write(400, {"error": str(exc)})
            return

        log(f"invoke admin-publish.sh {shlex.join(argv)}")
        try:
            entry = run_publish(argv)
        except subprocess.TimeoutExpired:
            self._write(504, {"error": "admin-publish.sh timed out"})
            return
        self._write(200, entry)


# --------------------------------------------------------------------------
# Listener.
# --------------------------------------------------------------------------
_LOOPBACK_ADDRS = {"127.0.0.1", "::1", "localhost"}


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threaded so that one slow or silent peer cannot stall the panel for
    everyone else, capped so that a flood of them cannot spawn threads
    without bound. Excess connections are closed immediately — a scanner
    gets a dropped socket, and the operator's next click still lands."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_cls, bind_and_activate: bool = True,
                 max_connections: int = MAX_CONNECTIONS) -> None:
        super().__init__(server_address, handler_cls, bind_and_activate)
        self.max_connections = max_connections
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            log(
                f"WARN refused connection from {client_address[0]} — "
                f"{self.max_connections} concurrent connections already in flight"
            )
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The worker thread never started, so it will never release.
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        """A peer hanging up mid-response is routine on a public bind, not
        a defect — a scanner does it on every probe. Log one line for those
        and keep the full traceback for anything that is actually our bug,
        so admin-http.log stays readable enough to spot the real one."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            log(f"connection from {client_address[0]} dropped: {type(exc).__name__}")
            return
        super().handle_error(request, client_address)


def build_server(address: tuple[str, int] | None = None, tls: bool = True,
                 **kwargs) -> BoundedThreadingHTTPServer:
    """Single construction point for the listener, so tests exercise the
    same wiring production runs.

    Serves TLS directly when a usable certificate is on disk — for the
    operator with a domain and nothing in front of the panel, who
    otherwise has no https:// to reach at all. `tls=False` is for tests
    that want a plain socket regardless of what the host has lying around."""
    global SERVING_TLS
    srv = BoundedThreadingHTTPServer(address or (LISTEN_ADDR, LISTEN_PORT), Handler, **kwargs)
    ctx = admin_tls.build_context(BASE_DIR, log=log) if tls else None
    if ctx is not None:
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        SERVING_TLS = True
        # Reload on renewal: automating renewal is pointless if it still
        # needs someone to restart the panel afterwards.
        admin_tls.watch(BASE_DIR, ctx, log=log)
    return srv


def main() -> None:
    if not os.access(PUBLISH_SH, os.X_OK):
        print(f"[admin-http] [ERROR] admin-publish.sh not executable at {PUBLISH_SH}", file=sys.stderr)
        sys.exit(1)
    if UI_ENABLED and not UI_PASSWORD:
        print("[admin-http] [ERROR] UI mode enabled but DUNE_ADMIN_UI_PASSWORD is empty", file=sys.stderr)
        sys.exit(1)
    if UI_ENABLED and not UI_SESSION_SECRET:
        print("[admin-http] [ERROR] DUNE_ADMIN_UI_SESSION_SECRET missing — prestart.sh should generate it", file=sys.stderr)
        sys.exit(1)
    # Refuse the dangerous internal-mode + non-loopback + no-auth combo.
    # Internal mode does not require a session token; an open bind with
    # no SHARED_AUTH would let any network neighbour invoke admin-publish.sh.
    if not UI_ENABLED and LISTEN_ADDR not in _LOOPBACK_ADDRS and not SHARED_AUTH:
        print(
            f"[admin-http] [ERROR] internal mode bound to {LISTEN_ADDR} without DUNE_ADMIN_HTTP_AUTH — "
            "either bind 127.0.0.1, set DUNE_ADMIN_HTTP_AUTH, or enable UI mode",
            file=sys.stderr,
        )
        sys.exit(1)
    # UI mode publicly served without a DUNE_ADMIN_UI_DOMAIN means CORS is
    # locked to same-origin (good) BUT the operator probably misconfigured.
    # Warn loudly; don't refuse — a same-origin deployment is legitimate.
    if UI_ENABLED and LISTEN_ADDR not in _LOOPBACK_ADDRS and not UI_DOMAIN:
        print(
            f"[admin-http] [WARN] UI mode bound to {LISTEN_ADDR} without DUNE_ADMIN_UI_DOMAIN — "
            "browser must access the panel from the same origin as the API",
            file=sys.stderr,
        )
    # Load any persisted session revocations from prior runs. The file
    # is best-effort; absence/corruption just means we start with an
    # empty revocation list (logged inside _load_revocations).
    _load_revocations()
    mode = "UI" if UI_ENABLED else "internal"
    log(f"mode={mode} bind={LISTEN_ADDR}:{LISTEN_PORT} scripts={SCRIPTS_DIR}")
    if UI_ENABLED:
        log(f"  web root: {UI_WEB_ROOT} ({'present' if (UI_WEB_ROOT / 'index.html').is_file() else 'missing — SPA not built yet'})")
        log(f"  domain  : {UI_DOMAIN or '(none — using IP+port directly)'}")
        log(f"  data    : vehicles={len(_DATA['vehicles'])} items={len(_DATA['items'])} skills={len(_DATA['skills'])}")
        log(f"  steam   : {'STEAM_API_KEY set — persona lookups enabled' if STEAM_API_KEY else 'STEAM_API_KEY blank — persona lookups disabled'}")
        log(f"  ratelim : {LOGIN_MAX_ATTEMPTS} /api/login attempts per {LOGIN_WINDOW_SECONDS}s per-IP")
        if TRUSTED_PROXIES:
            log(f"  proxies : trusting X-Forwarded-For from {', '.join(str(n) for n in TRUSTED_PROXIES)}")
        else:
            log("  proxies : none declared — bucketing on the socket peer "
                "(behind a reverse proxy that is ONE bucket for everyone; "
                "set DUNE_ADMIN_UI_TRUSTED_PROXIES)")
        if UI_TLS == "auto":
            fallback = "Secure" if cookie_secure() else "not Secure"
            log(f"  cookies : TLS=auto — {fallback} unless a declared proxy reports otherwise"
                + ("" if TRUSTED_PROXIES else "; nothing declared, so this is a guess from "
                   "DUNE_ADMIN_UI_DOMAIN. Set DUNE_ADMIN_UI_TLS=off if no TLS fronts this panel"))
        else:
            log(f"  cookies : TLS={UI_TLS} (explicit) — session cookies "
                f"{'are' if cookie_secure() else 'are NOT'} marked Secure")
        log(f"  revoked : {len(REVOCATIONS)} session token(s) on the revocation list")
    else:
        log(f"  auth    : {'shared-secret' if SHARED_AUTH else 'open (loopback only)'}")
    log(f"  sockets : {MAX_CONNECTIONS} concurrent max, {CONNECTION_TIMEOUT_SECONDS:g}s per-connection timeout")
    _, carried = admin_history.recent(BASE_DIR, 1)
    log(f"  audit   : {carried} entr{'y' if carried == 1 else 'ies'} carried over from previous runs")
    # Built before the TLS line is logged, because whether we terminate TLS
    # is decided here and it is the one thing the operator must not have to
    # guess about.
    server = build_server()
    if SERVING_TLS:
        log(f"  tls     : {admin_tls.describe(BASE_DIR)}")
    else:
        cert, _ = admin_tls.cert_paths(BASE_DIR)
        log(f"  tls     : plain HTTP — no certificate at {cert}"
            f"{'; something in front must terminate TLS' if UI_DOMAIN else ''}")
    # Mark the boot in the trail itself: a restart should read as an event,
    # not as an unexplained gap between two commands.
    admin_history.note(BASE_DIR, "(admin panel started)",
                       f"mode={mode} bind={LISTEN_ADDR}:{LISTEN_PORT} tls={'on' if SERVING_TLS else 'off'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
