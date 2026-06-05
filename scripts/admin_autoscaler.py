#!/usr/bin/env python3
"""Demand-based autoscaler — PURE policy engine (Phase 1).

Decides, per managed map, how many mock-k8s ServerSetScale replicas should run
given the live online-player count and the Director's inbound travel-demand. One
daemon, two behaviours selected by the per-map floor:

  * min_replicas >= 1  -> the map stays warm and load-scales up under population
                          (ceil(online / players_per_instance), capped at max).
  * min_replicas == 0  -> the map drains fully cold when idle and wakes to 1 on
                          inbound travel-demand parsed from director.log.

Both share the anti-flap hysteresis (idle-drain timer + post-demand grace) and
the hard guards: never evict a non-empty map, never act blind (online unreadable
-> hold, never scale down), never drop below the per-map floor. UE5 cold boot is
~40s, so a warm floor is how you buy instant joins; a cold map pays that 40s once
on wake as the price of freeing RAM.

This module is mechanism-free on purpose: every function here is pure and unit
-tested in test_admin_autoscaler.py. The I/O shell (mock-k8s PATCH, server-status
read, director.log tail, sqlite timer state, scan-loop) is Phase 2 and reuses the
shared primitives in admin_instances / admin_schedule rather than duplicating them.
"""
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import NamedTuple

# Sibling import (resolves whether run as a script, via importlib in tests, or -m),
# mirroring admin_schedule.py — gives us the SCALABLE_MAPS allowlist, the hard replica
# ceiling, and the shared mock-k8s transport (fetch/resolve/PATCH) without redefining them.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from admin_instances import (  # noqa: E402
    SCALABLE_MAPS,
    SCALE_REPLICAS_MAX,
    fetch_mock_status,
    mock_k8s_request,
    resolve_scale_key,
)

# Loop cadence floor: server-status shells psql, so a sub-30s loop is wasteful.
MIN_SCAN_INTERVAL_SECS = 30

DEFAULT_CONFIG = {
    "enabled": False,
    "scan_interval_secs": 60,
    "idle_drain_secs": 300,    # an empty map must stay empty this long before draining
    "demand_grace_secs": 120,  # after any inbound travel-demand, immune to drain this long
    "players_per_instance": 30,
}
# Per-map default: warm floor (never cold), one extra shard of headroom under load.
DEFAULT_MAP = {"enabled": True, "min_replicas": 1, "max_replicas": 2}


def iso(dt):
    """Serialize an aware UTC datetime to 'YYYY-MM-DDTHH:MM:SSZ', or None -> None.
    Exported (with parse_iso) so Phase 2 persists the decide_map timers through sqlite
    without reinventing the format or risking an aware/naive datetime slip that would
    silently stop a map ever draining."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None


def parse_iso(s):
    """Inverse of iso(): parse back to an aware UTC datetime, or None on anything bad."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _coerce_bool(v):
    """Real bools pass through; 'true'/'1'/'yes'/'on' (any case) are truthy."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _int_field(raw, key, default, *, minimum):
    """Coerce raw[key] to an int >= minimum (clamped, not rejected). Returns
    (value, None) or (None, error) when the value isn't an integer at all."""
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None, f"{key} must be an integer"
    try:
        return max(int(v), minimum), None
    except (TypeError, ValueError):
        return None, f"{key} must be an integer"


def _validate_map(raw):
    """Validate + coerce one managed-map entry. Returns (entry, None) or (None, err)."""
    if not isinstance(raw, dict):
        return None, "map entry must be an object"
    mp = raw.get("map")
    if mp not in SCALABLE_MAPS:
        return None, f"map must be one of: {', '.join(SCALABLE_MAPS)} (got {mp!r})"
    mn_raw = raw.get("min_replicas", DEFAULT_MAP["min_replicas"])
    mx_raw = raw.get("max_replicas", DEFAULT_MAP["max_replicas"])
    # Reject bool/float exactly like the top-level _int_field — no silent truncation.
    if any(isinstance(v, bool) or not isinstance(v, (int, str)) for v in (mn_raw, mx_raw)):
        return None, f"{mp}: min_replicas/max_replicas must be integers"
    try:
        min_r, max_r = int(mn_raw), int(mx_raw)
    except (TypeError, ValueError):
        return None, f"{mp}: min_replicas/max_replicas must be integers"
    if max_r < 1 or max_r > SCALE_REPLICAS_MAX:
        return None, f"{mp}: max_replicas must be 1-{SCALE_REPLICAS_MAX}"
    if min_r < 0 or min_r > max_r:
        return None, f"{mp}: min_replicas must be 0-{max_r}"
    return {"map": mp, "enabled": _coerce_bool(raw.get("enabled", True)),
            "min_replicas": min_r, "max_replicas": max_r}, None


def _default_maps():
    return [{"map": mp, **DEFAULT_MAP} for mp in SCALABLE_MAPS]


def validate_config(raw):
    """Validate + coerce a posted autoscaler config to the canonical shape.
    Returns (config, None) or (None, error). Pure (no I/O)."""
    if not isinstance(raw, dict):
        return None, "config must be an object"
    out: dict = {"enabled": _coerce_bool(raw.get("enabled", False))}
    out["scan_interval_secs"], err = _int_field(
        raw, "scan_interval_secs", DEFAULT_CONFIG["scan_interval_secs"], minimum=MIN_SCAN_INTERVAL_SECS)
    if err:
        return None, err
    for key, minimum in (("idle_drain_secs", 0), ("demand_grace_secs", 0), ("players_per_instance", 1)):
        out[key], err = _int_field(raw, key, DEFAULT_CONFIG[key], minimum=minimum)
        if err:
            return None, err
    raw_maps = raw.get("maps", None)
    if raw_maps is None:
        out["maps"] = _default_maps()
        return out, None
    if not isinstance(raw_maps, list):
        return None, "maps must be a list"
    by_map, seen = {}, set()
    for i, m in enumerate(raw_maps):
        vm, err = _validate_map(m)
        if err or vm is None:
            return None, f"maps[{i}]: {err}"
        if vm["map"] in seen:
            return None, f"duplicate map: {vm['map']}"
        seen.add(vm["map"])
        by_map[vm["map"]] = vm
    # canonical SCALABLE_MAPS order, dropping nothing the operator supplied
    out["maps"] = [by_map[mp] for mp in SCALABLE_MAPS if mp in by_map]
    return out, None


# -- travel-demand parsing -----------------------------------------------------
# The Director logs inbound travel as these shapes (seen live), each carrying a
# player count we read as "how many are trying to reach this map now":
#   Processing travel queue for <MAP> (... num=N)
#   Processing travel queue for ClassicalInstancing group <GROUP> (servers: [], num: N)
#   Processing travel queue for <MAP> (servers: [4 (<server-id>)], num: 0)   <- WARM, nested paren!
#   Received travel request for N player(s) to <MAP> (instancingMode=Dimension)
#
# We split each physical line into per-record segments at the record headers, then read
# num from WITHIN the segment. This is robust to two real hazards: (a) the gateway dedup
# concatenates several records onto one line (segment boundary stops a record borrowing
# the next one's num), and (b) a warm map's record nests a paren around the server id
# ("[4 (id)]") — a naive single-paren capture stops at the inner ")" and misses num,
# which then defaulted to a phantom 1 and pinned cold maps warm. Map/partition names are
# [A-Za-z0-9_] (captured exactly, not \S+, so a trailing comma / no-space "(" can't taint
# the name and lose the wake). num-less records are NO demand (0): every real travel
# record carries num, so a missing one is malformed, not a hidden wake.
_RECORD_START_RE = re.compile(r"travel (?:queue|request) for ", re.IGNORECASE)
_QUEUE_NAME_RE = re.compile(r"^travel queue for (?:ClassicalInstancing group )?([A-Za-z0-9_]+)", re.IGNORECASE)
_REQUEST_RE = re.compile(r"^travel request for (\d+) player\(s\) to ([A-Za-z0-9_]+)", re.IGNORECASE)
_NUM_RE = re.compile(r"num[:=]\s*(\d+)", re.IGNORECASE)


def parse_travel_demand(lines):
    """Pure. Parse a window of director.log lines into {map_or_group: peak_num}.
    Peak (max) over the window, not sum, so a tailed window can't double-count a
    repeated line. The caller maps names to managed maps via demand_for_map; non-map
    groups are harmless.

    PHASE 2 CONTRACT: feed only NEW lines (track the director.log read offset). Re-reading
    a fixed window every tick would read one event as demand on every tick and pin a
    cold (min_replicas==0) map warm forever via the demand grace."""
    out: "dict[str, int]" = {}
    for line in lines:
        starts = [m.start() for m in _RECORD_START_RE.finditer(line)]
        for i, s in enumerate(starts):
            seg = line[s:(starts[i + 1] if i + 1 < len(starts) else len(line))]
            req = _REQUEST_RE.match(seg)
            if req:
                name, num = req.group(2), int(req.group(1))
            else:
                q = _QUEUE_NAME_RE.match(seg)
                if not q:
                    continue
                name = q.group(1)
                nm = _NUM_RE.search(seg)  # whole segment -> finds num past a nested (server-id) paren
                num = int(nm.group(1)) if nm else 0  # num-less == no demand
            out[name] = max(out.get(name, 0), num)
    return out


def demand_for_map(parsed, mp):
    """Inbound travel demand for one managed map from parse_travel_demand output.
    Managed maps surface under their own name (dimension / request forms); the
    ClassicalInstancing GROUP form keys on sub-instance names that never equal a
    managed map, so a group never (and should never) drives a managed map's scale."""
    try:
        return max(int(parsed.get(mp, 0)), 0)
    except (TypeError, ValueError):
        return 0


# -- the decision --------------------------------------------------------------
class MapDecision(NamedTuple):
    action: str                 # "up" | "down" | "hold"
    target: int                 # replicas to set (== current for "hold")
    reason: str
    idle_since: "object | None"  # carried-forward drain-countdown start (datetime|None)
    last_demand: "object | None"  # carried-forward last inbound-demand instant


def _needed_for_load(online, players_per_instance):
    if online is None or online <= 0:
        return 0
    return math.ceil(online / max(int(players_per_instance), 1))


def decide_map(map_cfg, current, online, demand, *, now,
               idle_since=None, last_demand=None,
               players_per_instance=30, idle_drain_secs=300, demand_grace_secs=120):
    """Pure per-map scaling decision.

    Inputs: validated map_cfg ({min_replicas,max_replicas}); `current` desired
    replicas; `online` players on the map (int, or None = unreadable); `demand`
    inbound travel count for this tick; carried timer state (idle_since,
    last_demand); `now` (aware datetime). Returns a MapDecision with the action,
    target replicas, a reason, and the timers to persist for next tick.

    Up is immediate (responsiveness on wake); down is conservative: only when the
    map is provably empty AND past both the post-demand grace and the idle-drain
    timer (run concurrently), and never below the floor or onto a non-empty /
    unreadable map. A map the operator disabled is left entirely alone."""
    # Disabled maps stay under manual control — no up, no down.
    if not _coerce_bool(map_cfg.get("enabled", True)):
        return MapDecision("hold", current, "autoscaler disabled for this map", idle_since, last_demand)

    max_r = min(int(map_cfg.get("max_replicas", DEFAULT_MAP["max_replicas"])), SCALE_REPLICAS_MAX)
    min_r = max(0, min(int(map_cfg.get("min_replicas", DEFAULT_MAP["min_replicas"])), max_r))

    # Only a non-negative, non-bool int is a trustworthy count; anything else (None,
    # bool, float, negative) is "unreadable" and must never license a scale-down.
    if not (isinstance(online, int) and not isinstance(online, bool) and online >= 0):
        online = None

    has_demand = bool(demand) and demand > 0
    new_last_demand = now if has_demand else last_demand

    # 1) upward target = the most any signal asks for, clamped to the band. Immediate.
    want = min(max(min_r, _needed_for_load(online, players_per_instance), 1 if has_demand else 0), max_r)
    if want > current:
        return MapDecision("up", want,
                           f"scale up to {want} (online={online}, demand={demand}, floor={min_r})",
                           None, new_last_demand)

    # 2) downward: only ever toward the floor, and only when safe.
    if current <= min_r:
        return MapDecision("hold", current, f"at floor {min_r}", None, new_last_demand)
    if online is None:
        return MapDecision("hold", current, "population unreadable; holding (no blind scale-down)",
                           None, new_last_demand)
    if online > 0:
        return MapDecision("hold", current, f"{online} online; never evict a non-empty map",
                           None, new_last_demand)
    # online == 0 (confirmed empty + readable). The idle-drain and post-demand grace run
    # CONCURRENTLY: the idle timer accrues regardless of demand (so it isn't thrown away
    # by a blip), and a drain needs the map empty for idle_drain_secs AND no inbound demand
    # within demand_grace_secs.
    started = idle_since or now
    in_grace = (new_last_demand is not None
                and (now - new_last_demand).total_seconds() < demand_grace_secs)
    elapsed = (now - started).total_seconds()
    if elapsed >= idle_drain_secs and not in_grace:
        return MapDecision("down", min_r,
                           f"idle {int(elapsed)}s >= {idle_drain_secs}s; drain {current}->{min_r}",
                           None, new_last_demand)
    why = "post-demand grace" if in_grace else f"draining ({int(elapsed)}/{idle_drain_secs}s)"
    return MapDecision("hold", current, why, started, new_last_demand)


# ==========================================================================
# I/O layer (Phase 2): config, the new-lines-only director.log tailer, the timer
# ledger, the tick orchestration, and the CLI. The pure engine above is the only
# part with logic worth unit-testing in isolation; the wiring here is thin and is
# tested with fakes (monkeypatched mock-k8s + counts + tailer) over an in-memory
# ledger. DB access stays in admin-publish.sh; mock-k8s uses the shared transport.
# ==========================================================================
def _now():
    return datetime.now(timezone.utc)


def config_path(base):
    # Operator config lives under server/state/ (persistent — the install never wipes
    # it), NOT data/ (refreshed from the repo on every reinstall). prestart.sh seeds it
    # from the data/admin/ shipped default on first boot, so config survives reinstalls.
    return os.path.join(base, "server", "state", "admin", "autoscaler.json")


def ledger_path(base):
    return os.path.join(base, "server", "state", "autoscaler.db")


def director_log_path(base):
    return os.path.join(base, "logs", "director.log")


def _publish(base):
    return os.path.join(base, "scripts", "admin-publish.sh")


def load_config(base) -> dict:
    """Read autoscaler.json and return a fully-validated config, falling back to
    validated defaults on a missing/malformed/invalid file so the loop always has a
    clean config to act on (set-config is the only writer and validates on write)."""
    try:
        with open(config_path(base), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    cfg, err = validate_config(raw)
    if err or not isinstance(cfg, dict):
        cfg, _ = validate_config({})
    return cfg if isinstance(cfg, dict) else {}


def write_config(base, cfg):
    """Atomically persist autoscaler.json (tmp + os.replace) so a crash mid-write
    can't leave the loop reading a truncated config."""
    p = config_path(base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def read_new_log_lines(path, offset):
    """Return (complete new lines since `offset`, new_offset). The wake signal MUST
    be edge-triggered (Phase-1 contract): we consume only bytes appended since the
    last read so a single travel event reads as demand on exactly one tick — never a
    re-read window that would pin a cold map warm. Semantics:
      * offset is None (first run): skip the whole backlog, return ([], end) so we
        only ever react to demand that arrives AFTER the daemon starts;
      * file smaller than offset (rotated/truncated): reset to 0 and read from start;
      * a trailing partial line (no newline yet) is NOT consumed — offset stops at the
        last newline so the half-written line is read whole on a later tick;
      * missing/unreadable file: ([], offset) unchanged (first-runs when it appears)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if offset is None:
        return [], size
    if size < offset:
        offset = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    nl = data.rfind(b"\n")
    if nl == -1:
        return [], offset  # no complete line yet — don't advance past a partial
    consumed = data[:nl + 1]
    return consumed.decode("utf-8", "replace").splitlines(), offset + len(consumed)


def read_online_counts(base):
    """One per-map LIVE connected-player snapshot via admin-publish farm-player-count
    (sum of farm_state.connected_players per map). Returns {map: int}, or None if it
    could NOT be confirmed (subprocess / non-zero exit / parse failure) — distinct
    from a genuine empty so every scale-down fails safe rather than evicting on an
    unreadable status. One subprocess per tick.

    Uses farm_state (the instance socket count), NOT server-status: server-status
    groups dune.actors by the character's PERSISTENT home map, so a player VISITING a
    hub (Harko/Arrakeen) is counted under their home map, not the hub — which read the
    hub as empty and got a visitor drained+kicked. connected_players is who's actually
    on the instance, so the drain guard never evicts a hub visitor."""
    try:
        r = subprocess.run(["bash", _publish(base), "farm-player-count"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        import admin_status  # noqa: PLC0415 - lazy: only the tick needs it
        return {k: int(v) for k, v in admin_status.parse_player_counts(r.stdout).items()}
    except Exception:  # noqa: BLE001 - couldn't parse -> unconfirmed, not empty
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS autoscaler_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS autoscaler_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class AutoscalerLedger:
    """sqlite-backed per-map timer state (idle_since / last_demand), the director.log
    read offset, and an action history. Mirrors admin_schedule.SchedulerLedger."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def record(self, mp, action, detail=""):
        self.conn.execute(
            "INSERT INTO autoscaler_runs (map,action,detail,at) VALUES (?,?,?,?)",
            (mp, action, (detail or "")[:500], iso(_now())))
        self.conn.commit()

    def list_runs(self, limit=50):
        limit = 50 if (limit <= 0 or limit > 500) else limit
        cur = self.conn.execute(
            "SELECT map,action,detail,at FROM autoscaler_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(zip(["map", "action", "detail", "at"], row)) for row in cur.fetchall()]

    def get_state(self, k):
        cur = self.conn.execute("SELECT v FROM autoscaler_state WHERE k=?", (k,))
        r = cur.fetchone()
        return r[0] if r else None

    def set_state(self, k, v):
        self.conn.execute(
            "INSERT INTO autoscaler_state (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.conn.commit()

    def clear_state(self, k):
        self.conn.execute("DELETE FROM autoscaler_state WHERE k=?", (k,))
        self.conn.commit()

    def get_offset(self):
        v = self.get_state("log_offset")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def set_offset(self, n):
        self.set_state("log_offset", str(int(n)))


def _persist_timer(ledger, key, dt):
    """Store an aware datetime as ISO, or clear the key when None (so a reset timer
    leaves no stale value behind)."""
    if dt is None:
        ledger.clear_state(key)
    else:
        ledger.set_state(key, iso(dt))


def run_tick(base, cfg, ledger, now=None):
    """One autoscaler tick. Reads new travel-demand (edge-triggered), one online-count
    snapshot, and the current replicas per managed map; runs decide_map; persists the
    timers; and actuates up/down via a single ServerSetScale.spec.replicas PATCH.
    Returns the list of (map, action, ok, detail) actuations taken (holds are not
    recorded, to keep the history readable). Never raises into the scan-loop on a
    single map's failure — mock-k8s down / a map not yet tracked simply skips it."""
    now = now or _now()
    actions = []

    # 1) New director.log lines only (the cold-wake signal). Offset advances even when
    # nothing matches, so we never re-scan the same bytes.
    lines, new_offset = read_new_log_lines(director_log_path(base), ledger.get_offset())
    ledger.set_offset(new_offset)
    demand_by = parse_travel_demand(lines)

    # 2) One consistent snapshot of desired replicas + online players for this tick.
    mock = fetch_mock_status()
    counts = read_online_counts(base)
    ppi = int(cfg.get("players_per_instance", DEFAULT_CONFIG["players_per_instance"]))
    idle_secs = int(cfg.get("idle_drain_secs", DEFAULT_CONFIG["idle_drain_secs"]))
    grace_secs = int(cfg.get("demand_grace_secs", DEFAULT_CONFIG["demand_grace_secs"]))

    for mc in cfg.get("maps", []):
        mp = mc.get("map")
        key = resolve_scale_key(mock, mp) if mock else None
        if key is None:
            continue  # mock-k8s down or map not tracked this tick — leave it alone
        ns, name, current = key
        online = None if counts is None else int(counts.get(mp, 0))
        demand = demand_for_map(demand_by, mp)
        idle_since = parse_iso(ledger.get_state(f"idle:{mp}"))
        last_demand = parse_iso(ledger.get_state(f"demand:{mp}"))
        d = decide_map(mc, current, online, demand, now=now,
                       idle_since=idle_since, last_demand=last_demand,
                       players_per_instance=ppi, idle_drain_secs=idle_secs,
                       demand_grace_secs=grace_secs)
        _persist_timer(ledger, f"idle:{mp}", d.idle_since)
        _persist_timer(ledger, f"demand:{mp}", d.last_demand)
        if d.action in ("up", "down"):
            code, _ = mock_k8s_request(
                "PATCH", f"/apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}",
                {"spec": {"replicas": d.target}})
            ok = code is not None and code < 300
            ledger.record(mp, d.action if ok else "error",
                          d.reason if ok else f"PATCH http {code}: {d.reason}")
            actions.append((mp, d.action, ok, d.reason))
    return actions


def _status(base):
    led = AutoscalerLedger(ledger_path(base))
    cfg = load_config(base)
    state = {mc["map"]: {"idle_since": led.get_state(f"idle:{mc['map']}"),
                         "last_demand": led.get_state(f"demand:{mc['map']}")}
             for mc in cfg.get("maps", [])}
    out = {"ok": True, "config": {k: v for k, v in cfg.items() if k != "_comment"},
           "runs": led.list_runs(20), "state": state, "log_offset": led.get_offset()}
    led.close()
    return out


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    base = argv[2] if len(argv) > 2 else os.environ.get("DUNE_BASE_DIR", "/home/container")
    if cmd == "status":
        print(json.dumps(_status(base)))
        return 0
    if cmd == "tick":
        # One tick on demand (ignores enabled — the operator/route asked for it).
        led = AutoscalerLedger(ledger_path(base))
        acts = run_tick(base, load_config(base), led)
        led.close()
        print(json.dumps({"ok": True, "actions": [
            {"map": m, "action": a, "ok": ok, "detail": d} for (m, a, ok, d) in acts]}))
        return 0
    if cmd == "set-config":
        raw = argv[3] if len(argv) > 3 else ""
        try:
            parsed = json.loads(raw)
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid JSON"}))
            return 1
        cfg, err = validate_config(parsed)
        if err or cfg is None:
            print(json.dumps({"ok": False, "error": err or "invalid config"}))
            return 1
        write_config(base, cfg)
        print(json.dumps({"ok": True, "config": cfg}))
        return 0
    if cmd == "scan-loop":
        # Entrypoint launches this. Loops at scan_interval_secs, NO-OPS while
        # autoscaler.json enabled:false, and survives a malformed config.
        led = AutoscalerLedger(ledger_path(base))
        while True:
            try:
                cfg = load_config(base)
                interval = max(int(cfg.get("scan_interval_secs", DEFAULT_CONFIG["scan_interval_secs"])),
                               MIN_SCAN_INTERVAL_SECS)
                if cfg.get("enabled"):
                    for (m, a, ok, detail) in run_tick(base, cfg, led):
                        print(f"[autoscaler] {m} {a} ok={ok} {detail}", flush=True)
            except Exception as e:  # never let the loop die
                interval = 60
                print(f"[autoscaler] tick error: {e}", flush=True)
            time.sleep(interval)
    print("usage: admin_autoscaler.py <scan-loop|status|tick|set-config <json>> [BASE]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
