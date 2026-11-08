import unittest

from advanced_prediction import (
    build_ensemble, dixon_coles_1x2, elo_1x2, monte_carlo_1x2,
)


class AdvancedPredictionTests(unittest.TestCase):
    def test_dixon_coles_probabilities_sum_to_one(self):
        p = dixon_coles_1x2(1.45, 1.05)
        self.assertAlmostEqual(sum(p.values()), 1.0, places=10)
        self.assertTrue(all(0 <= x <= 1 for x in p.values()))

    def test_monte_carlo_is_reproducible(self):
        a = monte_carlo_1x2(1.4, 0.9, simulations=3000, seed=17)
        b = monte_carlo_1x2(1.4, 0.9, simulations=3000, seed=17)
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(a.values()), 1.0, places=10)

    def test_elo_reflects_stronger_team(self):
        history = []
        for i in range(25):
            history.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                            "home": "Strong FC", "away": "Weak FC",
                            "ft_home": 2, "ft_away": 0})
        p = elo_1x2("Strong FC", "Weak FC", history)
        self.assertGreater(p["1"], p["2"])

    def test_ensemble_exposes_components_and_upset_risk(self):
        report = build_ensemble("Home", "Away", 1.5, 1.0, [])
        self.assertAlmostEqual(sum(report["probabilities"].values()), 1.0, places=10)
        self.assertIn("dixon_coles", report["components"])
        self.assertIn("level", report["upset_risk"])

    def test_bad_inputs_rejected(self):
        with self.assertRaises(ValueError):
            dixon_coles_1x2(0, 1)
        with self.assertRaises(ValueError):
            monte_carlo_1x2(1, 1, simulations=100)


if __name__ == "__main__":
    unittest.main()
