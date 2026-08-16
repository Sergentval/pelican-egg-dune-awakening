#!/usr/bin/env python3
"""Unattended scheduler (Phase 2): due-logic + run ledger + auto-restart /
auto-backup tick loop. Mirrors admin_welcome (sqlite ledger + scan-loop). Config
in server/state/admin/schedule.json (OFF default, persists reinstall); ledger in server/state/scheduler.db.

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
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

# Sibling import (resolves whether run as a script, via importlib in tests, or -m),
# mirroring admin-http.py. Gives the scale-instance task the shared mock-k8s
# primitives without duplicating the CA-pinned transport.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from admin_instances import (  # noqa: E402
    SCALABLE_MAPS,
    SCALE_REPLICAS_MAX,
    fetch_mock_status,
    mock_k8s_request,
    resolve_scale_key,
)

DEFAULT_TICK_SECS = 30
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PENDING_KEY = "pending_restart_at"

# Generic scheduled-task framework (Phase 1). Tasks live in an array alongside
# the special-cased restart/backup, each: {id, type, enabled, schedule, params}.
# Their runs are recorded in the same ledger under task:<id>, so the existing
# run-history surface shows them automatically.
TASK_TYPES = ("broadcast", "scale-instance")
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DAILY_CATCHUP_GRACE_SECS = 3600  # don't fire a stale daily slot after this window

DEFAULT_CONFIG = {
    "restart": {"enabled": False, "time": "08:00", "days": list(DAYS),
                "warn_lead_secs": 300, "warn_freq_secs": 60, "catch_up_grace_secs": 3600},
    "backup": {"enabled": False, "every_hours": 24, "retention": 7},
    "tasks": [],
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
    # Persistent (server/state/), not data/ (wiped on reinstall); seeded from the
    # data/admin/ shipped default by prestart.sh. See admin_autoscaler.config_path.
    return os.path.join(base, "server", "state", "admin", "schedule.json")


def ledger_path(base):
    return os.path.join(base, "server", "state", "scheduler.db")


def load_config(base):
    """Read schedule.json, shallow-merged over defaults so missing keys never crash."""
    out = {"restart": dict(DEFAULT_CONFIG["restart"]),
           "backup": dict(DEFAULT_CONFIG["backup"]),
           "tasks": []}
    try:
        with open(config_path(base), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return out
    if isinstance(c, dict):
        for k in ("restart", "backup"):
            if isinstance(c.get(k), dict):
                out[k].update(c[k])
        if isinstance(c.get("tasks"), list):
            out["tasks"] = c["tasks"]
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
    raw_tasks = raw.get("tasks", [])
    if not isinstance(raw_tasks, list):
        return None, "tasks must be a list"
    tasks, seen = [], set()
    for i, t in enumerate(raw_tasks):
        vt, err = validate_task(t)
        if err or vt is None:
            return None, f"tasks[{i}]: {err}"
        if vt["id"] in seen:
            return None, f"duplicate task id: {vt['id']}"
        seen.add(vt["id"])
        tasks.append(vt)
    return {**out, "tasks": tasks}, None


def _validate_schedule(raw):
    """Validate + coerce a task schedule. Returns (schedule, None) or (None, err)."""
    if not isinstance(raw, dict):
        return None, "schedule must be an object"
    kind = raw.get("kind")
    if kind == "daily":
        t = str(raw.get("time", "08:00"))
        if _slot_today(_now_dt(), t) is None:
            return None, "schedule.time must be HH:MM"
        days = raw.get("days", list(DAYS))
        if not isinstance(days, list) or not days or not all(d in DAYS for d in days):
            return None, "schedule.days must be a non-empty subset of mon..sun"
        return {"kind": "daily", "time": t, "days": [d for d in DAYS if d in days]}, None
    if kind == "interval":
        try:
            every = int(raw.get("every_minutes", 60))
        except (TypeError, ValueError):
            return None, "schedule.every_minutes must be an integer"
        if every < 1:
            return None, "schedule.every_minutes must be >= 1"
        return {"kind": "interval", "every_minutes": every}, None
    return None, "schedule.kind must be 'daily' or 'interval'"


def _coerce_bool(v):
    """Coerce a config value to bool: real bools pass through; strings like
    'true'/'1'/'yes'/'on' are truthy; everything else falls back to bool()."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _validate_task_params(ttype, raw):
    """Validate + coerce per-type task params. Returns (params, None) or (None, err)."""
    raw = raw or {}
    if not isinstance(raw, dict):
        return None, "params must be an object"
    if ttype == "broadcast":
        title = str(raw.get("title", "")).strip()
        body = str(raw.get("body", "")).strip()
        if not title:
            return None, "broadcast params.title required"
        if not body:
            return None, "broadcast params.body required"
        try:
            dur = max(int(raw.get("duration", 30)), 1)
        except (TypeError, ValueError):
            return None, "broadcast params.duration must be an integer"
        return {"title": title[:200], "body": body[:500], "duration": dur}, None
    if ttype == "scale-instance":
        mp = raw.get("map")
        if mp not in SCALABLE_MAPS:
            return None, f"scale-instance params.map must be one of: {', '.join(SCALABLE_MAPS)}"
        replicas = raw.get("replicas")
        if isinstance(replicas, bool) or not isinstance(replicas, (int, str)):
            return None, "scale-instance params.replicas must be an integer"
        try:
            replicas = int(replicas)
        except (TypeError, ValueError):
            return None, "scale-instance params.replicas must be an integer"
        if replicas < 0 or replicas > SCALE_REPLICAS_MAX:
            return None, f"scale-instance params.replicas must be 0-{SCALE_REPLICAS_MAX}"
        return {"map": mp, "replicas": replicas, "force": _coerce_bool(raw.get("force", False))}, None
    return {}, None


def validate_task(raw):
    """Validate + coerce one generic task. Returns (task, None) or (None, err). Pure."""
    if not isinstance(raw, dict):
        return None, "task must be an object"
    tid = str(raw.get("id", "")).strip()
    if not _TASK_ID_RE.match(tid):
        return None, "id must match [a-z0-9][a-z0-9_-]{0,63}"
    ttype = raw.get("type")
    if ttype not in TASK_TYPES:
        return None, f"type must be one of: {', '.join(TASK_TYPES)}"
    sched, err = _validate_schedule(raw.get("schedule"))
    if err:
        return None, err
    params, err = _validate_task_params(ttype, raw.get("params"))
    if err:
        return None, err
    return {"id": tid, "type": ttype, "enabled": bool(raw.get("enabled", False)),
            "schedule": sched, "params": params}, None


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
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, AttributeError):
        return None  # bad format OR out-of-range hour/minute (replace raises ValueError)


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


def task_due(task, now, last_run):
    """True if a generic task should fire now. Pure. Mirrors restart/backup due-logic:
    'daily' fires once per slot within a catch-up grace; 'interval' fires every N
    minutes (and immediately when never run, like backup)."""
    if not task.get("enabled"):
        return False
    sched = task.get("schedule") or {}
    kind = sched.get("kind")
    if kind == "interval":
        try:
            every = max(int(sched.get("every_minutes", 60)), 1)
        except (TypeError, ValueError):
            return False
        lr = _parse_iso(last_run)
        return lr is None or now >= lr + timedelta(minutes=every)
    if kind == "daily":
        if DAYS[now.weekday()] not in (sched.get("days") or DAYS):
            return False
        slot = _slot_today(now, sched.get("time", "08:00"))
        if slot is None or now < slot:
            return False
        if now >= slot + timedelta(seconds=DAILY_CATCHUP_GRACE_SECS):
            return False  # missed the window — don't fire a stale catch-up
        lr = _parse_iso(last_run)
        return lr is None or lr < slot  # not already fired this slot
    return False


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

    def last_settled(self, task):
        """Most recent run that SETTLED the schedule slot (status ok or skipped) —
        excludes 'error' so a transient failure self-heals on the next tick within
        the catch-up grace instead of consuming the slot. Used for generic tasks[]
        dedupe; restart/backup keep their own last()-based logic."""
        cur = self.conn.execute(
            "SELECT at FROM scheduler_runs WHERE task=? AND status IN ('ok','skipped') "
            "ORDER BY id DESC LIMIT 1", (task,))
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


def run_broadcast(base, params):
    """admin-publish broadcast <title> <body> <duration>. Returns (ok, detail)."""
    params = params or {}
    title = str(params.get("title", "")).strip()
    body = str(params.get("body", "")).strip()
    if not title or not body:
        return False, "broadcast: title and body required"
    try:
        dur = max(int(params.get("duration", 30)), 1)
    except (TypeError, ValueError):
        dur = 30
    try:
        r = subprocess.run(["bash", _publish(base), "broadcast", title, body, str(dur)],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"broadcast error: {e}"[:300]
    return r.returncode == 0, (r.stdout.strip() or r.stderr.strip())[:300]


def online_count_for_map(base, mp):
    """LIVE connected-player count on one map via admin-publish farm-player-count
    (sum of farm_state.connected_players). Returns the count, or None if it could NOT
    be confirmed (subprocess / non-zero exit / parse failure) — distinct from a genuine
    0 so the scale-down guard can fail safe rather than evicting players on an unreadable
    status. Uses farm-player-count, NOT server-status: server-status keys on the
    character's actor map ("HarkoVillage"), missing hub VISITORS for the SSS map name
    (SH_HarkoVillage) and so reading a populated hub as empty. Isolated for monkeypatch."""
    try:
        r = subprocess.run(["bash", _publish(base), "farm-player-count"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        import admin_status  # noqa: PLC0415 - lazy: only the scale guard needs it
        return int(admin_status.parse_player_counts(r.stdout).get(mp, 0) or 0)
    except Exception:  # noqa: BLE001 - couldn't parse -> unconfirmed, not zero
        return None


def run_scale_instance(base, task):
    """Scale a map to params.replicas via mock-k8s ServerSetScale. Returns
    (status, detail) with status in (ok|skipped|error). Idempotent no-op when
    already at target; a scale-down with players online is skipped unless force.
    Mechanism (fetch/resolve/PATCH) is shared via admin_instances; the guard +
    idempotency policy live here (the interactive endpoint owns its own)."""
    p = task.get("params") or {}
    mp, replicas, force = p.get("map"), p.get("replicas"), bool(p.get("force", False))
    if mp not in SCALABLE_MAPS or not isinstance(replicas, int) or isinstance(replicas, bool):
        return "error", f"invalid scale params: map={mp!r} replicas={replicas!r}"
    mock = fetch_mock_status()
    if not mock:
        return "error", "mock-k8s /status unreachable"
    key = resolve_scale_key(mock, mp)
    if key is None:
        return "error", f"{mp} not tracked by mock-k8s"
    ns, name, current = key
    if replicas == current:
        return "ok", f"already at {replicas}"
    if replicas < current and not force:
        online = online_count_for_map(base, mp)
        if online is None:
            return "skipped", f"could not confirm players on {mp}; not scaled (force=false)"
        if online > 0:
            return "skipped", f"{online} player(s) online on {mp}; not scaled (force=false)"
    code, _ = mock_k8s_request(
        "PATCH", f"/apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}",
        {"spec": {"replicas": replicas}})
    if code is None or code >= 300:
        return "error", f"mock-k8s scale failed (http {code})"
    return "ok", f"scaled {mp} {current}->{replicas}"


def run_task(base, task):
    """Dispatch one generic scheduled task by type. Returns (status, detail) with
    status in (ok|error|skipped). I/O; isolated for monkeypatch."""
    ttype = task.get("type")
    if ttype == "broadcast":
        ok, detail = run_broadcast(base, task.get("params") or {})
        return ("ok" if ok else "error"), detail
    if ttype == "scale-instance":
        return run_scale_instance(base, task)
    return "error", f"unknown task type: {ttype}"


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


def _clean_setting(raw):
    """Panel variables arrive as typed/pasted. Strip surrounding whitespace
    and quotes: a value wrapped in quotes reaches the API as part of the
    credential and comes back 401, which reads as "my key is wrong"."""
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


# Header values cannot contain control characters. A key pasted with a
# trailing newline used to raise ValueError out of conn.request() — which
# nothing caught, and whose message quotes the whole Authorization header,
# so the API key landed in logs/scheduler.log in cleartext (readable from
# the panel file manager and rendered by the log viewer).
_HEADER_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def _restart_hint(code):
    """What an operator should actually check, per status. Measured against
    a live Pelican panel rather than guessed — the codes are not
    interchangeable and each points somewhere different."""
    if code == 401:
        return ("the panel rejected the credential: DUNE_PELICAN_CLIENT_KEY is not a valid "
                "client key. Check it was not truncated or revoked, and that you pasted the "
                "key itself rather than its description")
    if code == 403:
        return ("authenticated, but that key may not act on this server — an Application "
                "(admin) API key cannot drive the client API. Create an Account API key "
                "from the panel user that owns the server")
    if code == 404:
        return ("the credential is fine but the panel has no such server: check "
                "DUNE_PELICAN_SERVER_ID (the short id from the server's URL)")
    if code == 409:
        return "the panel refused the power action — the server is probably already restarting"
    if code == 422:
        return "the panel rejected the request body — report this, the egg built it wrong"
    return ""


def pelican_restart():
    """POST the Pelican client-API power:restart. Needs DUNE_PELICAN_URL +
    DUNE_PELICAN_CLIENT_KEY + DUNE_PELICAN_SERVER_ID env. Optional
    DUNE_PELICAN_RESOLVE pins the TCP target to a given IP (curl --resolve
    style) so an in-container restart can bypass a CDN in front of the panel."""
    url = _clean_setting(os.environ.get("DUNE_PELICAN_URL")).rstrip("/")
    key = _clean_setting(os.environ.get("DUNE_PELICAN_CLIENT_KEY"))
    sid = _clean_setting(os.environ.get("DUNE_PELICAN_SERVER_ID"))
    if not (url and key and sid):
        return False, "restart skipped: DUNE_PELICAN_{URL,CLIENT_KEY,SERVER_ID} not set"
    if _HEADER_UNSAFE.search(key):
        # Deliberately does not echo the value: this message is printed
        # into a log the operator's whole panel can read.
        return False, ("restart failed: DUNE_PELICAN_CLIENT_KEY contains a control character "
                       "(a stray newline from copy-paste?) — re-paste it as a single line")
    resolve_ip = _clean_setting(os.environ.get("DUNE_PELICAN_RESOLVE")) or None
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
        hint = _restart_hint(code)
        detail = f"power restart HTTP {code}{via}"
        return 200 <= code < 300, detail + (f" — {hint}" if hint else "")
    except ValueError as e:
        # Anything that made the request unbuildable. str(e) can quote the
        # header, so it is never included.
        return False, f"power restart failed to build the request ({type(e).__name__})"
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        return False, _redact(f"power restart error: {e}", key)[:200]
    finally:
        if conn is not None:
            conn.close()


def _redact(message, secret):
    """Last line of defence: no path may put the API key into a log."""
    return message.replace(secret, "<redacted>") if secret else message


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

    # 4) generic scheduled tasks (broadcast, scale-instance, ...). One bad task
    # never kills the rest. Dedupe keys off last_settled (ok/skipped) so a
    # transient error self-heals on the next tick instead of poisoning the slot.
    for task in (cfg.get("tasks") or []):
        key = f"task:{task.get('id', '?')}"
        # Dedupe source is kind-aware: a daily slot keys off last_settled (ok/skipped)
        # so a transient error retries within the catch-up grace; an interval keys off
        # last() (any terminal run, incl. error) so it respects every_minutes and does
        # not storm every tick while erroring.
        kind = (task.get("schedule") or {}).get("kind")
        last = ledger.last(key) if kind == "interval" else ledger.last_settled(key)
        try:
            if task_due(task, now, last):
                status, detail = run_task(base, task)
                ledger.record(key, status, detail)
                actions.append((key, status == "ok", detail))
        except Exception as e:  # noqa: BLE001 - isolate one task's failure from the loop
            ledger.record(key, "error", f"task error: {e}"[:200])
            actions.append((key, False, f"task error: {e}"[:200]))

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
    if cmd == "run-task":
        tid = argv[3] if len(argv) > 3 else ""
        led = SchedulerLedger(ledger_path(base))
        task = next((t for t in load_config(base).get("tasks", []) if t.get("id") == tid), None)
        if task is None:
            led.close()
            print(json.dumps({"ok": False, "error": f"no task with id {tid!r}"}))
            return 1
        status, detail = run_task(base, task)
        led.record(f"task:{tid}", status, detail)
        led.close()
        print(json.dumps({"ok": status == "ok", "status": status, "detail": detail}))
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
    print("usage: admin_schedule.py <scan-loop|status|runs|run-backup|run-restart|run-task <id>|set-config <json>> [BASE]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
