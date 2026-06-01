#!/usr/bin/env python3
"""Pure give-item planner math. Holds NO database / network / psycopg state:
admin-publish.sh reads the live inventory facts via dune_psql (existing
stacks, used slots/volume, max position, the per-template stack max + volume
learned from existing world items), passes them here as argv, and this module
decides how to split the requested quantity into stack top-ups + new stacks
and whether the inventory has room. The SQL UPDATE/INSERT is then run back in
bash inside a transaction.

Ported from Icehunter/dune-admin cmd/dune-admin/db.go (MIT) — planGiveItemStacks
/ fillExistingStacks / ensureGiveItemSlotCapacity / ensureGiveItemVolumeCapacity
/ maxItemsByVolume / requiredStackCount / formatGiveItemResult /
validateGiveItemInput. We drop dune-admin's item-definition JSON resolvers
(resolveStackMax / resolveItemVolume) because our item catalogue carries no
stack_max/volume; the bash layer supplies stack_max and per-item volume via
dune-admin's secondary DB fallback (MAX(stack_size) / MAX(volume_override)
over existing items of the same template). See ATTRIBUTION.md.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Optional


class StackUpdate(NamedTuple):
    """An existing stack to top up: add `add` to the row with id `stack_id`."""

    stack_id: int
    add: int


class PlannedNewStack(NamedTuple):
    """A brand-new stack row to insert at `position_index` holding `size`."""

    size: int
    position_index: int


class GiveItemPlan(NamedTuple):
    updates: list[StackUpdate]
    new_stacks: list[PlannedNewStack]
    error: Optional[str]


def validate_give_item_input(
    player_id: int, template: str, qty: int
) -> tuple[str, Optional[str]]:
    """Mirror dune-admin validateGiveItemInput: guard order player -> template
    -> qty; return the TrimSpace'd template on success. Messages are pinned by
    the dune-admin test oracle and must match verbatim."""
    if player_id == 0:
        return "", "player ID required"
    trimmed = template.strip()
    if not trimmed:
        return "", "item template required"
    if qty <= 0:
        return "", "quantity must be > 0"
    return trimmed, None


def required_stack_count(qty: int, stack_max: int) -> int:
    """ceil(qty / stack_max) via integer ceiling division."""
    if stack_max < 1:
        stack_max = 1
    return (qty + stack_max - 1) // stack_max


def max_items_by_volume(
    max_volume: float, used_volume: float, per_item_vol: float
) -> int:
    """How many more items fit by volume. Negative available volume (an
    over-full inventory) clamps to 0 before dividing."""
    available = max_volume - used_volume
    if available < 0:
        available = 0.0
    return int(math.floor(available / per_item_vol))


def plan_give_item_stacks(
    qty: int, stack_max: int, stacks: list[tuple[int, int]]
) -> tuple[list[StackUpdate], list[int]]:
    """Split `qty` units across the existing matching `stacks` (list of
    (stack_id, size)) and new stacks. Existing stacks are topped up
    largest-first, but only when stack_max > 1; full/oversized stacks are
    skipped. The leftover becomes new stacks of size min(stack_max, remaining).
    Returns (updates, new_stack_sizes)."""
    if stack_max < 1:
        stack_max = 1
    ordered = sorted(stacks, key=lambda s: s[1], reverse=True)
    remaining = qty
    updates: list[StackUpdate] = []
    if stack_max > 1:
        for stack_id, size in ordered:
            if remaining == 0:
                break
            space = stack_max - size
            if space <= 0:
                continue
            add = space if space < remaining else remaining
            updates.append(StackUpdate(stack_id, add))
            remaining -= add
    new_stacks: list[int] = []
    while remaining > 0:
        size = stack_max if stack_max < remaining else remaining
        new_stacks.append(size)
        remaining -= size
    return updates, new_stacks


def ensure_slot_capacity(
    has_slot_cap: bool, max_slots: int, used_slots: int, new_stack_count: int
) -> Optional[str]:
    """Enforced only when the inventory has a slot cap. Exact-fit is allowed;
    fails only on strict shortfall."""
    if not has_slot_cap:
        return None
    free_slots = max_slots - used_slots
    if free_slots < new_stack_count:
        return f"inventory full: need {new_stack_count} free slots, have {free_slots}"
    return None


def ensure_volume_capacity(
    has_vol_cap: bool,
    max_volume: float,
    used_volume: float,
    per_item_vol: float,
    qty: int,
    template: str,
) -> Optional[str]:
    """Enforced only when the inventory has a volume cap AND the item has a
    positive per-item volume. Zero-volume items are never blocked."""
    if not has_vol_cap:
        return None
    if per_item_vol <= 0:
        return None
    max_by_volume = max_items_by_volume(max_volume, used_volume, per_item_vol)
    if max_by_volume < qty:
        return (
            f"over weight limit: room for {max_by_volume} more {template} "
            f"({used_volume:.2f}/{max_volume:.2f} volume used)"
        )
    return None


def format_give_item_result(
    qty: int, template: str, player: int, topped_up: int, created: int
) -> str:
    """Success message. The glyph between qty and template is U+00D7 (×)."""
    base = f"Added {qty} × {template} to player {player}"
    if topped_up > 0 or created > 0:
        return f"{base} ({topped_up} stack(s) topped up, {created} new stack(s))"
    return base


def build_give_item_plan(
    qty: int,
    stack_max: int,
    template: str,
    stacks: list[tuple[int, int]],
    max_pos: int,
    max_slots: int,
    used_slots: int,
    max_volume: float,
    used_volume: float,
    per_item_vol: float,
) -> GiveItemPlan:
    """End-to-end plan: validate, check volume (dune-admin checks volume BEFORE
    planning), plan the stacks, then check slot capacity against the resulting
    new-stack count, and finally assign position_index values starting at
    max_pos + 1. Any failure returns a GiveItemPlan with `error` set and no
    updates/new_stacks."""
    trimmed, err = validate_give_item_input(1, template, qty)
    if err is not None:
        return GiveItemPlan([], [], err)

    has_vol_cap = max_volume > 0
    has_slot_cap = max_slots > 0

    vol_err = ensure_volume_capacity(
        has_vol_cap, max_volume, used_volume, per_item_vol, qty, trimmed
    )
    if vol_err is not None:
        return GiveItemPlan([], [], vol_err)

    updates, new_sizes = plan_give_item_stacks(qty, stack_max, stacks)

    slot_err = ensure_slot_capacity(has_slot_cap, max_slots, used_slots, len(new_sizes))
    if slot_err is not None:
        return GiveItemPlan([], [], slot_err)

    position = max_pos + 1
    new_stacks: list[PlannedNewStack] = []
    for size in new_sizes:
        new_stacks.append(PlannedNewStack(size, position))
        position += 1

    return GiveItemPlan(updates, new_stacks, None)
