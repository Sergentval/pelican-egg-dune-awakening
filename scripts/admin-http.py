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
import json
import mimetypes
import os
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
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


# --------------------------------------------------------------------------
# Config — env vars resolved once at process start.
# --------------------------------------------------------------------------
LISTEN_ADDR = os.environ.get("DUNE_ADMIN_HTTP_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("DUNE_ADMIN_HTTP_PORT", "8089"))
SHARED_AUTH = os.environ.get("DUNE_ADMIN_HTTP_AUTH", "")
BASE_DIR = os.environ.get("DUNE_BASE_DIR", "/home/container")
SCRIPTS_DIR = BASE_DIR + "/scripts"
# mock-k8s serves its /status (per-map instance/scale) over HTTPS on this port.
MOCK_K8S_PORT = int(os.environ.get("K8S_MOCK_PORT", "6443"))
# mock-k8s writes its self-signed cert here (in-cluster SA mount); we pin the
# /status fetch to it rather than disabling TLS verification.
SA_MOUNT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount"
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
HISTORY_BUFFER_SIZE = 200

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

# Login rate-limit. Sliding window per source IP: when N failed attempts
# land within W seconds, subsequent /api/login requests from that IP get
# 429 + Retry-After until the oldest attempt ages out. The deque has a
# fixed maxlen — older entries auto-evict, so we never grow unbounded
# per-IP. A successful login clears the bucket.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("DUNE_ADMIN_UI_LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("DUNE_ADMIN_UI_LOGIN_WINDOW_SECS", "900"))
LOGIN_ATTEMPTS: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque(maxlen=LOGIN_MAX_ATTEMPTS)
)

# In-memory ring buffer of recent command invocations. Surfaced via
# GET /api/history. Each entry: {ts, source, argv, ok, exit_code,
# stdout, stderr}.
HISTORY: collections.deque = collections.deque(maxlen=HISTORY_BUFFER_SIZE)

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
    log; in-memory revocations still apply for the current process."""
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
    REVOCATIONS[jti] = exp
    _save_revocations()
    return True


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


def run_publish(argv: list[str], timeout: int = 30) -> dict:
    result = subprocess.run(
        [PUBLISH_SH, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    ok = result.returncode == 0 and (
        "publish=ok" in result.stdout
        or "publish=db-delete" in result.stdout
        or "publish=db-write" in result.stdout
        or argv[0] in (
            "players", "resolve", "pos", "vehicles", "items", "skills", "items-json",
            "vehicle-list", "db-tables", "db-describe", "db-sample", "db-search", "db-sql",
            "player-state", "char-xp-read", "inventory-list", "tags-get",
            "server-status", "map-markers", "db-backup", "db-backup-list", "spice-list",
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
    HISTORY.append(entry)
    return entry


def run_welcome(sub: str, timeout: int = 60) -> dict:
    """Run scripts/admin_welcome.py <sub> <BASE_DIR> out-of-process (it shells to
    give-item, so keep it isolated + timeout-bounded). Returns {ok, data, ...}
    where `data` is the script's parsed JSON output."""
    res = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "admin_welcome.py"), sub, BASE_DIR],
        capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"raw": res.stdout[:500]}
    ok = res.returncode == 0 and (data.get("ok", True) if isinstance(data, dict) else True)
    return {"ok": ok, "exit_code": res.returncode, "data": data, "stderr": res.stderr[:500]}


def run_schedule(sub: str, *args: str, timeout: int = 60) -> dict:
    """Run scripts/admin_schedule.py <sub> <BASE_DIR> [args] out-of-process
    (it shells to admin-publish for backups/broadcast). Returns {ok, data, ...}
    where `data` is the script's parsed JSON output."""
    res = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "admin_schedule.py"), sub, BASE_DIR, *args],
        capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"raw": res.stdout[:500]}
    ok = res.returncode == 0 and (data.get("ok", True) if isinstance(data, dict) else True)
    return {"ok": ok, "exit_code": res.returncode, "data": data, "stderr": res.stderr[:500]}


def run_market(sub: str, *args: str, timeout: int = 20) -> dict:
    """Run scripts/admin_market.py <sub> [args] <BASE_DIR> out-of-process.
    Returns {ok, data, ...} where `data` is the script's parsed JSON output.
    Read-only (pricing + market summary); no exchange writes in Phase 7a."""
    res = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "admin_market.py"), sub, *args, BASE_DIR],
        capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"raw": res.stdout[:500]}
    ok = res.returncode == 0 and (data.get("ok", True) if isinstance(data, dict) else True)
    return {"ok": ok, "exit_code": res.returncode, "data": data, "stderr": res.stderr[:500]}


def fetch_mock_status(port: int, timeout: int = 5) -> dict | None:
    """GET mock-k8s's /status (HTTPS with a self-signed cert -> verification
    disabled) and return the parsed snapshot, or None if it is unreachable or
    unparseable. /status needs no auth. Used by GET /api/status to read per-map
    instance/scale state; a None result is surfaced to the caller (not silently
    swallowed) via the route's `sources` flags."""
    # Pin to mock-k8s's own self-signed cert (written to the in-cluster SA
    # ca.crt). The cert carries 127.0.0.1 + localhost SANs, so default hostname
    # verification holds and the connection is cryptographically bound to
    # mock-k8s. Only if that ca.crt is absent (mock-k8s not up yet) do we fall
    # back to an unverified loopback fetch.
    ca = os.path.join(SA_MOUNT_PATH, "ca.crt")
    ctx = ssl.create_default_context()
    if os.path.exists(ca):
        try:
            ctx.load_verify_locations(ca)
        except (ssl.SSLError, OSError):
            return None
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    url = f"https://127.0.0.1:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:  # noqa: S310 (localhost, CA-pinned)
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


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


class Handler(BaseHTTPRequestHandler):
    server_version = "DuneAdminHTTP/1.1"

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
        log(f"{self.command} {self.path} -> {format % args}")

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
        """Source IP for rate-limit bucketing. Trusts the socket peer —
        deliberately NOT X-Forwarded-For, because honouring that header
        in front of a proxy that doesn't strip it lets attackers forge
        an arbitrary IP and bypass per-IP rate limits. If a TLS-
        terminating reverse proxy is in front, all logins appear from
        127.0.0.1, which collapses the per-IP bucket into a single
        global bucket — that's a known limitation; operators behind a
        proxy should pair this with per-account lockout at the proxy."""
        try:
            return self.client_address[0]
        except (AttributeError, IndexError):
            return "unknown"

    def _login_rate_limited(self, ip: str) -> int:
        """Return Retry-After seconds if the IP is over its login quota,
        else 0. Drops attempts older than LOGIN_WINDOW_SECONDS from the
        bucket first so the window slides correctly."""
        now = time.time()
        bucket = LOGIN_ATTEMPTS.get(ip)
        if not bucket:
            return 0
        cutoff = now - LOGIN_WINDOW_SECONDS
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            # Bucket is empty after pruning — clear the dict entry so
            # we don't leak memory on transient IPs.
            LOGIN_ATTEMPTS.pop(ip, None)
            return 0
        if len(bucket) < LOGIN_MAX_ATTEMPTS:
            return 0
        # Bucket is full and oldest entry is still within window: throttled.
        retry_after = int(bucket[0] + LOGIN_WINDOW_SECONDS - now) + 1
        return max(retry_after, 1)

    def _record_login_failure(self, ip: str) -> None:
        LOGIN_ATTEMPTS[ip].append(time.time())

    def _cookie_flags(self) -> str:
        """Cookie attribute string shared by session + csrf cookies."""
        secure = UI_DOMAIN and LISTEN_ADDR not in _LOOPBACK_ADDRS
        bits = [
            "Path=/",
            f"Max-Age={SESSION_TTL_SECONDS}",
            "SameSite=Strict",
        ]
        if secure:
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
        """Set-Cookie headers that expire the session + csrf cookies."""
        secure = UI_DOMAIN and LISTEN_ADDR not in _LOOPBACK_ADDRS
        bits = ["Path=/", "Max-Age=0", "SameSite=Strict"]
        if secure:
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
        # status merged with per-map online player counts (from Postgres via
        # admin-publish server-status; admin-http holds no PG connection). A
        # missing source degrades gracefully and is flagged in `sources`.
        if path == "/api/status":
            mock = fetch_mock_status(MOCK_K8S_PORT)
            entry = run_publish(["server-status"], timeout=10)
            counts = parse_player_counts(entry["stdout"]) if entry["ok"] else {}
            grid = merge_status(mock or {}, counts)
            grid["ok"] = True
            grid["sources"] = {"mockK8s": mock is not None, "playerCounts": entry["ok"]}
            if mock is None:
                grid["warning"] = "mock-k8s /status unreachable; showing player counts only"
            self._write(200, grid)
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
            cfg = {}
            try:
                with open(os.path.join(BASE_DIR, "data", "admin", "market-bot.json"), encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                pass
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
            limit = max(1, min(HISTORY_BUFFER_SIZE, int(query.get("limit", ["50"])[0])))
            entries = list(HISTORY)[-limit:]
            self._write(200, {"entries": entries, "total": len(HISTORY)})
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

        # Manual task trigger: /api/tasks/trigger/<backup|restart> (auth+csrf).
        if path.startswith("/api/tasks/trigger/"):
            if not self._auth_ok():
                self._write(401, {"error": "auth required"})
                return
            if not self._csrf_ok():
                self._write(403, {"error": "csrf token missing or invalid"})
                return
            task = path[len("/api/tasks/trigger/"):]
            sub = {"backup": "run-backup", "restart": "run-restart"}.get(task)
            if sub is None:
                self._write(400, {"error": "unknown task (backup|restart)"})
                return
            r = run_schedule(sub, timeout=600 if task == "backup" else 60)
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
            entry = run_publish(["account-delete", player, confirm, reason], timeout=20)
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
            retry_after = self._login_rate_limited(client_ip)
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
                self._record_login_failure(client_ip)
                # Small fixed delay to slow down brute-force guessing.
                time.sleep(0.3)
                log(
                    f"WARN failed login from {client_ip} "
                    f"({len(LOGIN_ATTEMPTS.get(client_ip, ()))}/{LOGIN_MAX_ATTEMPTS} in window)"
                )
                self._write(401, {"error": "invalid password"})
                return
            # Successful login — clear any prior failure history for this
            # IP so a legitimate operator who fat-fingered a few times
            # isn't punished for the next 15 minutes.
            LOGIN_ATTEMPTS.pop(client_ip, None)
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
# Main.
# --------------------------------------------------------------------------
_LOOPBACK_ADDRS = {"127.0.0.1", "::1", "localhost"}


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
        log(f"  ratelim : {LOGIN_MAX_ATTEMPTS} failed /api/login per {LOGIN_WINDOW_SECONDS}s per-IP")
        log(f"  revoked : {len(REVOCATIONS)} session token(s) on the revocation list")
    else:
        log(f"  auth    : {'shared-secret' if SHARED_AUTH else 'open (loopback only)'}")
    HTTPServer((LISTEN_ADDR, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
