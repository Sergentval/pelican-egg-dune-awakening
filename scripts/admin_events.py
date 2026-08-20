#!/usr/bin/env python3
"""Player-events engine — dune-admin port (MIT), reshaped for this stack.

An "event" is an admin-defined watcher over the game's Postgres. Two types:

  zone_race  — config {"map","x","y","z","radius","participants":[account…]}:
               each tick, the FIRST participant (in list order) who is
               online, on the right map (empty = any) and inside the sphere
               wins — at most one outcome per tick. Everyone on the list
               who reaches the zone eventually gets paid once; the event
               keeps watching until disabled.
  milestone  — config {"signal":"level","threshold":N,"award_past":bool} or
               {"signal":"achievement_tag","tag_name":"…","award_past":…}:
               each tick, every online qualifying player gets an outcome.

Events never touch INI/cvars — their only effect is granting the shared
reward spec (admin_rewards) to individual players and an in-game broadcast
(upstream announced to Discord; our stack's broadcast banner is the 1:1
substitute). There is nothing to revert: disabling stops evaluation,
grants stay.

Safety model, straight from upstream's incident history:
  * The claim ledger (event_id, version, account_id) is the idempotency
    truth — granted/exhausted block delivery, pending resumes the
    un-granted remainder via the structured cursor (admin_rewards).
  * Failures retry with +24h backoff, 3 attempts, then exhausted (manual
    grant only). Retries resolve targets offline-capably (give-item).
  * On daemon (re)start, a silent backfill seals every already-satisfied
    milestone so a restart never re-announces past deeds; rewards flow
    during backfill only when the event's award_past says so. Zone races
    are session-scoped and never backfilled.

The engine talks to the game ONLY through admin-publish.sh (injected as
`publish(argv) -> (rc, stdout)`): db-sql for reads, give-currency /
give / give-item / award-track-xp for grants, broadcast for announces.
Admin-side state lives in server/state/player-events.db (SQLite).
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import sqlite3
import time

import admin_rewards

DB_PATH = "server/state/player-events.db"
CONFIG_PATH = "data/admin/events-engine.json"

EVENT_TYPES = ("zone_race", "milestone")
MILESTONE_SIGNALS = ("level", "achievement_tag")

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECS = 24 * 3600
DEFAULT_POLL_SECS = 7
DEFAULT_JITTER_SECS = 3

# Reserved system identities (GM / Server / MOTD) are never participants.
RESERVED_ACCOUNTS = (9000001, 9000002, 9000003)

# cumulativeXPByLevel[i] = total XP to reach level i (Funcom
# SkillXPPerLevel.json, extracted from dune-admin db.go; level 200 caps at
# 344,440 XP).
CUMULATIVE_XP = [
    0, 40, 215, 440, 740, 1240, 1790, 2390, 2990, 3590, 4190,
    4790, 5390, 5990, 6590, 7190, 7790, 8390, 8990, 9590, 10190,
    10790, 11390, 11990, 12590, 13190, 13790, 14390, 14990, 15590, 16190,
    16790, 17390, 17990, 18590, 19190, 19790, 20390, 20990, 21590, 22190,
    22790, 23390, 23990, 24590, 25190, 25790, 26390, 26990, 27590, 28190,
    28790, 29390, 29990, 30590, 31190, 31790, 32390, 32990, 33590, 34190,
    34790, 35390, 35990, 36590, 37190, 37790, 38390, 38990, 39590, 40190,
    40790, 41390, 41990, 42590, 43190, 43790, 44390, 44990, 45590, 46190,
    46790, 47390, 47990, 48590, 49190, 49790, 50390, 50990, 51590, 52190,
    52790, 53390, 53990, 54590, 55190, 55790, 56390, 56990, 57590, 58190,
    58840, 59490, 60140, 60790, 61440, 62090, 62740, 63390, 64040, 64690,
    65340, 65990, 66640, 67290, 67940, 68590, 69240, 69890, 70540, 71190,
    71840, 72490, 73140, 73790, 74440, 75090, 75740, 76391, 77044, 77699,
    78357, 79018, 79683, 80353, 81030, 81714, 82407, 83110, 83825, 84554,
    85298, 86060, 86842, 87646, 88475, 89332, 90220, 91141, 92100, 93099,
    94143, 95235, 96380, 97582, 98845, 100175, 101576, 103054, 104614, 106263,
    108006, 109849, 111799, 113862, 116046, 118358, 120806, 123397, 126139, 129041,
    132112, 135360, 138795, 142426, 146263, 150316, 154596, 159114, 163880, 168906,
    174203, 179784, 185661, 191846, 198353, 205195, 212385, 219938, 227868, 236190,
    244918, 254069, 263657, 273700, 284213, 295214, 306719, 318746, 331314, 344440,
]


def xp_to_level(xp: int) -> int:
    if xp <= 0:
        return 0
    lo, hi = 1, 200
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if CUMULATIVE_XP[mid] <= xp:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_definitions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT NOT NULL,
  type              TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 0,
  version           INTEGER NOT NULL DEFAULT 1,
  config_json       TEXT NOT NULL DEFAULT '{}',
  reward_json       TEXT NOT NULL DEFAULT '',
  announce_template TEXT NOT NULL DEFAULT '',
  poll_seconds      INTEGER NOT NULL DEFAULT 7,
  jitter_seconds    INTEGER NOT NULL DEFAULT 3,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS event_award_claims (
  event_id        INTEGER NOT NULL,
  version         INTEGER NOT NULL,
  account_id      INTEGER NOT NULL,
  status          TEXT NOT NULL,
  cursor          TEXT NOT NULL DEFAULT '',
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT NOT NULL DEFAULT '',
  next_attempt_at INTEGER NOT NULL DEFAULT 0,
  claimed_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  PRIMARY KEY (event_id, version, account_id)
);
CREATE TABLE IF NOT EXISTS engine_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def db_path(base: str) -> str:
    return os.path.join(base, DB_PATH)


def connect(base: str) -> sqlite3.Connection:
    path = db_path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def load_enabled(base: str) -> bool:
    try:
        with open(os.path.join(base, CONFIG_PATH), encoding="utf-8") as f:
            return json.load(f).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


# ---- definition CRUD ------------------------------------------------------

def validate_definition(name: str, ev_type: str, config_json: str,
                        reward_json: str) -> None:
    """Raises ValueError with an operator-facing message."""
    if not name or not name.strip():
        raise ValueError("event name is required")
    if ev_type not in EVENT_TYPES:
        raise ValueError(f"event type must be one of {EVENT_TYPES}")
    try:
        cfg = json.loads(config_json or "{}")
    except ValueError as exc:
        raise ValueError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    if ev_type == "zone_race":
        radius = cfg.get("radius")
        if not isinstance(radius, (int, float)) or radius <= 0:
            raise ValueError("zone_race.radius must be a positive number")
        parts = cfg.get("participants")
        if not isinstance(parts, list) or not parts \
                or not all(isinstance(p, int) and p > 0 for p in parts):
            raise ValueError("zone_race.participants must be a non-empty "
                             "list of account ids")
        for axis in ("x", "y", "z"):
            if not isinstance(cfg.get(axis, 0), (int, float)):
                raise ValueError(f"zone_race.{axis} must be a number")
    else:
        signal = cfg.get("signal")
        if signal not in MILESTONE_SIGNALS:
            raise ValueError(f"milestone.signal must be one of {MILESTONE_SIGNALS}")
        if signal == "level":
            thr = cfg.get("threshold")
            if not isinstance(thr, int) or not 1 <= thr <= 200:
                raise ValueError("milestone.threshold must be 1..200")
        else:
            if not isinstance(cfg.get("tag_name"), str) or not cfg["tag_name"]:
                raise ValueError("milestone.tag_name is required")
    admin_rewards.parse_spec(reward_json)  # raises on bad spec


def create_event(base: str, name: str, ev_type: str, config_json: str = "{}",
                 reward_json: str = "", announce_template: str = "",
                 poll_seconds: int = DEFAULT_POLL_SECS,
                 jitter_seconds: int = DEFAULT_JITTER_SECS) -> int:
    validate_definition(name, ev_type, config_json, reward_json)
    now = int(time.time())
    with connect(base) as conn:
        cur = conn.execute(
            "INSERT INTO event_definitions (name,type,enabled,config_json,"
            "reward_json,announce_template,poll_seconds,jitter_seconds,"
            "created_at,updated_at) VALUES (?,?,0,?,?,?,?,?,?,?)",
            (name.strip(), ev_type, config_json or "{}", reward_json or "",
             announce_template or "", int(poll_seconds), int(jitter_seconds),
             now, now))
        return int(cur.lastrowid or 0)


def update_event(base: str, event_id: int, **fields) -> bool:
    allowed = {"name", "config_json", "reward_json", "announce_template",
               "poll_seconds", "jitter_seconds"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update field(s): {sorted(unknown)}")
    with connect(base) as conn:
        row = conn.execute("SELECT * FROM event_definitions WHERE id=?",
                           (event_id,)).fetchone()
        if not row:
            return False
        merged = {k: fields.get(k, row[k]) for k in allowed}
        validate_definition(str(merged["name"]), row["type"],
                            str(merged["config_json"]),
                            str(merged["reward_json"]))
        conn.execute(
            "UPDATE event_definitions SET name=?, config_json=?, reward_json=?,"
            " announce_template=?, poll_seconds=?, jitter_seconds=?,"
            " updated_at=? WHERE id=?",
            (str(merged["name"]).strip(), str(merged["config_json"]),
             str(merged["reward_json"]), str(merged["announce_template"]),
             int(merged["poll_seconds"]), int(merged["jitter_seconds"]),
             int(time.time()), event_id))
        return True


def set_enabled(base: str, event_id: int, enabled: bool) -> bool:
    with connect(base) as conn:
        cur = conn.execute(
            "UPDATE event_definitions SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, int(time.time()), event_id))
        return cur.rowcount > 0


def delete_event(base: str, event_id: int) -> bool:
    with connect(base) as conn:
        cur = conn.execute("DELETE FROM event_definitions WHERE id=?", (event_id,))
        conn.execute("DELETE FROM event_award_claims WHERE event_id=?", (event_id,))
        conn.execute("DELETE FROM engine_state WHERE key=?", (f"next_due:{event_id}",))
        return cur.rowcount > 0


def reset_claims(base: str, event_id: int) -> int:
    """Forget who was paid — the next tick pays qualifiers again. A re-arm,
    not a rollback (upstream semantics)."""
    with connect(base) as conn:
        cur = conn.execute("DELETE FROM event_award_claims WHERE event_id=?",
                           (event_id,))
        return cur.rowcount


def list_events(base: str) -> list[dict]:
    with connect(base) as conn:
        rows = conn.execute(
            "SELECT * FROM event_definitions ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            counts = conn.execute(
                "SELECT status, COUNT(*) n FROM event_award_claims"
                " WHERE event_id=? AND version=? GROUP BY status",
                (r["id"], r["version"])).fetchall()
            d["claims"] = {c["status"]: c["n"] for c in counts}
            out.append(d)
        return out


def list_claims(base: str, event_id: int) -> list[dict]:
    with connect(base) as conn:
        rows = conn.execute(
            "SELECT * FROM event_award_claims WHERE event_id=?"
            " ORDER BY claimed_at", (event_id,)).fetchall()
        return [dict(r) for r in rows]


# ---- claim ledger ---------------------------------------------------------

def _claim(conn, event_id: int, version: int, account_id: int):
    return conn.execute(
        "SELECT * FROM event_award_claims WHERE event_id=? AND version=?"
        " AND account_id=?", (event_id, version, account_id)).fetchone()


def _seal_granted(conn, event_id, version, account_id, now):
    conn.execute(
        "INSERT INTO event_award_claims (event_id,version,account_id,status,"
        "claimed_at,updated_at) VALUES (?,?,?,'granted',?,?)"
        " ON CONFLICT(event_id,version,account_id) DO UPDATE SET"
        " status='granted', cursor='', last_error='', updated_at=?",
        (event_id, version, account_id, now, now, now))


def _record_failed(conn, event_id, version, account_id, cursor, error, now):
    """Single statement, upstream's shape: insert-as-attempt-1 or bump on
    conflict, flipping to exhausted at MAX_ATTEMPTS — no read-modify-write
    race."""
    conn.execute(
        "INSERT INTO event_award_claims (event_id,version,account_id,status,"
        "cursor,attempts,last_error,next_attempt_at,claimed_at,updated_at)"
        " VALUES (?,?,?,'pending',?,1,?,?,?,?)"
        " ON CONFLICT(event_id,version,account_id) DO UPDATE SET"
        "  attempts = attempts + 1,"
        "  status = CASE WHEN attempts + 1 >= ? THEN 'exhausted'"
        "                ELSE 'pending' END,"
        "  cursor = excluded.cursor, last_error = excluded.last_error,"
        "  next_attempt_at = excluded.next_attempt_at, updated_at = excluded.updated_at",
        (event_id, version, account_id, cursor, error[:500],
         now + RETRY_BACKOFF_SECS, now, now, MAX_ATTEMPTS))


# ---------------------------------------------------------------------------
# Game reads / grant runner (all through publish)
# ---------------------------------------------------------------------------

def _csv_rows(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


def _db_rows(publish, sql: str) -> list[dict]:
    rc, out = publish(["db-sql", sql])
    if rc != 0:
        raise RuntimeError(f"db-sql failed rc={rc}")
    return _csv_rows(out)


def fetch_online_players(publish) -> list[dict]:
    """[{account_id, controller_id, pawn_id, fls, name}] for Online players,
    reserved identities excluded."""
    reserved = ", ".join(str(a) for a in RESERVED_ACCOUNTS)
    rows = _db_rows(publish, f"""
SELECT eps.account_id,
       COALESCE(eps.player_controller_id, 0) AS controller_id,
       eps.player_pawn_id AS pawn_id,
       COALESCE(ea."user", '') AS fls,
       COALESCE(convert_from(eps.encrypted_character_name, 'UTF8'), '') AS name
FROM dune.encrypted_player_state eps
LEFT JOIN dune.encrypted_accounts ea ON ea.id = eps.account_id
WHERE eps.online_status = 'Online'
  AND eps.player_pawn_id IS NOT NULL
  AND eps.account_id NOT IN ({reserved})""")
    return [{"account_id": int(r["account_id"]),
             "controller_id": int(r["controller_id"]),
             "pawn_id": int(r["pawn_id"]),
             "fls": r["fls"], "name": r["name"]} for r in rows]


def fetch_positions(publish, account_ids: list[int]) -> dict[int, dict]:
    if not account_ids:
        return {}
    ids = ", ".join(str(int(a)) for a in account_ids)
    rows = _db_rows(publish, f"""
SELECT eps.account_id, COALESCE(a.map, '') AS map,
       ((a.transform).location).x AS x,
       ((a.transform).location).y AS y,
       ((a.transform).location).z AS z
FROM dune.encrypted_player_state eps
JOIN dune.actors a ON a.id = eps.player_pawn_id
WHERE eps.online_status = 'Online' AND eps.player_pawn_id IS NOT NULL
  AND eps.account_id IN ({ids})""")
    return {int(r["account_id"]): {"map": r["map"],
                                   "x": float(r["x"] or 0),
                                   "y": float(r["y"] or 0),
                                   "z": float(r["z"] or 0)} for r in rows}


def fetch_levels(publish, account_ids: list[int]) -> dict[int, int]:
    if not account_ids:
        return {}
    ids = ", ".join(str(int(a)) for a in account_ids)
    rows = _db_rows(publish, f"""
SELECT eps.account_id,
       COALESCE((fe.components->'FLevelComponent'->1->>'TotalXPEarned')::bigint, 0) AS xp
FROM dune.encrypted_player_state eps
JOIN dune.actor_fgl_entities afe
  ON afe.actor_id = eps.player_pawn_id AND afe.slot_name = 'DuneCharacter'
JOIN dune.fgl_entities fe ON fe.entity_id = afe.entity_id
WHERE eps.account_id IN ({ids})""")
    return {int(r["account_id"]): xp_to_level(int(r["xp"] or 0)) for r in rows}


def fetch_tag_holders(publish, account_ids: list[int], tag: str) -> set[int]:
    """Accounts among account_ids holding `tag`. The tags table may be keyed
    by account_id or character_id depending on game version (upstream #267);
    resolve through the most-recently-active character when migrated
    (upstream #290)."""
    if not account_ids:
        return set()
    ids = ", ".join(str(int(a)) for a in account_ids)
    safe_tag = tag.replace("'", "''")
    cols = _db_rows(publish,
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema='dune' AND table_name='player_tags'")
    names = {c["column_name"] for c in cols}
    if "account_id" in names:
        rows = _db_rows(publish, f"""
SELECT DISTINCT pt.account_id AS account_id FROM dune.player_tags pt
WHERE pt.account_id IN ({ids}) AND pt.tag = '{safe_tag}'""")
    else:
        rows = _db_rows(publish, f"""
SELECT DISTINCT eps.account_id AS account_id
FROM dune.player_tags pt
JOIN LATERAL (
    SELECT e.account_id FROM dune.encrypted_player_state e
    WHERE e.id = pt.character_id
) eps ON TRUE
WHERE eps.account_id IN ({ids}) AND pt.tag = '{safe_tag}'""")
    return {int(r["account_id"]) for r in rows}


def make_grant_runner(publish, fls: str, online: bool):
    """admin_rewards runner for one target player. Items go through the
    online RMQ give when the player is connected, the offline DB insert
    otherwise; currency/xp subcommands carry their own safety."""
    def runner(step):
        stage, payload = step["stage"], step["payload"]
        if stage == "currency":
            argv = ["give-currency", fls, str(payload["amount"])]
        elif stage == "items":
            if online:
                argv = ["give", fls, payload["template"], str(payload["qty"])]
            else:
                argv = ["give-item", fls, payload["template"],
                        str(payload["qty"]), str(payload["quality"])]
        else:
            argv = ["award-track-xp", fls, payload["track"],
                    str(payload["amount"])]
        rc, out = publish(argv)
        if rc != 0:
            return f"{argv[0]} rc={rc}: {out[-200:]}"
        return ""
    return runner


def render_announce(template: str, player: str, event: str, value) -> str:
    return (template.replace("{player}", player)
            .replace("{event}", event)
            .replace("{value}", str(value)))


# ---------------------------------------------------------------------------
# Tick engine
# ---------------------------------------------------------------------------

def _state_get(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def _state_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO engine_state (key,value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def _apply_outcome(conn, publish, event, player, value, now,
                   announce: bool, grant: bool) -> str:
    """One qualifying (event, player). Returns 'granted'|'skipped'|'failed'.
    Claim first (idempotency), deliver, seal, then announce — an announce
    never precedes a landed reward (upstream ordering)."""
    account = player["account_id"]
    row = _claim(conn, event["id"], event["version"], account)
    if row and row["status"] in ("granted", "exhausted"):
        return "skipped"
    cursor = row["cursor"] if row else ""
    spec = admin_rewards.parse_spec(event["reward_json"] if grant else "")
    if admin_rewards.is_empty(spec):
        _seal_granted(conn, event["id"], event["version"], account, now)
    else:
        runner = make_grant_runner(publish, player["fls"],
                                   player.get("online", True))
        result = admin_rewards.deliver(spec, runner, cursor)
        if not result["ok"]:
            _record_failed(conn, event["id"], event["version"], account,
                           result["cursor"], result["error"], now)
            return "failed"
        _seal_granted(conn, event["id"], event["version"], account, now)
    if announce and event["announce_template"]:
        msg = render_announce(event["announce_template"],
                              player.get("name") or player["fls"],
                              event["name"], value)
        publish(["broadcast", event["name"], msg, "20"])
    return "granted"


def _evaluate_event(conn, publish, event, players, now) -> dict:
    """Returns {granted, failed, skipped} counts for one due event."""
    counts = {"granted": 0, "failed": 0, "skipped": 0}
    cfg = json.loads(event["config_json"] or "{}")
    by_account = {p["account_id"]: p for p in players}

    if event["type"] == "zone_race":
        participants = [a for a in cfg.get("participants", []) if a in by_account]
        if not participants:
            return counts
        positions = fetch_positions(publish, participants)
        for account in cfg.get("participants", []):
            pos = positions.get(account)
            if not pos:
                continue
            if cfg.get("map") and pos["map"] != cfg["map"]:
                continue
            dx = pos["x"] - float(cfg.get("x", 0))
            dy = pos["y"] - float(cfg.get("y", 0))
            dz = pos["z"] - float(cfg.get("z", 0))
            if dx * dx + dy * dy + dz * dz > float(cfg["radius"]) ** 2:
                continue
            row = _claim(conn, event["id"], event["version"], account)
            if row and row["status"] in ("granted", "exhausted"):
                continue
            outcome = _apply_outcome(conn, publish, event, by_account[account],
                                     "winner", now, announce=True, grant=True)
            counts[outcome] += 1
            break  # at most one outcome per tick (upstream semantics)
        return counts

    signal = cfg.get("signal")
    if signal == "level":
        levels = fetch_levels(publish, list(by_account))
        qualifiers = [(a, levels[a]) for a in by_account
                      if levels.get(a, 0) >= int(cfg.get("threshold", 0))]
    else:
        holders = fetch_tag_holders(publish, list(by_account),
                                    str(cfg.get("tag_name", "")))
        qualifiers = [(a, cfg.get("tag_name", "")) for a in holders]
    for account, value in qualifiers:
        row = _claim(conn, event["id"], event["version"], account)
        if row and row["status"] in ("granted", "exhausted"):
            counts["skipped"] += 1
            continue
        outcome = _apply_outcome(conn, publish, event, by_account[account],
                                 value, now, announce=True, grant=True)
        counts[outcome] += 1
    return counts


def _retry_pass(conn, publish, now) -> int:
    """Re-attempt due pending claims (offline-capable: resolve current
    online state per target). Returns the number retried."""
    rows = conn.execute(
        "SELECT c.*, d.reward_json, d.name, d.announce_template, d.type"
        " FROM event_award_claims c"
        " JOIN event_definitions d ON d.id = c.event_id AND d.version = c.version"
        " WHERE c.status='pending' AND c.attempts < ? AND c.next_attempt_at <= ?",
        (MAX_ATTEMPTS, now)).fetchall()
    retried = 0
    for row in rows:
        target = _db_rows(publish, f"""
SELECT COALESCE(ea."user", '') AS fls,
       (eps.online_status = 'Online') AS online,
       COALESCE(convert_from(eps.encrypted_character_name, 'UTF8'), '') AS name
FROM dune.encrypted_player_state eps
LEFT JOIN dune.encrypted_accounts ea ON ea.id = eps.account_id
WHERE eps.account_id = {int(row['account_id'])}""")
        if not target or not target[0]["fls"]:
            _record_failed(conn, row["event_id"], row["version"],
                           row["account_id"], row["cursor"],
                           "grant target no longer resolvable", now)
            continue
        player = {"account_id": row["account_id"], "fls": target[0]["fls"],
                  "name": target[0]["name"],
                  "online": target[0]["online"] in ("t", "true", "True")}
        event = {"id": row["event_id"], "version": row["version"],
                 "name": row["name"], "reward_json": row["reward_json"],
                 "announce_template": ""}  # retries never re-announce
        _apply_outcome(conn, publish, event, player, "", now,
                       announce=False, grant=True)
        retried += 1
    return retried


def _backfill(conn, publish, events, players, now) -> int:
    """Silent seal of already-satisfied milestones after a daemon (re)start:
    grants only when the event's config award_past is true; never announces;
    zone races are session-scoped and skipped (upstream semantics)."""
    sealed = 0
    by_account = {p["account_id"]: p for p in players}
    for event in events:
        if event["type"] != "milestone" or not event["enabled"]:
            continue
        cfg = json.loads(event["config_json"] or "{}")
        award_past = cfg.get("award_past") is True
        if cfg.get("signal") == "level":
            levels = fetch_levels(publish, list(by_account))
            qualifiers = [a for a in by_account
                          if levels.get(a, 0) >= int(cfg.get("threshold", 0))]
        else:
            qualifiers = list(fetch_tag_holders(publish, list(by_account),
                                                str(cfg.get("tag_name", ""))))
        for account in qualifiers:
            row = _claim(conn, event["id"], event["version"], account)
            if row:
                continue
            outcome = _apply_outcome(conn, publish, event, by_account[account],
                                     "", now, announce=False, grant=award_past)
            if outcome != "failed":
                sealed += 1
    return sealed


def make_publish(base: str):
    """The real publish: shell out to admin-publish.sh. rc=0 returns pure
    stdout (db-sql CSV must stay clean); failures return both streams."""
    import subprocess
    script = os.path.join(base, "scripts", "admin-publish.sh")

    def publish(argv):
        try:
            res = subprocess.run(["bash", script] + [str(a) for a in argv],
                                 capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return 1, "publish timed out"
        if res.returncode == 0:
            return 0, res.stdout or ""
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    return publish


def run_tick(base: str, publish, now: int | None = None,
             rng: random.Random | None = None, boot: bool = False) -> dict:
    """One engine tick. Returns a summary dict for the daemon log line."""
    now = int(now if now is not None else time.time())
    rng = rng or random.Random()
    summary = {"due": 0, "granted": 0, "failed": 0, "skipped": 0,
               "retried": 0, "backfilled": 0}
    if not load_enabled(base):
        return summary
    with connect(base) as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM event_definitions").fetchall()]
        enabled = [e for e in events if e["enabled"]]
        due = []
        for e in enabled:
            key = f"next_due:{e['id']}"
            raw = _state_get(conn, key)
            next_due = int(raw) if raw.isdigit() else 0  # absent ⇒ fire now
            if next_due <= now:
                due.append(e)
        players = []
        if due or boot:
            players = fetch_online_players(publish)
        if boot:
            summary["backfilled"] = _backfill(conn, publish, events, players, now)
        for e in due:
            counts = _evaluate_event(conn, publish, e, players, now)
            for k, v in counts.items():
                summary[k] += v
            poll = e["poll_seconds"] if e["poll_seconds"] > 0 else DEFAULT_POLL_SECS
            jitter = max(0, e["jitter_seconds"])
            _state_set(conn, f"next_due:{e['id']}",
                       str(now + poll + rng.randint(0, jitter)))
        summary["due"] = len(due)
        summary["retried"] = _retry_pass(conn, publish, now)
    return summary


def main(argv: list[str]) -> int:
    import sys
    if len(argv) >= 2 and argv[0] == "tick":
        base = argv[1]
        boot = "--boot" in argv[2:]
        summary = run_tick(base, make_publish(base), boot=boot)
        if any(summary[k] for k in ("due", "granted", "failed", "retried",
                                    "backfilled")):
            print(f"[events] {json.dumps(summary)}")
        return 0
    print("usage: admin_events.py tick <base> [--boot]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
