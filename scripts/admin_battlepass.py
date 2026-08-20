#!/usr/bin/env python3
"""Battlepass engine — dune-admin port (MIT), reshaped for this stack.

A catalog of tiers ("level:5", "journey:DA_MQ_…", "tag:Exploration.…"),
each worth intel (the game's tech-knowledge currency) and optionally item
schematics. Progression signals are read from the game DB — character
level (derived from FLevelComponent.TotalXPEarned), completed journey
nodes, exploration/achievement player tags. The default 188-tier catalog
(1,619 intel) is seeded from data/admin/battlepass-catalog.json, extracted
verbatim from upstream's generator; the DB copy is then the source of
truth and operator edits survive restarts (seed-if-empty, never clobber).

The two invariants every upstream incident traces back to (#259, #280,
#291, #293, #297), preserved exactly:

  * The claim state machine. `baseline` marks progress that existed
    before the pass watched the player — NEVER grantable. A tier only
    `earn`s after the engine has watched THAT tier go unsatisfied →
    satisfied for THAT account (the per-(account,tier) `seen` marker);
    `award_past` bypasses the watch. This is what stops a game update
    that back-fills default-completed nodes from auto-granting real intel
    to everyone (upstream #297).
  * The grant ledger, keyed (tier_key, account). Enqueue is INSERT OR
    IGNORE (safe every tick, retroactive for claims earned while
    auto-grant was off). Delivery order is money-safety: intel → flip
    claim to granted → seal ledger → items best-effort and never retried
    (a retry would re-pay intel). Intel requires the player OFFLINE (the
    game caches TechKnowledge in memory); "player online" is not a
    failure — 5-minute retry-later that never consumes attempts. Real
    failures back off 24h, 3 attempts, then exhausted (manual only).

Resets: `demote` flips earned → baseline (nothing left to deliver, claims
kept so re-evaluation can't re-earn); `purge` deletes claims AND seen
markers together — deleting claims alone would instantly re-earn every
still-satisfied tier and re-grant it (upstream's documented storm).

Talks to the game only through admin-publish.sh: db-sql reads, award-intel
(new, clamped + offline-gated) and give-item for delivery.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import admin_events  # xp_to_level, _csv_rows/_db_rows, RESERVED_ACCOUNTS

DB_PATH = admin_events.DB_PATH  # one state DB for the player-events family
CONFIG_PATH = "data/admin/battlepass.json"
CATALOG_PATH = "data/admin/battlepass-catalog.json"

SIGNALS = ("level", "journey_node", "player_tag")
STATUS_BASELINE = "baseline"
STATUS_EARNED = "earned"
STATUS_GRANTED = "granted"

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECS = 24 * 3600
ONLINE_RETRY_SECS = 5 * 60
MAX_INTEL = 2779  # cumulative spendable cap; granting past it is waste (#208)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS battlepass_tiers (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  tier_key     TEXT NOT NULL UNIQUE,
  category     TEXT NOT NULL,
  label        TEXT NOT NULL,
  signal       TEXT NOT NULL,
  signal_key   TEXT NOT NULL DEFAULT '',
  threshold    INTEGER NOT NULL DEFAULT 0,
  intel        INTEGER NOT NULL DEFAULT 0,
  reward_items TEXT NOT NULL DEFAULT '',
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS battlepass_claims (
  tier_key   TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  status     TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (tier_key, account_id)
);
CREATE TABLE IF NOT EXISTS battlepass_tier_seen (
  tier_key   TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  PRIMARY KEY (tier_key, account_id)
);
CREATE TABLE IF NOT EXISTS battlepass_grant_ledger (
  tier_key        TEXT NOT NULL,
  account_id      INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT NOT NULL DEFAULT '',
  next_attempt_at INTEGER NOT NULL DEFAULT 0,
  updated_at      INTEGER NOT NULL,
  PRIMARY KEY (tier_key, account_id)
);
"""


def connect(base: str) -> sqlite3.Connection:
    conn = admin_events.connect(base)
    conn.executescript(_SCHEMA)
    return conn


def load_config(base: str) -> dict:
    cfg = {"enabled": False, "award_past": False, "auto_grant": False,
           "poll_seconds": 60}
    try:
        with open(os.path.join(base, CONFIG_PATH), encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key in ("enabled", "award_past", "auto_grant"):
                cfg[key] = raw.get(key) is True
            poll = raw.get("poll_seconds", 60)
            if isinstance(poll, int) and not isinstance(poll, bool):
                cfg["poll_seconds"] = min(600, max(10, poll))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(base: str, cfg: dict) -> bool:
    path = os.path.join(base, CONFIG_PATH)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": cfg.get("enabled") is True,
                       "award_past": cfg.get("award_past") is True,
                       "auto_grant": cfg.get("auto_grant") is True,
                       "poll_seconds": int(cfg.get("poll_seconds", 60))},
                      f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Catalog / tiers
# ---------------------------------------------------------------------------

def validate_tier(tier: dict) -> None:
    for field in ("tier_key", "category", "label"):
        if not isinstance(tier.get(field), str) or not tier[field]:
            raise ValueError(f"tier.{field} is required")
    if tier.get("signal") not in SIGNALS:
        raise ValueError(f"tier.signal must be one of {SIGNALS}")
    threshold = tier.get("threshold", 0)
    intel = tier.get("intel", 0)
    if not isinstance(threshold, int) or threshold < 0:
        raise ValueError("tier.threshold must be >= 0")
    if not isinstance(intel, int) or intel < 0:
        raise ValueError("tier.intel must be >= 0")
    if tier["signal"] == "level" and threshold < 1:
        raise ValueError("level tiers need threshold >= 1")
    if tier["signal"] != "level" and not tier.get("signal_key"):
        raise ValueError(f"{tier['signal']} tiers need a signal_key")
    items = tier.get("reward_items", "")
    if items:
        parsed = json.loads(items)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("reward_items must be a non-empty JSON list"
                             " (use '' for intel-only)")
        for it in parsed:
            if not isinstance(it, dict) or not it.get("template"):
                raise ValueError("reward_items entries need a template")


def _insert_tier(conn, tier: dict, now: int) -> None:
    conn.execute(
        "INSERT INTO battlepass_tiers (tier_key,category,label,signal,"
        "signal_key,threshold,intel,reward_items,enabled,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tier["tier_key"], tier["category"], tier["label"], tier["signal"],
         tier.get("signal_key", ""), int(tier.get("threshold", 0)),
         int(tier.get("intel", 0)), tier.get("reward_items", ""),
         1 if tier.get("enabled", True) else 0, now, now))


def _load_catalog(base: str) -> list[dict]:
    with open(os.path.join(base, CATALOG_PATH), encoding="utf-8") as f:
        doc = json.load(f)
    tiers = doc.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("catalog has no tiers")
    seen = set()
    for tier in tiers:
        validate_tier(tier)
        if tier["tier_key"] in seen:
            raise ValueError(f"duplicate tier_key {tier['tier_key']}")
        seen.add(tier["tier_key"])
    return tiers


def seed_tiers_if_empty(base: str) -> int:
    """First-boot seed only — an operator-edited catalog is never clobbered
    by a restart (upstream discipline)."""
    now = int(time.time())
    with connect(base) as conn:
        count = conn.execute("SELECT COUNT(*) FROM battlepass_tiers").fetchone()[0]
        if count:
            return 0
        tiers = _load_catalog(base)
        for tier in tiers:
            _insert_tier(conn, tier, now)
        return len(tiers)


def reseed_tiers(base: str, tiers: list[dict] | None = None) -> int:
    """Explicit reset/import: replace every tier. Claims SURVIVE — they
    re-attach by tier_key. The whole payload validates before any write."""
    if tiers is None:
        tiers = _load_catalog(base)
    else:
        seen = set()
        for tier in tiers:
            validate_tier(tier)
            if tier["tier_key"] in seen:
                raise ValueError(f"duplicate tier_key {tier['tier_key']}")
            seen.add(tier["tier_key"])
        if not tiers:
            raise ValueError("empty tier list")
    now = int(time.time())
    with connect(base) as conn:
        conn.execute("DELETE FROM battlepass_tiers")
        for tier in tiers:
            _insert_tier(conn, tier, now)
        return len(tiers)


def list_tiers(base: str) -> list[dict]:
    with connect(base) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM battlepass_tiers ORDER BY id").fetchall()]


def update_tier(base: str, tier_id: int, **fields) -> bool:
    allowed = {"label", "intel", "enabled", "reward_items", "category",
               "signal", "signal_key", "threshold"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update field(s): {sorted(unknown)}"
                         " (tier_key is immutable)")
    with connect(base) as conn:
        row = conn.execute("SELECT * FROM battlepass_tiers WHERE id=?",
                           (tier_id,)).fetchone()
        if not row:
            return False
        merged = dict(row)
        for k, v in fields.items():
            merged[k] = v
        merged["enabled"] = bool(merged["enabled"])
        validate_tier(merged)
        conn.execute(
            "UPDATE battlepass_tiers SET label=?, intel=?, enabled=?,"
            " reward_items=?, category=?, signal=?, signal_key=?, threshold=?,"
            " updated_at=? WHERE id=?",
            (merged["label"], int(merged["intel"]),
             1 if merged["enabled"] else 0, merged["reward_items"],
             merged["category"], merged["signal"], merged["signal_key"],
             int(merged["threshold"]), int(time.time()), tier_id))
        return True


# ---------------------------------------------------------------------------
# Game reads
# ---------------------------------------------------------------------------

def fetch_tracked_players(publish) -> list[dict]:
    """Every real account with a pawn — OFFLINE INCLUDED (the pass tracks
    everyone, unlike the events engine)."""
    reserved = ", ".join(str(a) for a in admin_events.RESERVED_ACCOUNTS)
    rows = admin_events._db_rows(publish, f"""
SELECT eps.account_id,
       COALESCE(ea."user", '') AS fls,
       COALESCE(convert_from(eps.encrypted_character_name, 'UTF8'), '') AS name,
       (eps.online_status = 'Online') AS online,
       COALESCE((fe.components->'FLevelComponent'->1->>'TotalXPEarned')::bigint, 0) AS xp
FROM dune.encrypted_player_state eps
LEFT JOIN dune.encrypted_accounts ea ON ea.id = eps.account_id
LEFT JOIN dune.actor_fgl_entities afe
       ON afe.actor_id = eps.player_pawn_id AND afe.slot_name = 'DuneCharacter'
LEFT JOIN dune.fgl_entities fe ON fe.entity_id = afe.entity_id
WHERE eps.player_pawn_id IS NOT NULL
  AND eps.account_id NOT IN ({reserved})""")
    return [{"account_id": int(r["account_id"]), "fls": r["fls"],
             "name": r["name"],
             "online": r["online"] in ("t", "true", "True"),
             "level": admin_events.xp_to_level(int(r["xp"] or 0))}
            for r in rows]


def fetch_journey_nodes(publish, account_id: int) -> set[str]:
    cols = admin_events._db_rows(
        publish,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema='dune' AND table_name='journey_story_node'")
    names = {c["column_name"] for c in cols}
    if "account_id" in names:
        where = f"account_id = {int(account_id)}"
    else:
        # migrated schema: resolve via the MOST RECENTLY ACTIVE character
        # (upstream #290 — oldest-id resolution silently hit a stale
        # duplicate).
        where = f"""character_id = (
            SELECT id FROM dune.encrypted_player_state
            WHERE account_id = {int(account_id)}
            ORDER BY last_login_time DESC NULLS LAST, id DESC LIMIT 1)"""
    rows = admin_events._db_rows(publish, f"""
SELECT story_node_id FROM dune.journey_story_node
WHERE {where} AND complete_condition_state = 'true'::jsonb""")
    return {r["story_node_id"] for r in rows}


def fetch_tags(publish, account_id: int) -> set[str]:
    cols = admin_events._db_rows(
        publish,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema='dune' AND table_name='player_tags'")
    names = {c["column_name"] for c in cols}
    if "account_id" in names:
        where = f"account_id = {int(account_id)}"
    else:
        where = f"""character_id = (
            SELECT id FROM dune.encrypted_player_state
            WHERE account_id = {int(account_id)}
            ORDER BY last_login_time DESC NULLS LAST, id DESC LIMIT 1)"""
    rows = admin_events._db_rows(publish,
                                 f"SELECT tag FROM dune.player_tags WHERE {where}")
    return {r["tag"] for r in rows}


def tier_satisfied(tier: dict, player: dict, journey: set[str],
                   tags: set[str]) -> bool:
    if tier["signal"] == "level":
        return player["level"] >= int(tier["threshold"])
    if tier["signal"] == "journey_node":
        return tier["signal_key"] in journey
    return tier["signal_key"] in tags


def tier_action(satisfied: bool, seen: bool, award_past: bool) -> str:
    """'' | 'mark_seen' | 'baseline' | 'earned' — upstream's decision table
    (battlepassTierAction), verbatim."""
    if not satisfied:
        return "mark_seen"
    if award_past or seen:
        return "earned"
    return "baseline"


# ---------------------------------------------------------------------------
# Claims / ledger
# ---------------------------------------------------------------------------

def _record_claim(conn, tier_key: str, account_id: int, status: str, now: int):
    """INSERT-or-ignore: re-evaluation never downgrades earned/granted."""
    conn.execute(
        "INSERT INTO battlepass_claims (tier_key,account_id,status,"
        "created_at,updated_at) VALUES (?,?,?,?,?)"
        " ON CONFLICT(tier_key,account_id) DO NOTHING",
        (tier_key, account_id, status, now, now))


def _enqueue_grant(conn, tier_key: str, account_id: int, now: int):
    conn.execute(
        "INSERT INTO battlepass_grant_ledger (tier_key,account_id,status,"
        "updated_at) VALUES (?,?,'pending',?)"
        " ON CONFLICT(tier_key,account_id) DO NOTHING",
        (tier_key, account_id, now))


def list_claims(base: str, account_id: int | None = None) -> list[dict]:
    with connect(base) as conn:
        if account_id is None:
            rows = conn.execute("SELECT * FROM battlepass_claims"
                                " ORDER BY account_id, tier_key").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM battlepass_claims WHERE account_id=?"
                " ORDER BY tier_key", (account_id,)).fetchall()
        return [dict(r) for r in rows]


def list_ledger(base: str) -> list[dict]:
    with connect(base) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM battlepass_grant_ledger"
            " ORDER BY account_id, tier_key").fetchall()]


def reset_claims(base: str, mode: str, account_id: int = 0) -> dict:
    """Incident cleanup, upstream shape. Both modes first delete
    pending+exhausted ledger rows (granted rows are delivery history)."""
    if mode not in ("demote", "purge"):
        raise ValueError("mode must be demote or purge")
    now = int(time.time())
    scope_sql = "" if not account_id else " AND account_id = ?"
    scope_args = [] if not account_id else [account_id]
    with connect(base) as conn:
        conn.execute(
            f"DELETE FROM battlepass_grant_ledger WHERE status != 'granted'{scope_sql}",
            scope_args)
        if mode == "demote":
            cur = conn.execute(
                f"UPDATE battlepass_claims SET status='baseline', updated_at=?"
                f" WHERE status='earned'{scope_sql}", [now] + scope_args)
            return {"mode": mode, "changed": cur.rowcount}
        # purge: claims AND seen markers together — atomically, in one
        # transaction. Purging claims alone re-earns every still-satisfied
        # tier on the next scan (the documented storm).
        cur = conn.execute(
            f"DELETE FROM battlepass_claims WHERE 1=1{scope_sql}", scope_args)
        conn.execute(
            f"DELETE FROM battlepass_tier_seen WHERE 1=1{scope_sql}", scope_args)
        return {"mode": mode, "changed": cur.rowcount}


# ---------------------------------------------------------------------------
# Grant delivery
# ---------------------------------------------------------------------------

def _deliver_grant(conn, publish, tier: dict, account_id: int, fls: str,
                   now: int) -> str:
    """One ledger row. Money-safety order: intel → claim granted → seal
    ledger → items best-effort (never retried). Returns
    granted|retry_later|failed|skipped."""
    ledger = conn.execute(
        "SELECT * FROM battlepass_grant_ledger WHERE tier_key=? AND account_id=?",
        (tier["tier_key"], account_id)).fetchone()
    if ledger and ledger["status"] == "granted":
        return "skipped"
    claim = conn.execute(
        "SELECT status FROM battlepass_claims WHERE tier_key=? AND account_id=?",
        (tier["tier_key"], account_id)).fetchone()
    if not claim or claim["status"] != STATUS_EARNED:
        # a manual grant raced the loop — seal and move on
        conn.execute(
            "UPDATE battlepass_grant_ledger SET status='granted', updated_at=?"
            " WHERE tier_key=? AND account_id=?",
            (now, tier["tier_key"], account_id))
        return "skipped"
    intel = int(tier["intel"] or 0)
    if intel > 0:
        rc, out = publish(["award-intel", fls, str(intel)])
        if rc == 4:  # player online — not a failure, short retry, no attempt
            conn.execute(
                "UPDATE battlepass_grant_ledger SET next_attempt_at=?,"
                " updated_at=? WHERE tier_key=? AND account_id=?",
                (now + ONLINE_RETRY_SECS, now, tier["tier_key"], account_id))
            return "retry_later"
        if rc != 0:
            conn.execute(
                "UPDATE battlepass_grant_ledger SET attempts = attempts + 1,"
                " status = CASE WHEN attempts + 1 >= ? THEN 'exhausted'"
                "               ELSE 'pending' END,"
                " last_error=?, next_attempt_at=?, updated_at=?"
                " WHERE tier_key=? AND account_id=?",
                (MAX_ATTEMPTS, out[-300:], now + RETRY_BACKOFF_SECS, now,
                 tier["tier_key"], account_id))
            return "failed"
    conn.execute(
        "UPDATE battlepass_claims SET status='granted', updated_at=?"
        " WHERE tier_key=? AND account_id=?",
        (now, tier["tier_key"], account_id))
    conn.execute(
        "UPDATE battlepass_grant_ledger SET status='granted', last_error='',"
        " updated_at=? WHERE tier_key=? AND account_id=?",
        (now, tier["tier_key"], account_id))
    if tier.get("reward_items"):
        try:
            items = json.loads(tier["reward_items"])
        except ValueError:
            items = []
        for it in items:
            publish(["give-item", fls, str(it.get("template", "")),
                     str(it.get("qty", 1)), str(it.get("quality", 0))])
    return "granted"


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

def run_tick(base: str, publish, now: int | None = None) -> dict:
    now = int(now if now is not None else time.time())
    cfg = load_config(base)
    summary = {"scanned": 0, "baselined": 0, "earned": 0, "seen": 0,
               "granted": 0, "retry_later": 0, "failed": 0}
    if not cfg["enabled"]:
        return summary
    with connect(base) as conn:
        tiers = [dict(r) for r in conn.execute(
            "SELECT * FROM battlepass_tiers WHERE enabled=1").fetchall()]
        if not tiers:
            return summary
        players = fetch_tracked_players(publish)
        for player in players:
            account = player["account_id"]
            summary["scanned"] += 1
            claimed = {r["tier_key"] for r in conn.execute(
                "SELECT tier_key FROM battlepass_claims WHERE account_id=?",
                (account,)).fetchall()}
            open_tiers = [t for t in tiers if t["tier_key"] not in claimed]
            if not open_tiers:
                continue
            # fetch expensive signals only when an open tier needs them
            journey: set[str] = set()
            tags: set[str] = set()
            if any(t["signal"] == "journey_node" for t in open_tiers):
                journey = fetch_journey_nodes(publish, account)
            if any(t["signal"] == "player_tag" for t in open_tiers):
                tags = fetch_tags(publish, account)
            seen = {r["tier_key"] for r in conn.execute(
                "SELECT tier_key FROM battlepass_tier_seen WHERE account_id=?",
                (account,)).fetchall()}
            for tier in open_tiers:
                satisfied = tier_satisfied(tier, player, journey, tags)
                action = tier_action(satisfied, tier["tier_key"] in seen,
                                     cfg["award_past"])
                if action == "mark_seen":
                    conn.execute(
                        "INSERT INTO battlepass_tier_seen (tier_key,account_id)"
                        " VALUES (?,?) ON CONFLICT DO NOTHING",
                        (tier["tier_key"], account))
                    summary["seen"] += 1
                elif action == "baseline":
                    _record_claim(conn, tier["tier_key"], account,
                                  STATUS_BASELINE, now)
                    summary["baselined"] += 1
                else:
                    _record_claim(conn, tier["tier_key"], account,
                                  STATUS_EARNED, now)
                    summary["earned"] += 1
        if cfg["auto_grant"]:
            # retroactive enqueue every tick: claims earned while auto-grant
            # was off must not sit pending forever (upstream #259/#280)
            for row in conn.execute(
                    "SELECT tier_key, account_id FROM battlepass_claims"
                    " WHERE status='earned'").fetchall():
                _enqueue_grant(conn, row["tier_key"], row["account_id"], now)
            by_account = {p["account_id"]: p for p in players}
            tiers_by_key = {t["tier_key"]: t for t in tiers}
            due = conn.execute(
                "SELECT * FROM battlepass_grant_ledger WHERE status='pending'"
                " AND attempts < ? AND next_attempt_at <= ?",
                (MAX_ATTEMPTS, now)).fetchall()
            for row in due:
                tier = tiers_by_key.get(row["tier_key"])
                player = by_account.get(row["account_id"])
                if not tier or not player or not player["fls"]:
                    continue
                outcome = _deliver_grant(conn, publish, tier,
                                         row["account_id"], player["fls"], now)
                if outcome in summary:
                    summary[outcome] += 1
    return summary


def grant_account(base: str, publish, account_id: int) -> dict:
    """Manual grant: deliver every earned tier for one account now (ignores
    backoff and attempt limits; still refuses an online player via
    award-intel's gate)."""
    now = int(time.time())
    with connect(base) as conn:
        tiers = {t["tier_key"]: t for t in
                 (dict(r) for r in conn.execute(
                     "SELECT * FROM battlepass_tiers").fetchall())}
        earned = [r["tier_key"] for r in conn.execute(
            "SELECT tier_key FROM battlepass_claims WHERE account_id=?"
            " AND status='earned'", (account_id,)).fetchall()]
        if not earned:
            return {"granted": 0, "failed": 0, "detail": "nothing earned"}
        target = admin_events._db_rows(publish, f"""
SELECT COALESCE(ea."user", '') AS fls
FROM dune.encrypted_player_state eps
LEFT JOIN dune.encrypted_accounts ea ON ea.id = eps.account_id
WHERE eps.account_id = {int(account_id)} LIMIT 1""")
        fls = target[0]["fls"] if target else ""
        if not fls:
            return {"granted": 0, "failed": len(earned),
                    "detail": "grant target not resolvable"}
        granted = failed = 0
        detail = ""
        for tier_key in earned:
            tier = tiers.get(tier_key)
            if not tier:
                continue
            _enqueue_grant(conn, tier_key, account_id, now)
            outcome = _deliver_grant(conn, publish, tier, account_id, fls, now)
            if outcome == "granted":
                granted += 1
            elif outcome in ("failed", "retry_later"):
                failed += 1
                if outcome == "retry_later":
                    detail = "player is online — intel needs them offline"
                    break
        return {"granted": granted, "failed": failed, "detail": detail}
