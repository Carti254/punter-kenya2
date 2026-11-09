import unittest

from dixon_coles_engine import (
    outcome_probabilities,
    score_matrix,
    selected_outcome_probability,
)


class DixonColesEngineTests(unittest.TestCase):
    def test_score_matrix_is_normalized(self):
        matrix = score_matrix(1.45, 1.10)
        self.assertAlmostEqual(sum(map(sum, matrix)), 1.0, places=10)

    def test_outcomes_sum_to_one(self):
        probs = outcome_probabilities(1.45, 1.10)
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=10)
        self.assertTrue(all(0 < value < 1 for value in probs.values()))

    def test_home_advantage_is_reflected(self):
        probs = outcome_probabilities(1.8, 0.9)
        self.assertGreater(probs["HOME"], probs["AWAY"])

    def test_selection_resolves_exact_team_or_draw(self):
        self.assertIsNotNone(selected_outcome_probability(1.5, 1.0, "Arsenal", "Arsenal", "Chelsea"))
        self.assertIsNotNone(selected_outcome_probability(1.5, 1.0, "Draw", "Arsenal", "Chelsea"))
        self.assertIsNone(selected_outcome_probability(1.5, 1.0, "Over 1.5", "Arsenal", "Chelsea"))

    def test_invalid_rates_are_rejected(self):
        with self.assertRaises(ValueError):
            score_matrix(0, 1.0)


if __name__ == "__main__":
    unittest.main()
