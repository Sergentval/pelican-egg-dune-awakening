#!/usr/bin/env python3
"""Persisted command audit for the admin panel (GET /api/history).

Replaces an in-memory deque that had two problems:

  * It died with the process. The panel restarts with the server — the
    scheduler's unattended restart, `panel restart`, a reinstall — so the
    audit trail an operator wants *precisely when something went wrong*
    was the first thing lost.

  * MapTab polls /api/map/markers every 4 seconds and every poll went
    through run_publish, so 200 buffer slots of `map-markers` evicted
    every real action in about thirteen minutes. The trail was already
    unusable before any restart wiped it.

So: skip read-only traffic, persist what remains under server/state/
(which a reinstall does not touch), and bound what one entry can cost.

The stored shape matches what the SPA already renders — {ts, argv, ok,
exit_code, stdout, stderr} — so nothing in web/ has to change.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

# Subcommands that only read. Everything else is recorded, so a new
# subcommand is audited by default and has to be opted OUT deliberately —
# the failure mode of forgetting is a noisier trail, not a missing one.
#
# db-sql is here because it is read-only twice over: is_read_only_sql()
# rejects anything but select/explain/show/with, and the psql session it
# runs in is pinned read-only. db-backup is deliberately NOT here — it
# writes a file, so it belongs in the trail.
READ_ONLY_SUBCOMMANDS = frozenset({
    "players", "resolve", "pos", "vehicles", "items", "skills", "items-json",
    "vehicle-list", "db-tables", "db-describe", "db-sample", "db-search", "db-sql",
    "player-state", "char-xp-read", "inventory-list", "tags-get",
    "server-status", "farm-player-count", "map-markers", "db-backup-list",
    "spice-list", "world-partition-list", "market-bot-status",
    "char-backup-list", "doctor", "bases", "base-water",
})

DEFAULT_KEEP = 500          # rows retained; ~13 months of real actions in practice
MAX_STREAM_CHARS = 4000     # per stdout/stderr — a db dump must not bloat the trail
_TRUNCATED = "\n… [truncated in the audit trail]"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_history (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  argv      TEXT    NOT NULL,
  ok        INTEGER NOT NULL,
  exit_code INTEGER,
  stdout    TEXT,
  stderr    TEXT
);
CREATE INDEX IF NOT EXISTS idx_command_history_id ON command_history (id DESC);
"""


def history_path(base: str) -> str:
    """server/state/ so the trail survives a reinstall, which wipes data/."""
    return os.path.join(base, "server", "state", "admin-history.db")


def is_recordable(argv) -> bool:
    """True for anything that changed the world. Unknown subcommands count
    as changes — see READ_ONLY_SUBCOMMANDS."""
    if not argv:
        return False
    return str(argv[0]) not in READ_ONLY_SUBCOMMANDS


def _clip(text) -> str:
    text = text or ""
    return text if len(text) <= MAX_STREAM_CHARS else text[:MAX_STREAM_CHARS] + _TRUNCATED


def _connect(base: str):
    """One short-lived connection per operation: the HTTP server is
    threaded and sqlite3 connections are not shareable across threads.
    The timeout covers two requests landing together."""
    path = history_path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def record(base: str, entry: dict, keep: int = DEFAULT_KEEP) -> bool:
    """Persist one entry. Returns False when the entry was skipped as
    read-only. Storage failures are swallowed — an unwritable audit trail
    must never take an admin command down with it."""
    if not is_recordable(entry.get("argv")):
        return False
    try:
        with _connect(base) as conn:
            conn.execute(
                "INSERT INTO command_history (ts,argv,ok,exit_code,stdout,stderr) "
                "VALUES (?,?,?,?,?,?)",
                (int(entry.get("ts") or time.time()),
                 json.dumps([str(a) for a in entry.get("argv") or []]),
                 1 if entry.get("ok") else 0,
                 entry.get("exit_code"),
                 _clip(entry.get("stdout")),
                 _clip(entry.get("stderr"))))
            conn.execute(
                "DELETE FROM command_history WHERE id NOT IN "
                "(SELECT id FROM command_history ORDER BY id DESC LIMIT ?)", (keep,))
        return True
    except (sqlite3.Error, OSError):
        return False


def note(base: str, text: str, detail: str = "", ok: bool = True) -> bool:
    """Record a non-command event — a boot, say — so a restart is visible
    in the trail instead of an unexplained gap in it."""
    return record(base, {"ts": int(time.time()), "argv": [text], "ok": ok,
                         "exit_code": 0, "stdout": detail, "stderr": ""})


def recent(base: str, limit: int = 50) -> tuple[list, int]:
    """(entries oldest-first, total retained). Oldest-first matches what
    the in-memory deque returned, which the SPA reverses for display."""
    try:
        with _connect(base) as conn:
            total = conn.execute("SELECT COUNT(*) FROM command_history").fetchone()[0]
            rows = conn.execute(
                "SELECT ts,argv,ok,exit_code,stdout,stderr FROM command_history "
                "ORDER BY id DESC LIMIT ?", (max(1, limit),)).fetchall()
    except (sqlite3.Error, OSError):
        return [], 0
    entries = []
    for ts, argv, ok, code, out, err in reversed(rows):
        try:
            parsed = json.loads(argv)
        except ValueError:
            parsed = [argv]
        entries.append({"ts": ts, "argv": parsed, "ok": bool(ok),
                        "exit_code": code, "stdout": out or "", "stderr": err or ""})
    return entries, total
