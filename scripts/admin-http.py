#!/usr/bin/env python3
"""Tiny HTTP wrapper for scripts/admin-publish.sh.

POSTed admin commands get executed via admin-publish.sh which publishes
to the heartbeats:notifications exchange on rabbit-game. Same publish
pipeline as the console.sh stdin listener — different invocation
surface for tools that prefer HTTP (curl, panel iframes, future GUIs).

The server binds 127.0.0.1 by default so it is only reachable from
inside the container. If you want to expose it to the Pelican panel,
either reverse-proxy from a sibling container or add a port forward
in the egg JSON.

Endpoints:

  GET  /                        -> usage + curl examples
  POST /admin/<subcommand>      -> body is JSON, keys are admin-publish.sh args
  POST /admin/raw               -> body is the inner ServerCommand JSON

Auth: shared secret in the Bearer header, default is the value of
$DUNE_ADMIN_TOKEN if set, else nothing required (since we bind
loopback only). Override the listen address via DUNE_ADMIN_HTTP_ADDR
and the port via DUNE_ADMIN_HTTP_PORT.

Started by scripts/start-admin-http.sh and managed by console.sh's
phased shutdown handler.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


LISTEN_ADDR = os.environ.get("DUNE_ADMIN_HTTP_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("DUNE_ADMIN_HTTP_PORT", "8089"))
SHARED_AUTH = os.environ.get("DUNE_ADMIN_HTTP_AUTH", "")
SCRIPTS_DIR = os.environ.get("DUNE_BASE_DIR", "/home/container") + "/scripts"
PUBLISH_SH = SCRIPTS_DIR + "/admin-publish.sh"


def log(msg: str) -> None:
    print(f"[admin-http] [INFO] {msg}", flush=True)


USAGE = (
    "admin-http — Pelican Dune Awakening admin HTTP wrapper\n"
    "Listening on " + f"{LISTEN_ADDR}:{LISTEN_PORT}" + "\n\n"
    "Examples:\n"
    "  curl -X POST http://127.0.0.1:8089/admin/broadcast \\\n"
    "       -H 'Content-Type: application/json' \\\n"
    "       -d '{\"title\":\"Hi\",\"body\":\"Hello\",\"duration\":15}'\n\n"
    "  curl -X POST http://127.0.0.1:8089/admin/kick \\\n"
    "       -H 'Content-Type: application/json' \\\n"
    "       -d '{\"player_id\":\"fls-12345\"}'\n\n"
    "  curl -X POST http://127.0.0.1:8089/admin/raw \\\n"
    "       -H 'Content-Type: application/json' \\\n"
    "       -d '{\"ServerCommand\":\"AwardXP\",\"PlayerId\":\"fls-1\",\"Experience\":1000}'\n\n"
    "Subcommands accept the same payloads as scripts/admin-publish.sh:\n"
    "  broadcast {title, body, duration?}\n"
    "  shutdown  {type, lead_secs?, freq_secs?}     # type: Restart|Maintenance|Update|cancel\n"
    "  kick      {player_id}                         # player_id '*' = all online\n"
    "  clean     {player_id}                         # DESTRUCTIVE: wipes inventory\n"
    "  reset     {player_id}                         # DESTRUCTIVE: wipes XP+skills\n"
    "  water     {player_id, amount?}\n"
    "  give      {player_id, item, qty?, durability?}\n"
    "  xp        {player_id, amount}                # Category auto-injected\n"
    "  skill     {player_id, module, level}         # module e.g. Swordmaster_T1\n"
    "  points    {player_id, amount}\n"
    "  teleport  {player_id, x, y, z, yaw?}         # exact XYZ\n"
    "  tpsafe    {player_id, x, y, z, yaw?}         # snaps to safe location\n"
    "  vehicle   {player_id, class, x, y, z, template, rotation?, persistent?}\n"
    "  cheat     {player_id, script}                # NO-OP on seabass\n"
    "  exec      {command}                          # NO-OP on seabass\n"
    "  raw       <inline JSON inner payload>\n"
)


def build_argv(sub: str, body: dict) -> list[str]:
    """Map a subcommand + JSON body to the admin-publish.sh argv."""
    if sub == "broadcast":
        return [
            sub,
            str(body["title"]),
            str(body["body"]),
            str(body.get("duration", 30)),
        ]
    if sub == "shutdown":
        return [
            sub,
            str(body["type"]),
            str(body["lead_secs"]),
            str(body.get("freq_secs", 60)),
        ]
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
        # Rotation and persistent are positional after template; pass empty
        # placeholder for rotation if persistent is set but rotation isn't.
        if "rotation" in body or "persistent" in body:
            argv.append(str(body.get("rotation", "")))
            argv.append(str(body.get("persistent", 1.0)))
        return argv
    if sub == "cheat":
        return [sub, str(body["player_id"]), str(body["script"])]
    if sub == "exec":
        return [sub, str(body["command"])]
    if sub == "raw":
        # Body is the full inner JSON object; serialise back to a single arg.
        return [sub, json.dumps(body, separators=(",", ":"))]
    raise ValueError(f"unknown subcommand: {sub}")


class Handler(BaseHTTPRequestHandler):
    def _write(self, status: int, payload: dict | str, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload, indent=2)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — match base signature
        log(f"{self.command} {self.path} -> {format % args}")

    def _auth_ok(self) -> bool:
        if not SHARED_AUTH:
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {SHARED_AUTH}"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/healthz", "/usage"):
            if self.path == "/healthz":
                self._write(200, {"ok": True})
                return
            self._write(200, USAGE, content_type="text/plain")
            return
        self._write(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self._write(401, {"error": "auth required"})
            return

        url = urlparse(self.path)
        if not url.path.startswith("/admin/"):
            self._write(404, {"error": "unknown route", "path": self.path})
            return
        sub = url.path[len("/admin/") :]

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            self._write(400, {"error": "invalid JSON body", "detail": str(exc)})
            return

        try:
            argv = build_argv(sub, body)
        except KeyError as exc:
            self._write(400, {"error": "missing field", "field": str(exc)})
            return
        except ValueError as exc:
            self._write(400, {"error": str(exc)})
            return

        log(f"invoke admin-publish.sh {shlex.join(argv)}")
        result = subprocess.run(
            [PUBLISH_SH, *argv],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = result.returncode == 0 and "publish=ok" in result.stdout
        self._write(
            200 if ok else 502,
            {
                "ok": ok,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )


def main() -> None:
    if not os.access(PUBLISH_SH, os.X_OK):
        print(f"[admin-http] [ERROR] admin-publish.sh not executable at {PUBLISH_SH}", file=sys.stderr)
        sys.exit(1)
    log(f"binding on {LISTEN_ADDR}:{LISTEN_PORT}, scripts={SCRIPTS_DIR}, auth={'on' if SHARED_AUTH else 'off (localhost)'}")
    HTTPServer((LISTEN_ADDR, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
