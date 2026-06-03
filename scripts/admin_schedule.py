#!/usr/bin/env python3
"""Unattended scheduler (Phase 2): due-logic + run ledger + auto-restart /
auto-backup tick loop. Mirrors admin_welcome (sqlite ledger + scan-loop). Config
in data/admin/schedule.json (OFF default); ledger in server/state/scheduler.db.

Restart is NON-BLOCKING: when a restart slot is due, the loop broadcasts the
in-game countdown (admin-publish shutdown Restart) and stores a pending restart at
now+warn_lead; a later tick fires the Pelican client-API power:restart (a clean
stop, which console.sh makes data-safe). Backup runs admin-publish db-backup.

Pure due-logic (restart_due/backup_due/_slot_today/load_config) is unit-tested;
the I/O (subprocess, HTTP, sqlite) is thin and isolated for monkeypatching.
"""
import http.client
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

DEFAULT_TICK_SECS = 30
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PENDING_KEY = "pending_restart_at"

DEFAULT_CONFIG = {
    "restart": {"enabled": False, "time": "08:00", "days": list(DAYS),
                "warn_lead_secs": 300, "warn_freq_secs": 60, "catch_up_grace_secs": 3600},
    "backup": {"enabled": False, "every_hours": 24, "retention": 7},
}


def _now_dt():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def config_path(base):
    return os.path.join(base, "data", "admin", "schedule.json")


def ledger_path(base):
    return os.path.join(base, "server", "state", "scheduler.db")


def load_config(base):
    """Read schedule.json, shallow-merged over defaults so missing keys never crash."""
    out = {"restart": dict(DEFAULT_CONFIG["restart"]), "backup": dict(DEFAULT_CONFIG["backup"])}
    try:
        with open(config_path(base), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return out
    if isinstance(c, dict):
        for k in out:
            if isinstance(c.get(k), dict):
                out[k].update(c[k])
    return out


def validate_config(raw):
    """Validate + coerce a posted config to the canonical shape. Returns
    (config, None) or (None, error). Pure (no I/O)."""
    if not isinstance(raw, dict):
        return None, "config must be an object"
    r, b = raw.get("restart", {}), raw.get("backup", {})
    if not isinstance(r, dict) or not isinstance(b, dict):
        return None, "restart and backup must be objects"
    out = {"restart": dict(DEFAULT_CONFIG["restart"]), "backup": dict(DEFAULT_CONFIG["backup"])}
    out["restart"]["enabled"] = bool(r.get("enabled", False))
    t = str(r.get("time", out["restart"]["time"]))
    if _slot_today(_now_dt(), t) is None:
        return None, "restart.time must be HH:MM"
    out["restart"]["time"] = t
    days = r.get("days", out["restart"]["days"])
    if not isinstance(days, list) or not days or not all(d in DAYS for d in days):
        return None, "restart.days must be a non-empty subset of mon..sun"
    out["restart"]["days"] = [d for d in DAYS if d in days]  # canonical order, deduped
    for k in ("warn_lead_secs", "warn_freq_secs", "catch_up_grace_secs"):
        try:
            out["restart"][k] = max(int(r.get(k, out["restart"][k])), 0)
        except (TypeError, ValueError):
            return None, f"restart.{k} must be an integer"
    out["backup"]["enabled"] = bool(b.get("enabled", False))
    try:
        out["backup"]["every_hours"] = max(int(b.get("every_hours", 24)), 1)
        out["backup"]["retention"] = max(int(b.get("retention", 7)), 1)
    except (TypeError, ValueError):
        return None, "backup.every_hours and backup.retention must be integers"
    return out, None


def save_config(base, config):
    path = config_path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)  # atomic


def _slot_today(now, hhmm):
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except (ValueError, AttributeError):
        return None
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def restart_due(rcfg, now, last_warn):
    """True if a restart should be WARNED now: enabled, today is an allowed day,
    now is within [slot, slot+grace), and we haven't already warned this slot."""
    if not rcfg.get("enabled"):
        return False
    if DAYS[now.weekday()] not in (rcfg.get("days") or DAYS):
        return False
    slot = _slot_today(now, rcfg.get("time", "08:00"))
    if slot is None or now < slot:
        return False
    grace = max(int(rcfg.get("catch_up_grace_secs", 3600)), 0)
    if now >= slot + timedelta(seconds=grace):
        return False  # missed the window — don't fire a stale catch-up restart
    lw = _parse_iso(last_warn)
    if lw is not None and lw >= slot:
        return False  # already warned for this slot
    return True


def backup_due(bcfg, now, last_backup):
    if not bcfg.get("enabled"):
        return False
    lb = _parse_iso(last_backup)
    if lb is None:
        return True
    return now >= lb + timedelta(hours=max(int(bcfg.get("every_hours", 24)), 1))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT NOT NULL, status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scheduler_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class SchedulerLedger:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def record(self, task, status, detail=""):
        self.conn.execute(
            "INSERT INTO scheduler_runs (task,status,detail,at) VALUES (?,?,?,?)",
            (task, status, (detail or "")[:500], _iso(_now_dt())))
        self.conn.commit()

    def last(self, task):
        cur = self.conn.execute(
            "SELECT at FROM scheduler_runs WHERE task=? ORDER BY id DESC LIMIT 1", (task,))
        r = cur.fetchone()
        return r[0] if r else None

    def list_runs(self, limit=50):
        limit = 50 if (limit <= 0 or limit > 500) else limit
        cur = self.conn.execute(
            "SELECT task,status,detail,at FROM scheduler_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(zip(["task", "status", "detail", "at"], row)) for row in cur.fetchall()]

    def get_state(self, k):
        cur = self.conn.execute("SELECT v FROM scheduler_state WHERE k=?", (k,))
        r = cur.fetchone()
        return r[0] if r else None

    def set_state(self, k, v):
        self.conn.execute(
            "INSERT INTO scheduler_state (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.conn.commit()

    def clear_state(self, k):
        self.conn.execute("DELETE FROM scheduler_state WHERE k=?", (k,))
        self.conn.commit()


# -- task runners (I/O; isolated so run_tick can be tested with fakes) ----------
def _publish(base):
    return os.path.join(base, "scripts", "admin-publish.sh")


def restart_configured():
    return all(os.environ.get(k) for k in
               ("DUNE_PELICAN_URL", "DUNE_PELICAN_CLIENT_KEY", "DUNE_PELICAN_SERVER_ID"))


def run_backup(base):
    try:
        r = subprocess.run(["bash", _publish(base), "db-backup"],
                           capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"backup error: {e}"[:300]
    ok = r.returncode == 0 and "backup=ok" in r.stdout
    if ok:
        return True, (r.stdout.strip().splitlines() or [""])[-1]
    return False, (r.stderr.strip()[:300] or "pg_dump failed")


def broadcast_restart(base, lead, freq):
    try:
        r = subprocess.run(["bash", _publish(base), "shutdown", "Restart", str(lead), str(freq)],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"broadcast error: {e}"[:300]
    return r.returncode == 0, (r.stdout.strip() or r.stderr.strip())[:300]


class _ResolvingHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that TCP-connects to a fixed IP while keeping TLS SNI,
    certificate validation, and the Host header pinned to the original hostname
    — the Python equivalent of `curl --resolve host:port:ip`.

    Used so the in-container scheduler can reach the Pelican panel straight over
    the wg tunnel (e.g. 10.99.0.1) instead of via public DNS, bypassing a CDN/
    proxy such as Cloudflare that may challenge the container's egress IP. The
    cert still has to be valid for the real hostname, so this is not a
    verification downgrade — only a routing override."""

    def __init__(self, host, resolve_ip, *, context=None, **kw):
        context = context or ssl.create_default_context()
        super().__init__(host, context=context, **kw)
        self._resolve_ip = resolve_ip
        self._ssl_ctx = context

    def connect(self):
        sock = socket.create_connection((self._resolve_ip, self.port), self.timeout)
        self.sock = self._ssl_ctx.wrap_socket(sock, server_hostname=self.host)


def _power_endpoint(url, sid):
    """Pure: parse (scheme, host, port, path) for a server's power endpoint."""
    p = urllib.parse.urlsplit(url)
    scheme = p.scheme or "https"
    host = p.hostname or ""
    port = p.port or (443 if scheme == "https" else 80)
    return scheme, host, port, f"/api/client/servers/{sid}/power"


def _open_power_conn(scheme, host, port, resolve_ip, timeout):
    """Build the HTTP(S) connection for the power POST. When resolve_ip is set
    the TCP target is overridden to that IP (SNI/cert/Host stay `host`)."""
    if scheme == "https":
        ctx = ssl.create_default_context()
        if resolve_ip:
            return _ResolvingHTTPSConnection(host, resolve_ip, port=port, timeout=timeout, context=ctx)
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=ctx)
    return http.client.HTTPConnection(resolve_ip or host, port=port, timeout=timeout)


def pelican_restart():
    """POST the Pelican client-API power:restart. Needs DUNE_PELICAN_URL +
    DUNE_PELICAN_CLIENT_KEY + DUNE_PELICAN_SERVER_ID env. Optional
    DUNE_PELICAN_RESOLVE pins the TCP target to a given IP (curl --resolve
    style) so an in-container restart can bypass a CDN in front of the panel."""
    url = os.environ.get("DUNE_PELICAN_URL", "").rstrip("/")
    key = os.environ.get("DUNE_PELICAN_CLIENT_KEY", "")
    sid = os.environ.get("DUNE_PELICAN_SERVER_ID", "")
    if not (url and key and sid):
        return False, "restart skipped: DUNE_PELICAN_{URL,CLIENT_KEY,SERVER_ID} not set"
    resolve_ip = os.environ.get("DUNE_PELICAN_RESOLVE", "").strip() or None
    scheme, host, port, path = _power_endpoint(url, sid)
    if not host:
        return False, "restart skipped: DUNE_PELICAN_URL has no host"
    body = json.dumps({"signal": "restart"}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json", "Host": host}
    conn = None
    try:
        conn = _open_power_conn(scheme, host, port, resolve_ip, timeout=20)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        code = resp.status
        resp.read()
        via = f" via {resolve_ip}" if resolve_ip else ""
        return 200 <= code < 300, f"power restart HTTP {code}{via}"
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        return False, f"power restart error: {e}"[:200]
    finally:
        if conn is not None:
            conn.close()


def run_tick(base, ledger, now=None, cfg=None):
    """One scheduler tick. Returns a list of (task, ok, detail) actions taken."""
    now = now or _now_dt()
    cfg = cfg or load_config(base)
    actions = []

    # 1) fire a pending restart whose countdown has elapsed
    pend = _parse_iso(ledger.get_state(PENDING_KEY))
    if pend is not None and now >= pend:
        ok, detail = pelican_restart()
        ledger.record("restart", "ok" if ok else "error", detail)
        ledger.clear_state(PENDING_KEY)
        return [("restart", ok, detail)]  # container is restarting — stop here

    # 2) restart warn (broadcast countdown + arm pending), if due
    rcfg = cfg.get("restart", {})
    if pend is None and restart_due(rcfg, now, ledger.last("restart-warn")):
        if not restart_configured():
            ledger.record("restart-warn", "skipped",
                          "auto-restart enabled but DUNE_PELICAN_{URL,CLIENT_KEY,SERVER_ID} unset")
            actions.append(("restart-warn", False, "not configured"))
        else:
            lead = max(int(rcfg.get("warn_lead_secs", 300)), 0)
            ok, detail = broadcast_restart(base, lead, int(rcfg.get("warn_freq_secs", 60)))
            ledger.record("restart-warn", "ok" if ok else "error", detail)
            ledger.set_state(PENDING_KEY, _iso(now + timedelta(seconds=lead)))
            actions.append(("restart-warn", ok, detail))

    # 3) backup, if due
    bcfg = cfg.get("backup", {})
    if backup_due(bcfg, now, ledger.last("backup")):
        ok, detail = run_backup(base)
        ledger.record("backup", "ok" if ok else "error", detail)
        actions.append(("backup", ok, detail))

    return actions


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    base = argv[2] if len(argv) > 2 else os.environ.get("DUNE_BASE_DIR", "/home/container")
    if cmd == "status":
        led = SchedulerLedger(ledger_path(base))
        out = {"ok": True, "config": load_config(base), "runs": led.list_runs(20),
               "pending_restart": led.get_state(PENDING_KEY),
               "restart_configured": restart_configured()}
        led.close()
        print(json.dumps(out))
        return 0
    if cmd == "runs":
        led = SchedulerLedger(ledger_path(base))
        n = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 50
        print(json.dumps({"ok": True, "runs": led.list_runs(n)}))
        led.close()
        return 0
    if cmd in ("run-backup", "run-restart"):
        led = SchedulerLedger(ledger_path(base))
        if cmd == "run-backup":
            ok, detail = run_backup(base)
            led.record("backup", "ok" if ok else "error", detail)
        elif not restart_configured():
            led.record("restart-warn", "skipped", "not configured")
            ok, detail = False, "auto-restart needs DUNE_PELICAN_{URL,CLIENT_KEY,SERVER_ID}"
        else:
            rcfg = load_config(base).get("restart", {})
            lead = max(int(rcfg.get("warn_lead_secs", 300)), 0)
            ok, detail = broadcast_restart(base, lead, int(rcfg.get("warn_freq_secs", 60)))
            led.set_state(PENDING_KEY, _iso(_now_dt() + timedelta(seconds=lead)))
            led.record("restart-warn", "ok" if ok else "error", detail)
        led.close()
        print(json.dumps({"ok": ok, "detail": detail}))
        return 0
    if cmd == "set-config":
        raw = argv[3] if len(argv) > 3 else ""
        try:
            parsed = json.loads(raw)
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid JSON"}))
            return 1
        cfg, err = validate_config(parsed)
        if err:
            print(json.dumps({"ok": False, "error": err}))
            return 1
        save_config(base, cfg)
        print(json.dumps({"ok": True, "config": cfg}))
        return 0
    if cmd == "scan-loop":
        led = SchedulerLedger(ledger_path(base))
        while True:
            try:
                for (t, ok, d) in run_tick(base, led):
                    print(f"[scheduler] {t} ok={ok} {d}", flush=True)
            except Exception as e:  # never let the loop die
                print(f"[scheduler] tick error: {e}", flush=True)
            time.sleep(DEFAULT_TICK_SECS)
    print("usage: admin_schedule.py <scan-loop|status|runs|run-backup|run-restart> [BASE]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
