import unittest

from premium_intelligence import (
    assess_price, kelly_fraction, market_probabilities, performance_summary,
    remove_margin, rollover_projection, score_matrix, stake_advice,
)


class PremiumIntelligenceTests(unittest.TestCase):
    def test_score_matrix_probability_mass_and_tail(self):
        matrix, tail = score_matrix(1.4, 1.1, 12)
        self.assertAlmostEqual(sum(map(sum, matrix)) + tail, 1.0, places=10)
        self.assertGreaterEqual(tail, 0)

    def test_market_probabilities_are_bounded(self):
        markets = market_probabilities(1.5, 1.0)
        for key, value in markets.items():
            if not key.startswith("_"):
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, 1)
        self.assertAlmostEqual(markets["1"] + markets["X"] + markets["2"] + markets["_tail_mass"], 1, places=6)

    def test_price_value(self):
        result = assess_price(0.60, 2.0)
        self.assertAlmostEqual(result["edge"], 0.10)
        self.assertAlmostEqual(result["expected_roi"], 0.20)
        self.assertTrue(result["qualifies"])

    def test_remove_margin(self):
        result = remove_margin([2.0, 2.0])
        self.assertEqual(result["probabilities"], [0.5, 0.5])
        self.assertEqual(result["overround"], 0)

    def test_kelly_is_capped_and_zero_without_edge(self):
        self.assertEqual(kelly_fraction(0.4, 2.0), 0)
        self.assertLessEqual(kelly_fraction(0.8, 2.0), 0.02)

    def test_stake_advice(self):
        result = stake_advice(1000, 0.6, 2.0)
        self.assertLessEqual(result["stake"], 20)
        self.assertGreaterEqual(result["stake"], 0)

    def test_rollover_is_projection_not_prediction(self):
        result = rollover_projection(100, 3, 2)
        self.assertEqual(result["illustrative_gross_if_every_day_won"], 800)
        self.assertIn("not a forecast", result["warning"])

    def test_performance_excludes_pending_and_void(self):
        result = performance_summary([
            {"status": "WON", "stake": 10, "profit_loss": 10},
            {"status": "LOST", "stake": 10, "profit_loss": -10},
            {"status": "PENDING", "stake": 50, "profit_loss": None},
            {"status": "VOID", "stake": 20, "profit_loss": 0},
        ])
        self.assertEqual(result["settled"], 2)
        self.assertEqual(result["roi"], 0)


if __name__ == "__main__":
    unittest.main()
