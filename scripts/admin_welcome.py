#!/usr/bin/env python3
"""Welcome kits (Phase 6) — port of dune-admin welcome_store / welcome_package.

A configurable starter package granted once per player per package version,
tracked in a sqlite ledger. The scan lists ONLINE players (via admin-publish.sh
db-sql — admin holds no PG connection), skips anyone already recorded for the
active version, and grants the rest via admin-publish.sh give-item (which routes
online->RMQ / offline->DB and capacity-checks). Bump the version to re-grant.

Pure helpers (validate_items, parse_accounts_csv, scan_once) + the WelcomeLedger
are unit-tested; the subprocess/file I/O lives in the thin wrappers + CLI below.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_items(items) -> None:
    """Raise ValueError unless items is a non-empty list of {template, qty>0,
    quality>=0}."""
    if not items:
        raise ValueError("welcome package has no items")
    for it in items:
        t = str(it.get("template", "")).strip()
        if not t:
            raise ValueError("welcome item template must not be empty")
        q = it.get("qty")
        if not isinstance(q, int) or isinstance(q, bool) or q <= 0:
            raise ValueError(f"welcome item {t!r} qty must be an integer > 0")
        ql = it.get("quality", 0)
        if not isinstance(ql, int) or isinstance(ql, bool) or ql < 0:
            raise ValueError(f"welcome item {t!r} quality must be an integer >= 0")


def parse_accounts_csv(text):
    """Parse the `fls,account_id` CSV from db-sql into [{fls, account_id}].
    Tolerates the header row, blanks, CRLF, and malformed rows (skipped)."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        fls, acct = parts[0].strip(), parts[1].strip()
        if fls.lower() == "fls" or not acct.isdigit():
            continue
        out.append({"fls": fls, "account_id": int(acct)})
    return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS welcome_grants (
    fls_id          TEXT    NOT NULL,
    package_version TEXT    NOT NULL,
    account_id      INTEGER NOT NULL,
    character_name  TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL,
    granted_at      TEXT    NOT NULL DEFAULT '',
    attempts        INTEGER NOT NULL DEFAULT 1,
    last_error      TEXT    NOT NULL DEFAULT '',
    detected_at     TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (fls_id, package_version, account_id)
);"""


class WelcomeLedger:
    """sqlite ledger of welcome-package grants (one connection per instance)."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def granted_exists(self, fls: str, version: str, account_id: int) -> bool:
        # A row in ANY status (granted OR failed) blocks a re-grant; clear a
        # failed row via delete_failed to retry. Matches dune-admin.
        cur = self.conn.execute(
            "SELECT 1 FROM welcome_grants WHERE fls_id=? AND package_version=? AND account_id=? LIMIT 1",
            (fls, version, account_id))
        return cur.fetchone() is not None

    def record_granted(self, fls, version, account_id, character_name):
        now = _now()
        self.conn.execute(
            """INSERT INTO welcome_grants
                 (fls_id,package_version,account_id,character_name,status,granted_at,attempts,last_error,detected_at,updated_at)
               VALUES (?,?,?,?,'granted',?,1,'',?,?)
               ON CONFLICT(fls_id,package_version,account_id) DO UPDATE SET
                 status='granted', granted_at=excluded.granted_at,
                 character_name=excluded.character_name,
                 attempts=welcome_grants.attempts+1, last_error='',
                 updated_at=excluded.updated_at""",
            (fls, version, account_id, character_name, now, now, now))
        self.conn.commit()

    def record_failed(self, fls, version, account_id, character_name, err):
        now = _now()
        self.conn.execute(
            """INSERT INTO welcome_grants
                 (fls_id,package_version,account_id,character_name,status,granted_at,attempts,last_error,detected_at,updated_at)
               VALUES (?,?,?,?,'failed','',1,?,?,?)
               ON CONFLICT(fls_id,package_version,account_id) DO UPDATE SET
                 status='failed', character_name=excluded.character_name,
                 attempts=welcome_grants.attempts+1, last_error=excluded.last_error,
                 updated_at=excluded.updated_at""",
            (fls, version, account_id, character_name, err, now, now))
        self.conn.commit()

    def delete_failed(self, fls, version, account_id) -> int:
        cur = self.conn.execute(
            "DELETE FROM welcome_grants WHERE fls_id=? AND package_version=? AND account_id=? AND status='failed'",
            (fls, version, account_id))
        self.conn.commit()
        return cur.rowcount

    def delete_all_failed(self) -> int:
        cur = self.conn.execute("DELETE FROM welcome_grants WHERE status='failed'")
        self.conn.commit()
        return cur.rowcount

    def list_grants(self, limit: int = 100):
        limit = 100 if (limit <= 0 or limit > 500) else limit
        cols = ["fls_id", "package_version", "account_id", "character_name",
                "status", "granted_at", "attempts", "last_error", "updated_at"]
        cur = self.conn.execute(
            f"SELECT {','.join(cols)} FROM welcome_grants ORDER BY updated_at DESC LIMIT ?",
            (limit,))
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def stats(self):
        d = {"granted": 0, "failed": 0}
        for status, n in self.conn.execute("SELECT status,count(*) FROM welcome_grants GROUP BY status"):
            d[status] = n
        return d


def render_announce(cfg, character_name):
    """Optional welcome broadcast (title, body) for a freshly-granted player, or None
    when announce is disabled or has no body. {name} in the title/body is replaced
    with the character name (or 'A new player' when unknown). Pure. NB: there is no
    per-player whisper RPC, so this is a SERVER-WIDE broadcast (everyone sees it)."""
    if not cfg.get("announce"):
        return None
    body = str(cfg.get("announce_text", "")).strip()
    if not body:
        return None
    title = str(cfg.get("announce_title", "Welcome!")).strip() or "Welcome!"
    name = str(character_name or "").strip() or "A new player"
    return title.replace("{name}", name), body.replace("{name}", name)


def scan_once(version, items, accounts, ledger, grant_fn, on_granted=None):
    """Grant `items` (version `version`) to each account not already recorded.
    grant_fn(account, items) returns a list of failure/skip reasons ([] = full
    success). on_granted(account), if given, is called once per FRESH successful
    grant (used to fire the optional welcome broadcast). Accounts with an empty fls
    are silently ignored. Returns counts."""
    granted = failed = skipped = 0
    for acc in accounts:
        fls = str(acc.get("fls", "")).strip()
        if not fls:
            continue
        aid = acc["account_id"]
        if ledger.granted_exists(fls, version, aid):
            skipped += 1
            continue
        reasons = grant_fn(acc, items)
        if reasons:
            ledger.record_failed(fls, version, aid, acc.get("character_name", ""), "; ".join(reasons))
            failed += 1
        else:
            ledger.record_granted(fls, version, aid, acc.get("character_name", ""))
            granted += 1
            if on_granted is not None:
                on_granted(acc)
    return {"granted": granted, "failed": failed, "skipped": skipped}


# --------------------------------------------------------------------------
# I/O wrappers + CLI (not unit-tested; exercised live).
# --------------------------------------------------------------------------
def _publish(base):
    return os.path.join(base, "scripts", "admin-publish.sh")


def ledger_path(base):
    return os.path.join(base, "server", "state", "welcome-kits.db")


def config_path(base):
    return os.path.join(base, "data", "admin", "welcome-kit.json")


def load_config(base):
    with open(config_path(base), encoding="utf-8") as f:
        return json.load(f)


def active_package(cfg):
    ver = cfg.get("active_version", "")
    for p in cfg.get("packages", []):
        if p.get("version") == ver:
            return p
    pkgs = cfg.get("packages", [])
    return pkgs[0] if pkgs else None


def list_online_accounts(base):
    sql = ('SELECT a."user" AS fls, a.id AS account_id '
           'FROM dune.encrypted_accounts a '
           'JOIN dune.encrypted_player_state ps ON ps.account_id=a.id '
           "WHERE ps.online_status='Online'")
    res = subprocess.run(["bash", _publish(base), "db-sql", sql],
                         capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError("list online accounts failed: " + (res.stderr or "").strip())
    return parse_accounts_csv(res.stdout)


def grant_account(base, account, items):
    """Grant each item to account['fls'] via give-item. Returns failure reasons
    ([] = all granted). give-item routes online->RMQ / offline->DB itself."""
    reasons = []
    for it in items:
        argv = ["bash", _publish(base), "give-item", account["fls"],
                it["template"], str(it["qty"]), str(it.get("quality", 0))]
        res = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        ok = res.returncode == 0 and ("publish=db-write" in res.stdout or "publish=ok" in res.stdout)
        if not ok:
            tail = (res.stderr or res.stdout or f"rc={res.returncode}").strip().splitlines()
            reasons.append(f"{it['template']}: {tail[-1] if tail else 'grant failed'}")
    return reasons


def broadcast_welcome(base, title, body, duration=20):
    """Fire a server-wide ServiceBroadcast for a welcome message. Best-effort: a
    broadcast failure must NEVER fail the grant it follows."""
    try:
        subprocess.run(["bash", _publish(base), "broadcast", title, body, str(duration)],
                       capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def run_scan(base, ledger=None):
    cfg = load_config(base)
    if not cfg.get("enabled"):
        return {"ok": True, "disabled": True, "granted": 0, "failed": 0, "skipped": 0}
    pkg = active_package(cfg)
    if not pkg:
        return {"ok": False, "error": "no active welcome package"}
    items = pkg.get("items", [])
    try:
        validate_items(items)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    own = ledger is None
    if ledger is None:
        ledger = WelcomeLedger(ledger_path(base))
    try:
        accounts = list_online_accounts(base)

        def _announce(acc):
            msg = render_announce(cfg, acc.get("character_name", ""))
            if msg:
                broadcast_welcome(base, msg[0], msg[1])

        r = scan_once(pkg["version"], items, accounts, ledger,
                      lambda a, i: grant_account(base, a, i),
                      on_granted=_announce)
        r.update({"ok": True, "version": pkg["version"], "online": len(accounts)})
        return r
    finally:
        if own:
            ledger.close()


def _main(argv):
    if len(argv) < 2:
        print("usage: admin_welcome.py <scan|status|list|retry-failed|scan-loop> [BASE]", file=sys.stderr)
        return 2
    cmd = argv[1]
    base = argv[2] if len(argv) > 2 else os.environ.get("DUNE_BASE_DIR", "/home/container")
    if cmd == "scan":
        print(json.dumps(run_scan(base)))
        return 0
    if cmd == "status":
        cfg = load_config(base)
        led = WelcomeLedger(ledger_path(base))
        try:
            print(json.dumps({"enabled": bool(cfg.get("enabled")),
                              "active_version": cfg.get("active_version"),
                              "packages": [p.get("version") for p in cfg.get("packages", [])],
                              "ledger": led.stats()}))
        finally:
            led.close()
        return 0
    if cmd == "list":
        led = WelcomeLedger(ledger_path(base))
        try:
            print(json.dumps(led.list_grants(200)))
        finally:
            led.close()
        return 0
    if cmd == "retry-failed":
        led = WelcomeLedger(ledger_path(base))
        try:
            print(json.dumps({"cleared": led.delete_all_failed()}))
        finally:
            led.close()
        return 0
    if cmd == "scan-loop":
        # 6b entrypoint launches this; loops at scan_interval_secs, no-ops while
        # disabled, survives a malformed config without crashing the loop.
        while True:
            try:
                cfg = load_config(base)
                interval = max(int(cfg.get("scan_interval_secs", 60)), 15)
                r = run_scan(base)
                if r.get("granted") or r.get("failed"):
                    print(f"[welcome] {json.dumps(r)}", flush=True)
            except Exception as e:  # never let the loop die
                interval = 60
                print(f"[welcome] scan error: {e}", flush=True)
            time.sleep(interval)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
