"""Run from the repo root:  python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import slippage as s  # noqa: E402

TOKEN = "0xabc"


def pair(liq, price=1.0, labels=None, base=TOKEN):
    p = {"baseToken": {"address": base}, "priceUsd": str(price), "liquidity": {"usd": liq}}
    if labels:
        p["labels"] = labels
    return p


class ToyPool(unittest.TestCase):
    """The worked example from the field guide: 1,000 tokens and $1,000, fees ignored."""

    def test_values_match_the_field_guide(self):
        for sold, expected in [(100, 90.91), (1000, 500.00), (10000, 909.09)]:
            got = s.simulate_sale([(1000, 1000)], sold, fee=0)
            self.assertAlmostEqual(got, expected, places=2)

    def test_never_drains_the_pool(self):
        self.assertLess(s.simulate_sale([(1000, 1000)], 1e9, fee=0), 1000)

    def test_fee_reduces_proceeds(self):
        self.assertLess(s.simulate_sale([(1000, 1000)], 100, fee=0.003),
                        s.simulate_sale([(1000, 1000)], 100, fee=0))

    def test_splitting_across_pools_pays_more(self):
        one = s.simulate_sale([(1000, 1000)], 500, fee=0)
        two = s.simulate_sale([(1000, 1000), (1000, 1000)], 500, fee=0)
        self.assertGreater(two, one)

    def test_input_not_modified(self):
        pools = [(1000, 1000)]
        s.simulate_sale(pools, 100)
        self.assertEqual(pools, [(1000, 1000)])


class Estimate(unittest.TestCase):
    def test_basic_estimate_and_impact(self):
        # liquidity $2,000 at price $1 -> reserves 1,000 tokens and $1,000
        r = s.estimate_sale([pair(2000)], TOKEN, 100, fee=0)
        self.assertAlmostEqual(r["est_proceeds_usd"], 90.91, places=2)
        self.assertAlmostEqual(r["impact_pct"], 9.09, places=2)
        self.assertAlmostEqual(r["coverage_pct"], 100.0)

    def test_tiny_pool_caps_proceeds_like_the_ceiling(self):
        # $2,450 pool: you can never get more than about half of it
        r = s.estimate_sale([pair(2450, price=0.00003)], TOKEN, 1e10, fee=0)
        self.assertLess(r["est_proceeds_usd"], 1225)
        self.assertGreater(r["est_proceeds_usd"], 1200)

    def test_concentrated_pools_are_excluded_and_reported(self):
        pairs = [pair(1000), pair(3000, labels=["v3"])]
        r = s.estimate_sale(pairs, TOKEN, 10, fee=0)
        self.assertAlmostEqual(r["coverage_pct"], 25.0)
        self.assertIsNotNone(r["est_proceeds_usd"])

    def test_only_concentrated_pools_gives_no_estimate(self):
        r = s.estimate_sale([pair(5000, labels=["V3"])], TOKEN, 10)
        self.assertIsNone(r["est_proceeds_usd"])

    def test_token_only_as_quote_is_ignored(self):
        r = s.estimate_sale([pair(5000, base="0xother")], TOKEN, 10)
        self.assertIsNone(r["est_proceeds_usd"])

    def test_bad_data_is_skipped_not_fatal(self):
        bad = {"baseToken": {"address": TOKEN}, "priceUsd": None, "liquidity": {"usd": "oops"}}
        r = s.estimate_sale([bad, pair(2000)], TOKEN, 100, fee=0)
        self.assertAlmostEqual(r["est_proceeds_usd"], 90.91, places=2)

    def test_no_amount_or_no_pairs(self):
        self.assertIsNone(s.estimate_sale([pair(2000)], TOKEN, None)["est_proceeds_usd"])
        self.assertIsNone(s.estimate_sale([pair(2000)], TOKEN, 0)["est_proceeds_usd"])
        self.assertIsNone(s.estimate_sale([], TOKEN, 100)["est_proceeds_usd"])


if __name__ == "__main__":
    unittest.main()
