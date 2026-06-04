#!/usr/bin/env python3
"""Pure helpers for the tech-unlock admin write (#6).

"Tech" = the schematics/recipes tree, stored on the PAWN (BP_DunePlayerCharacter)
at properties -> TechKnowledgePlayerComponent -> m_TechKnowledge -> m_TechKnowledgeData,
an array of {ItemKey, bIsNewEntry, UnlockedState}. UnlockedState is "Purchased" or
"NotPurchased". unlock-all flips every DISCOVERED entry to Purchased; lock-all sets
them back to NotPurchased. (It can't add recipes the player never encountered — those
aren't in the array; truly-everything would need the master catalog.)

The jsonb rewrite is SQL in admin-publish.sh (dune_apply_tech_unlock); this module
holds the small testable bits: mode -> (state, bIsNewEntry) and a count summary.
"""
from typing import Iterable

# mode -> the UnlockedState to write + the bIsNewEntry to pair with it. Mirrors the
# natural shapes observed live: Purchased entries carry bIsNewEntry false; freshly
# NotPurchased ones carry true (so a re-lock shows as "new" again, like a reset).
TECH_MODES: dict[str, dict] = {
    "unlock-all": {"state": "Purchased", "bnew": False},
    "lock-all": {"state": "NotPurchased", "bnew": True},
}


def plan_tech(mode: str) -> tuple[str, bool]:
    """Resolve a tech-write mode to (UnlockedState, bIsNewEntry). Raises ValueError
    on an unknown mode. Pure."""
    spec = TECH_MODES.get(mode)
    if spec is None:
        raise ValueError(f"mode must be one of: {', '.join(TECH_MODES)}")
    return spec["state"], spec["bnew"]


def summarize(entries: Iterable) -> tuple[int, int]:
    """(total, unlocked) for an m_TechKnowledgeData array. 'unlocked' = entries whose
    UnlockedState is 'Purchased'. Non-dict/garbage entries count toward total only.
    Pure."""
    total = 0
    unlocked = 0
    for e in entries or []:
        total += 1
        if isinstance(e, dict) and e.get("UnlockedState") == "Purchased":
            unlocked += 1
    return total, unlocked
