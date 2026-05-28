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
import hashlib
import hmac
import json
import mimetypes
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote


# --------------------------------------------------------------------------
# Config — env vars resolved once at process start.
# --------------------------------------------------------------------------
LISTEN_ADDR = os.environ.get("DUNE_ADMIN_HTTP_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("DUNE_ADMIN_HTTP_PORT", "8089"))
SHARED_AUTH = os.environ.get("DUNE_ADMIN_HTTP_AUTH", "")
BASE_DIR = os.environ.get("DUNE_BASE_DIR", "/home/container")
SCRIPTS_DIR = BASE_DIR + "/scripts"
PUBLISH_SH = SCRIPTS_DIR + "/admin-publish.sh"

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

# In-memory ring buffer of recent command invocations. Surfaced via
# GET /api/history. Each entry: {ts, source, argv, ok, exit_code,
# stdout, stderr}.
HISTORY: collections.deque = collections.deque(maxlen=HISTORY_BUFFER_SIZE)

# Steam personaname/avatar cache. {steam_id: (cached_at_ts, info_dict)}.
STEAM_CACHE: dict[str, tuple[float, dict]] = {}


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


def issue_session_token(subject: str = "admin") -> str:
    if not UI_SESSION_SECRET:
        raise RuntimeError("DUNE_ADMIN_UI_SESSION_SECRET not set — prestart.sh failed to generate it")
    now = int(time.time())
    payload = {"iat": now, "exp": now + SESSION_TTL_SECONDS, "sub": subject}
    payload_b64 = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(
        UI_SESSION_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).digest()
    return f"{payload_b64}.{_b64u(sig)}"


def verify_session_token(token: str) -> dict | None:
    """Returns the decoded payload if the token is valid + unexpired, else None."""
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
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


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
    if sub == "raw":
        return [sub, json.dumps(body, separators=(",", ":"))]
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
        or argv[0] in ("players", "resolve", "pos", "vehicles", "items", "skills", "items-json")
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
        origin = self.headers.get("Origin", "")
        allowed = ""
        if UI_DOMAIN:
            wanted = {f"https://{UI_DOMAIN}", f"http://{UI_DOMAIN}"}
            if origin in wanted:
                allowed = origin
        elif origin:
            allowed = origin
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _write(self, status: int, payload, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload, indent=2)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log(f"{self.command} {self.path} -> {format % args}")

    # ------ auth ---------------------------------------------------------
    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        return header[7:].strip()

    def _auth_ok(self) -> bool:
        token = self._bearer()
        # UI mode: session token issued by /api/login
        if UI_ENABLED:
            return verify_session_token(token) is not None
        # Internal mode: optional shared-secret Bearer
        if not SHARED_AUTH:
            return True
        return token == SHARED_AUTH

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

        if path == "/api/me":
            token = self._bearer()
            payload = verify_session_token(token) if UI_ENABLED else {"sub": "internal"}
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
            self._write(200 if entry["ok"] else 502, entry)
            return

        if path.startswith("/api/pos/"):
            # URL-decode so `name:Sergentval` survives a fetch() call from
            # the SPA (which encodeURIComponent's the colon to %3A).
            player = unquote(path[len("/api/pos/"):])
            entry = run_publish(["pos", player], timeout=10)
            self._write(200 if entry["ok"] else 502, entry)
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
                200 if entry["ok"] else 502,
                entry["stdout"] + entry["stderr"],
                content_type="text/plain",
            )
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

    # ------ POST ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            self._write(400, {"error": "invalid JSON body", "detail": str(exc)})
            return

        # /api/login is the only POST that bypasses Bearer auth.
        if path == "/api/login":
            if not UI_ENABLED:
                self._write(400, {"error": "login not available in internal mode"})
                return
            if not UI_PASSWORD:
                self._write(503, {"error": "DUNE_ADMIN_UI_PASSWORD not set on the server"})
                return
            supplied = str(body.get("password", ""))
            if not hmac.compare_digest(supplied, UI_PASSWORD):
                # Small fixed delay to slow down brute-force guessing.
                time.sleep(0.3)
                self._write(401, {"error": "invalid password"})
                return
            token = issue_session_token()
            self._write(
                200,
                {
                    "token": token,
                    "expires_in": SESSION_TTL_SECONDS,
                    "type": "Bearer",
                },
            )
            return

        if not self._auth_ok():
            self._write(401, {"error": "auth required"})
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
        self._write(200 if entry["ok"] else 502, entry)


# --------------------------------------------------------------------------
# Main.
# --------------------------------------------------------------------------
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
    mode = "UI" if UI_ENABLED else "internal"
    log(f"mode={mode} bind={LISTEN_ADDR}:{LISTEN_PORT} scripts={SCRIPTS_DIR}")
    if UI_ENABLED:
        log(f"  web root: {UI_WEB_ROOT} ({'present' if (UI_WEB_ROOT / 'index.html').is_file() else 'missing — SPA not built yet'})")
        log(f"  domain  : {UI_DOMAIN or '(none — using IP+port directly)'}")
        log(f"  data    : vehicles={len(_DATA['vehicles'])} items={len(_DATA['items'])} skills={len(_DATA['skills'])}")
        log(f"  steam   : {'STEAM_API_KEY set — persona lookups enabled' if STEAM_API_KEY else 'STEAM_API_KEY blank — persona lookups disabled'}")
    else:
        log(f"  auth    : {'shared-secret' if SHARED_AUTH else 'open (loopback only)'}")
    HTTPServer((LISTEN_ADDR, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
