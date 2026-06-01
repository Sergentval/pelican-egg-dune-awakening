#!/usr/bin/env python3
"""Pure character-progression math for the Dune admin stack — XP->level,
intel curve, keystone skill-point bonus, and the grant-all-keystones target
computation. Ported verbatim from Icehunter/dune-admin (MIT)
cmd/dune-admin/{db.go,keystones.go}; see ATTRIBUTION.md.

This is an IMPORTABLE library module (underscore name), distinct from the
admin-*.py CLI scripts. It holds NO database/network state: the live SQL that
reads FLevelComponent / inventory / tags lives in admin-publish.sh, and this
module only does the arithmetic on the values those reads return. admin-http.py
imports it to enrich the char-xp endpoint; Phase 3 writes reuse the same math.

All constants are loaded once from data/admin/{skill-xp-per-level,keystones}.json
so the 201 XP thresholds and 205 keystones have a single source of truth.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Iterable

# --------------------------------------------------------------------------
# Data loading. Resolve the data dir from DUNE_PROGRESSION_DATA, else
# DUNE_BASE_DIR/data/admin (matches admin-lookup.py), else relative to this
# file (scripts/ -> ../data/admin). Loaded once at import.
# --------------------------------------------------------------------------
def _data_dir() -> pathlib.Path:
    override = os.environ.get("DUNE_PROGRESSION_DATA")
    if override:
        return pathlib.Path(override)
    base = os.environ.get("DUNE_BASE_DIR")
    if base:
        return pathlib.Path(base) / "data" / "admin"
    return pathlib.Path(__file__).resolve().parent.parent / "data" / "admin"


def _load(name: str) -> dict:
    with open(_data_dir() / name, encoding="utf-8") as fh:
        return json.load(fh)


_XP = _load("skill-xp-per-level.json")
_KEYSTONES = _load("keystones.json")
_FACTIONS = _load("factions.json")

CUMULATIVE_XP: tuple[int, ...] = tuple(_XP["cumulativeXPByLevel"])
MAX_CHAR_XP: int = int(_XP["maxCharXP"])
MAX_LEVEL: int = int(_XP.get("maxLevel", len(CUMULATIVE_XP) - 1))
_INTEL = _XP["intelCurve"]
_INTEL_SEGMENTS: tuple[dict, ...] = tuple(_INTEL["segments"])
_INTEL_CAP_LEVEL: int = int(_INTEL["capLevel"])
_INTEL_CAP_VALUE: int = int(_INTEL["capValue"])

# keystone_id -> skill-point bonus (mirrors dune-admin keystoneSPBonus).
_KEYSTONE_SP: dict[int, int] = {
    int(k["id"]): int(k["spBonus"]) for k in _KEYSTONES["keystones"]
}
_ALL_KEYSTONE_IDS: tuple[int, ...] = tuple(sorted(_KEYSTONE_SP))

# Faction reputation tiers (great houses share one threshold table).
FACTION_REP_CAP: int = int(_FACTIONS["repCap"])
_FACTION_TIER_THRESHOLDS: tuple[int, ...] = tuple(_FACTIONS["tierThresholds"])
_FACTION_MAX_TIER: int = len(_FACTION_TIER_THRESHOLDS) - 1  # 20
_FACTION_TIER_NAMES: dict[int, str] = {int(k): v for k, v in _FACTIONS["tierNames"].items()}
_FACTION_TIER20_NAMES: dict[int, str] = {int(k): v for k, v in _FACTIONS["tier20Names"].items()}
_FACTION_NAMES: dict[int, str] = {int(k): v for k, v in _FACTIONS["factions"].items()}


# --------------------------------------------------------------------------
# XP -> level. Binary search over cumulativeXPByLevel, ported verbatim from
# dune-admin xpToLevel: 0/negative XP -> level 0; any positive XP floors to
# at least level 1; clamps to MAX_LEVEL (200).
# --------------------------------------------------------------------------
def xp_to_level(xp: int) -> int:
    if xp <= 0:
        return 0
    lo, hi = 1, MAX_LEVEL
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if CUMULATIVE_XP[mid] <= xp:
            lo = mid
        else:
            hi = mid - 1
    return lo


# --------------------------------------------------------------------------
# Cumulative intel points earned through a given level. Data-driven walk over
# the piecewise IntelPointsRewarded curve (dune-admin intelAtLevel).
# --------------------------------------------------------------------------
def intel_at_level(level: int) -> int:
    if level <= 0:
        return 0
    if level > _INTEL_CAP_LEVEL:
        return _INTEL_CAP_VALUE
    for seg in _INTEL_SEGMENTS:
        if level <= int(seg["upTo"]):
            return int(seg["base"]) + (level - int(seg["perLevelFrom"])) * int(seg["perLevel"])
    return _INTEL_CAP_VALUE


# --------------------------------------------------------------------------
# Keystones.
# --------------------------------------------------------------------------
def all_keystone_ids() -> list[int]:
    """The full keystone id set (1-205)."""
    return list(_ALL_KEYSTONE_IDS)


def keystone_sp_bonus(ids: Iterable[int]) -> int:
    """Total extra skill points granted by a set of keystone ids. Unknown
    ids are ignored (mirrors dune-admin keystoneSPBonus)."""
    return sum(_KEYSTONE_SP.get(int(i), 0) for i in ids)


def grant_all_keystone_targets(xp: int, spent_sp: int) -> tuple[int, int, int]:
    """Returns (expected_total_sp, expected_unspent_sp, keystone_bonus) for a
    grant-all-keystones operation, ported from dune-admin grantAllKeystoneTargets.
    The starter job always occupies 1 SP, hence the -1. Unspent never < 0."""
    keystone_bonus = keystone_sp_bonus(_ALL_KEYSTONE_IDS)
    level = xp_to_level(xp)
    expected_total = level + keystone_bonus
    expected_unspent = max(0, expected_total - spent_sp - 1)
    return expected_total, expected_unspent, keystone_bonus


# --------------------------------------------------------------------------
# Faction reputation. Ported from dune-admin db.go: factionRepCap,
# factionTierThresholds[21], repToTier, factionTierName, factionDisplayName.
# Reputation/rank keys on the player-CONTROLLER actor; the live SQL (proc call
# + m_FactionDataArray jsonb rebuild) lives in admin-publish.sh, this module
# only does the arithmetic + naming on the values it reads/writes.
# --------------------------------------------------------------------------
def clamp_faction_rep(rep: int) -> int:
    """Clamp reputation to [0, FACTION_REP_CAP] (tier-20 threshold)."""
    if rep < 0:
        return 0
    if rep > FACTION_REP_CAP:
        return FACTION_REP_CAP
    return rep


def rep_to_tier(rep: int) -> int:
    """Highest tier i in 0..20 whose cumulative threshold the rep has reached
    (mirrors dune-admin repToTier; thresholds are monotonic, saturates at 20)."""
    tier = 0
    for i in range(1, _FACTION_MAX_TIER + 1):
        if rep >= _FACTION_TIER_THRESHOLDS[i]:
            tier = i
        else:
            break
    return tier


def faction_display_name(faction_id: int) -> str:
    """House display name; unknown ids fall through to 'Faction<id>'."""
    return _FACTION_NAMES.get(int(faction_id), f"Faction{int(faction_id)}")


def faction_tier_name(faction_id: int, tier: int) -> str:
    """Tier rank name: the named map for 0..5, faction-specific names at tier 20
    (Atreides=Envoy, Harkonnen=Enforcer), else 'Tier <n>' (mirrors dune-admin
    factionTierName)."""
    if tier == _FACTION_MAX_TIER and int(faction_id) in _FACTION_TIER20_NAMES:
        return _FACTION_TIER20_NAMES[int(faction_id)]
    if tier in _FACTION_TIER_NAMES:
        return _FACTION_TIER_NAMES[tier]
    return f"Tier {tier}"


def faction_rep_outcome(current_rep: int, delta: int, faction_id: int) -> dict:
    """Apply a signed reputation delta on the canonical give-faction-rep path
    (dune-admin applyFactionRepDelta): new_rep = clamp(current + delta) to
    [0, cap], then derive tier + tier name. `capped` flags that the clamp moved
    the value (under 0 or over cap). Pure — the caller writes new_rep via the
    proc and rebuilds the jsonb component."""
    raw = current_rep + delta
    new_rep = clamp_faction_rep(raw)
    tier = rep_to_tier(new_rep)
    return {
        "new_rep": new_rep,
        "tier": tier,
        "tier_name": faction_tier_name(faction_id, tier),
        "faction_name": faction_display_name(faction_id),
        "capped": new_rep != raw,
    }


# --------------------------------------------------------------------------
# Character-XP summary used to enrich the read-only char-xp endpoint.
# --------------------------------------------------------------------------
def char_xp_summary(total_xp: int) -> dict:
    level = xp_to_level(total_xp)
    return {
        "xp": total_xp,
        "level": level,
        "intel": intel_at_level(level),
        "maxXP": MAX_CHAR_XP,
        "maxLevel": MAX_LEVEL,
        "atCap": total_xp >= MAX_CHAR_XP,
    }


# --------------------------------------------------------------------------
# award-char-xp outcome (Phase 3). Ported from dune-admin computeAwardCharXP
# Outcome: new XP = clamp(current + amount, 0, maxCharXP); skill points are
# re-derived from the new level + the player's keystone bonus (starter job
# always occupies 1 SP, excluded from spent_sp). Pure — the caller reads
# current_xp/spent_sp + the keystone bonus from the DB and writes the result
# back via jsonb_set. (We clamp to >=0; dune-admin only caps the upper bound.)
# --------------------------------------------------------------------------
def award_char_xp_outcome(current_xp: int, spent_sp: int,
                          keystone_bonus: int, amount: int) -> dict:
    new_xp = current_xp + amount
    if new_xp > MAX_CHAR_XP:
        new_xp = MAX_CHAR_XP
    if new_xp < 0:
        new_xp = 0
    level = xp_to_level(new_xp)
    total_sp = level + keystone_bonus
    unspent_sp = max(0, total_sp - spent_sp - 1)
    return {
        "new_xp": new_xp,
        "new_level": level,
        "new_total_sp": total_sp,
        "new_unspent_sp": unspent_sp,
        "new_intel": intel_at_level(level),
        "capped": new_xp == MAX_CHAR_XP,
    }
