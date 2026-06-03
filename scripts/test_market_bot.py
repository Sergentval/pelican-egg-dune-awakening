"""Unit tests for the 7b-3 gamble-buy decision logic (admin_market.gamble_pass +
select_buys). Pure functions only — the DB/subprocess paths (read_buy_candidates,
run_buy_tick, market-bot-buy) are exercised live against the dev server, not here.

The d-die roll is injected via roll_fn so selection is deterministic in tests.
"""
import unittest

from admin_market import compute_price, gamble_pass, select_buys, validate_config_patch


# A tiny catalog. Prices come from compute_price; we pick templates with known
# vendor prices so the price gate math is predictable.
#   cheap : vendor 100 common -> base 100 -> price 100 at grade 0
#   pricey: vendor 1000 common -> base 1000 -> price 1000 at grade 0
CATALOG = {
    "cheap": {"vendor_price": 100, "rarity": "common", "tier": 1, "stack_max": 1, "tradeable": True},
    "pricey": {"vendor_price": 1000, "rarity": "common", "tier": 1, "stack_max": 1, "tradeable": True},
}
ALWAYS = lambda _die: 1   # roll always passes (1 <= target)
NEVER = lambda die: die   # roll always == die, fails for target < die


def cand(order_id, template, price, grade=0):
    return {"order_id": order_id, "template": template, "price": price, "grade": grade}


class TestGamblePass(unittest.TestCase):
    def test_in_band(self):
        self.assertTrue(gamble_pass(1, 5))
        self.assertTrue(gamble_pass(5, 5))

    def test_out_of_band(self):
        self.assertFalse(gamble_pass(6, 5))
        self.assertFalse(gamble_pass(12, 5))

    def test_target_zero_disables(self):
        self.assertFalse(gamble_pass(1, 0))
        self.assertFalse(gamble_pass(0, 0))


class TestSelectBuys(unittest.TestCase):
    def test_reference_price_anchors_the_gate(self):
        # Sanity: our reference prices are what we expect, so the gate tests below
        # are meaningful.
        self.assertEqual(compute_price(CATALOG["cheap"], 0), 100)
        self.assertEqual(compute_price(CATALOG["pricey"], 0), 1000)

    def test_buys_at_or_below_threshold(self):
        # price 105 vs ref 100 * 1.05 = 105 -> exactly at the cap, buy.
        chosen = select_buys([cand(1, "cheap", 105)], CATALOG,
                             threshold=1.05, target=5, max_buys=5, roll_fn=ALWAYS)
        self.assertEqual(chosen, [1])

    def test_skips_above_threshold(self):
        # price 106 vs 105 cap -> skip.
        chosen = select_buys([cand(1, "cheap", 106)], CATALOG,
                             threshold=1.05, target=5, max_buys=5, roll_fn=ALWAYS)
        self.assertEqual(chosen, [])

    def test_gamble_blocks_even_cheap_orders(self):
        chosen = select_buys([cand(1, "cheap", 50)], CATALOG,
                             threshold=1.05, die=12, target=5, max_buys=5, roll_fn=NEVER)
        self.assertEqual(chosen, [])

    def test_unknown_template_skipped(self):
        chosen = select_buys([cand(1, "ghost", 1)], CATALOG,
                             threshold=1.05, target=5, max_buys=5, roll_fn=ALWAYS)
        self.assertEqual(chosen, [])

    def test_disabled_template_skipped(self):
        chosen = select_buys([cand(1, "cheap", 1)], CATALOG, disabled={"cheap"},
                             threshold=1.05, target=5, max_buys=5, roll_fn=ALWAYS)
        self.assertEqual(chosen, [])

    def test_max_buys_caps_committed(self):
        cands = [cand(i, "cheap", 50) for i in range(1, 11)]
        chosen = select_buys(cands, CATALOG, threshold=1.05, target=5,
                             max_buys=3, roll_fn=ALWAYS)
        self.assertEqual(chosen, [1, 2, 3])

    def test_grade_adjusts_reference_price(self):
        # pricey at grade 1 -> 1000*1.25=1250 -> step-rounds to 1300 ref; a 1300
        # listing is within 1300*1.05=1365 -> buy. Same listing judged at grade 0
        # (ref 1000, cap 1050) would be skipped.
        self.assertEqual(compute_price(CATALOG["pricey"], 1), 1300)
        buy = select_buys([cand(1, "pricey", 1300, grade=1)], CATALOG,
                          threshold=1.05, target=5, max_buys=5, roll_fn=ALWAYS)
        skip = select_buys([cand(1, "pricey", 1300, grade=0)], CATALOG,
                           threshold=1.05, target=5, max_buys=5, roll_fn=ALWAYS)
        self.assertEqual((buy, skip), ([1], []))

    def test_threshold_zero_via_gate_is_caller_concern(self):
        # select_buys itself doesn't read enabled/buy_threshold<=0 — that's
        # run_buy_tick's job. With target<=0 the gamble blocks everything.
        chosen = select_buys([cand(1, "cheap", 1)], CATALOG,
                             threshold=1.05, target=0, max_buys=5, roll_fn=ALWAYS)
        self.assertEqual(chosen, [])


class TestValidateConfigPatch(unittest.TestCase):
    def test_coerces_known_fields(self):
        clean, errors = validate_config_patch(
            {"enabled": True, "max_listings": 150, "buy_threshold": 1.1, "unknown": 9})
        self.assertEqual(errors, [])
        self.assertEqual(clean, {"enabled": True, "max_listings": 150, "buy_threshold": 1.1})

    def test_enabled_must_be_bool(self):
        _, errors = validate_config_patch({"enabled": "yes"})
        self.assertTrue(any("enabled" in e for e in errors))

    def test_range_enforced(self):
        _, errors = validate_config_patch({"scan_interval_secs": 5})  # below min 30
        self.assertTrue(any("scan_interval_secs" in e for e in errors))

    def test_cross_field_within_patch(self):
        _, errors = validate_config_patch({"gamble_die": 6, "gamble_target": 7})
        self.assertTrue(any("gamble_target" in e for e in errors))

    def test_cross_field_against_current(self):
        # Patching only the target must be checked against the existing die.
        _, errors = validate_config_patch({"gamble_target": 99}, current={"gamble_die": 12})
        self.assertTrue(any("gamble_target" in e for e in errors))

    def test_cross_field_ok_against_current(self):
        clean, errors = validate_config_patch({"gamble_target": 4}, current={"gamble_die": 12})
        self.assertEqual(errors, [])
        self.assertEqual(clean, {"gamble_target": 4})


if __name__ == "__main__":
    unittest.main()
