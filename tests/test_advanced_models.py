import unittest

from advanced_models import build_elo_ratings, elo_expected, monte_carlo_scoreline, pearson_correlation


class AdvancedModelTests(unittest.TestCase):
    def test_elo_is_chronological_and_excludes_forecast_date(self):
        ratings = build_elo_ratings([
            {"date": "2026-01-01", "home": "A", "away": "B", "home_goals": 2, "away_goals": 0},
            {"date": "2026-01-02", "home": "B", "away": "A", "home_goals": 0, "away_goals": 1},
            {"date": "2026-01-03", "home": "A", "away": "B", "home_goals": 0, "away_goals": 5},
        ], before_date="2026-01-03")
        self.assertGreater(ratings["A"], ratings["B"])
        self.assertGreater(elo_expected(ratings["A"], ratings["B"]), 0.5)

    def test_monte_carlo_probabilities_are_bounded_and_reproducible(self):
        a = monte_carlo_scoreline(1.4, 0.9, simulations=5000, seed=42)
        b = monte_carlo_scoreline(1.4, 0.9, simulations=5000, seed=42)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a["home"] + a["draw"] + a["away"], 1.0)
        self.assertTrue(all(0 <= a[k] <= 1 for k in ("over_1_5", "over_2_5", "btts")))

    def test_correlation_requires_paired_observations(self):
        self.assertIsNone(pearson_correlation([1, 2], [1, 2]))
        self.assertAlmostEqual(pearson_correlation([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]), 1.0)


if __name__ == "__main__":
    unittest.main()
