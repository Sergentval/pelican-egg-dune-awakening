#!/usr/bin/env python3
"""Unit tests for the give-item planner compute logic in
admin_inventory_plan.py, ported from Icehunter/dune-admin's pure helpers
(planGiveItemStacks, fillExistingStacks, ensureGiveItemSlotCapacity,
ensureGiveItemVolumeCapacity, maxItemsByVolume, requiredStackCount,
formatGiveItemResult, validateGiveItemInput — see
cmd/dune-admin/db.go + db_cmd_give_item_test.go, MIT). The DB INSERT/UPDATE
itself is verified live in-container; here we pin the math.

The oracle (expected values) is taken verbatim from dune-admin's Go unit
tests so the Python port preserves the same invariants.

Run: python3 scripts/test_give_item.py
"""
import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import admin_inventory_plan as ip  # noqa: E402  # type: ignore[import-not-found]


class ValidateGiveItemInput(unittest.TestCase):
    def test_valid_trims_template(self):
        tmpl, err = ip.validate_give_item_input(123, "  Dune.Item  ", 2)
        self.assertEqual(tmpl, "Dune.Item")
        self.assertIsNone(err)

    def test_missing_player(self):
        tmpl, err = ip.validate_give_item_input(0, "x", 1)
        self.assertEqual(tmpl, "")
        self.assertEqual(err, "player ID required")

    def test_missing_template_whitespace_only(self):
        tmpl, err = ip.validate_give_item_input(1, "   ", 1)
        self.assertEqual(tmpl, "")
        self.assertEqual(err, "item template required")

    def test_invalid_qty_zero(self):
        _, err = ip.validate_give_item_input(1, "x", 0)
        self.assertEqual(err, "quantity must be > 0")

    def test_invalid_qty_negative(self):
        _, err = ip.validate_give_item_input(1, "x", -3)
        self.assertEqual(err, "quantity must be > 0")


class PlanGiveItemStacks(unittest.TestCase):
    def test_topup_largest_first_then_new(self):
        # qty=17, stackMax=10, existing [(1,8),(2,10),(3,2)].
        # Sorted desc: id2(10 full -> skipped), id1(8 -> +2), id3(2 -> +8).
        # Consumes 10; remaining 7 -> one new stack of 7.
        updates, new_stacks = ip.plan_give_item_stacks(17, 10, [(1, 8), (2, 10), (3, 2)])
        self.assertEqual(updates, [ip.StackUpdate(1, 2), ip.StackUpdate(3, 8)])
        self.assertEqual(new_stacks, [7])

    def test_no_stacking_when_stackmax_one(self):
        # stackMax==1: top-up skipped entirely; qty new stacks of size 1.
        updates, new_stacks = ip.plan_give_item_stacks(3, 1, [(1, 1)])
        self.assertEqual(updates, [])
        self.assertEqual(new_stacks, [1, 1, 1])

    def test_empty_inventory_single_new_stack(self):
        updates, new_stacks = ip.plan_give_item_stacks(5, 10, [])
        self.assertEqual(updates, [])
        self.assertEqual(new_stacks, [5])

    def test_multiple_full_stacks_plus_partial(self):
        # 25 with max 10 -> [10,10,5]
        updates, new_stacks = ip.plan_give_item_stacks(25, 10, [])
        self.assertEqual(updates, [])
        self.assertEqual(new_stacks, [10, 10, 5])

    def test_exact_fill_no_new_stacks(self):
        # one partial stack of 2, max 10, qty 8 -> tops up to 10, no new.
        updates, new_stacks = ip.plan_give_item_stacks(8, 10, [(5, 2)])
        self.assertEqual(updates, [ip.StackUpdate(5, 8)])
        self.assertEqual(new_stacks, [])

    def test_oversized_stack_skipped(self):
        # an existing stack already above stackMax has negative space -> skipped.
        updates, new_stacks = ip.plan_give_item_stacks(3, 10, [(9, 15)])
        self.assertEqual(updates, [])
        self.assertEqual(new_stacks, [3])

    def test_stackmax_below_one_clamped(self):
        # defensive: stackMax<1 must clamp to 1 (no infinite loop).
        updates, new_stacks = ip.plan_give_item_stacks(2, 0, [])
        self.assertEqual(updates, [])
        self.assertEqual(new_stacks, [1, 1])


class SlotCapacity(unittest.TestCase):
    def test_exact_fit_allowed(self):
        # max5 used3 -> free2; need2 ok.
        self.assertIsNone(ip.ensure_slot_capacity(True, 5, 3, 2))

    def test_shortfall_errors(self):
        msg = ip.ensure_slot_capacity(True, 5, 3, 3)
        self.assertEqual(msg, "inventory full: need 3 free slots, have 2")

    def test_no_cap_skips(self):
        self.assertIsNone(ip.ensure_slot_capacity(False, 5, 3, 99))


class VolumeCapacity(unittest.TestCase):
    def test_max_items_by_volume(self):
        self.assertEqual(ip.max_items_by_volume(100, 40, 15), 4)   # floor(60/15)
        self.assertEqual(ip.max_items_by_volume(100, 140, 10), 0)  # clamp negative

    def test_fits_exactly(self):
        # max10 used4 per2 -> floor(6/2)=3; qty3 ok.
        self.assertIsNone(ip.ensure_volume_capacity(True, 10, 4, 2, 3, "Dune.Item"))

    def test_over_limit_errors(self):
        msg = ip.ensure_volume_capacity(True, 10, 4, 2, 4, "Dune.Item")
        self.assertIsNotNone(msg)
        self.assertIn("over weight limit", msg)
        self.assertIn("Dune.Item", msg)

    def test_no_cap_skips(self):
        self.assertIsNone(ip.ensure_volume_capacity(False, 10, 4, 2, 999, "X"))

    def test_zero_per_item_volume_skips(self):
        self.assertIsNone(ip.ensure_volume_capacity(True, 10, 4, 0, 999, "X"))


class RequiredStackCount(unittest.TestCase):
    def test_ceiling_division(self):
        self.assertEqual(ip.required_stack_count(10, 3), 4)
        self.assertEqual(ip.required_stack_count(1, 1), 1)


class FormatGiveItemResult(unittest.TestCase):
    def test_with_counts(self):
        msg = ip.format_give_item_result(3, "Dune.Item", 42, topped_up=1, created=2)
        self.assertEqual(
            msg,
            "Added 3 × Dune.Item to player 42 (1 stack(s) topped up, 2 new stack(s))",
        )

    def test_without_counts(self):
        msg = ip.format_give_item_result(1, "X", 1, topped_up=0, created=0)
        self.assertEqual(msg, "Added 1 × X to player 1")


class BuildGiveItemPlan(unittest.TestCase):
    """Orchestration (our own layer, not in dune-admin's unit tests):
    volume-before-slot ordering, position_index assignment off max_pos."""

    def _plan(self, **kw):
        base = dict(
            qty=3, stack_max=1, template="Dune.Item", stacks=[], max_pos=-1,
            max_slots=-1, used_slots=0, max_volume=-1, used_volume=0.0, per_item_vol=0.0,
        )
        base.update(kw)
        return ip.build_give_item_plan(**base)

    def test_positions_start_at_max_pos_plus_one(self):
        plan = self._plan(qty=3, stack_max=1, max_pos=2)
        self.assertIsNone(plan.error)
        self.assertEqual([n.position_index for n in plan.new_stacks], [3, 4, 5])
        self.assertEqual([n.size for n in plan.new_stacks], [1, 1, 1])

    def test_empty_inventory_first_pos_zero(self):
        plan = self._plan(qty=2, stack_max=1, max_pos=-1)
        self.assertEqual([n.position_index for n in plan.new_stacks], [0, 1])

    def test_topup_positions(self):
        plan = self._plan(qty=17, stack_max=10, stacks=[(1, 8), (2, 10), (3, 2)], max_pos=4)
        self.assertEqual(plan.updates, [ip.StackUpdate(1, 2), ip.StackUpdate(3, 8)])
        self.assertEqual([(n.size, n.position_index) for n in plan.new_stacks], [(7, 5)])

    def test_volume_checked_before_slot(self):
        # Both caps would fail; volume is checked first so its message wins.
        plan = self._plan(
            qty=5, stack_max=1, max_slots=1, used_slots=1,   # 0 free slots
            max_volume=10, used_volume=9, per_item_vol=2,    # room for 0 more
        )
        self.assertIsNotNone(plan.error)
        self.assertIn("over weight limit", plan.error)

    def test_slot_failure_when_volume_ok(self):
        plan = self._plan(
            qty=3, stack_max=1, max_slots=5, used_slots=4,   # 1 free slot, need 3
            max_volume=-1, used_volume=0, per_item_vol=0,    # no volume cap
        )
        self.assertIsNotNone(plan.error)
        self.assertIn("inventory full", plan.error)

    def test_invalid_qty_surfaces_validation_error(self):
        plan = self._plan(qty=0)
        self.assertEqual(plan.error, "quantity must be > 0")


if __name__ == "__main__":
    unittest.main()
