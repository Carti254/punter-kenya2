import unittest

from accumulator_engine import build_accumulator


class AccumulatorEngineTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            {"home": "A", "away": "B", "odds": 2.0, "probability": 0.60, "market": "1X2"},
            {"home": "A", "away": "B", "odds": 1.8, "probability": 0.75, "market": "Over 1.5"},
            {"home": "C", "away": "D", "odds": 1.8, "probability": 0.65, "market": "1X2"},
            {"home": "E", "away": "F", "odds": 1.7, "probability": 0.70, "market": "1X2"},
        ]

    def test_reaches_target_without_duplicate_fixtures(self):
        result = build_accumulator(self.candidates, 3.0, max_legs=4)
        self.assertTrue(result["target_reached"])
        fixtures = [(x["home"], x["away"]) for x in result["selections"]]
        self.assertEqual(len(fixtures), len(set(fixtures)))
        self.assertGreaterEqual(result["combined_odds"], 3.0)

    def test_best_available_is_not_misreported_as_target_reached(self):
        result = build_accumulator([self.candidates[0]], 5.0, max_legs=2)
        self.assertFalse(result["target_reached"])
        self.assertLess(result["combined_odds"], 5.0)

    def test_invalid_candidates_are_ignored(self):
        result = build_accumulator([{"home": "X", "away": "Y", "odds": 1.0, "probability": 0.8}], 2.0)
        self.assertEqual(result["selections"], [])

    def test_single_longshot_does_not_satisfy_small_target(self):
        result = build_accumulator([{"home": "Long", "away": "Shot", "odds": 22.0, "probability": 0.426}], 2.0)
        self.assertFalse(result["target_reached"])
        self.assertEqual(result["selections"], [])

    def test_builds_a_near_target_accumulator_instead_of_forcing_a_longshot(self):
        candidates = [
            {"home": "One", "away": "Two", "odds": 1.50, "probability": 0.78, "market": "1X2"},
            {"home": "Three", "away": "Four", "odds": 1.40, "probability": 0.80, "market": "Over 1.5"},
            {"home": "Five", "away": "Six", "odds": 22.00, "probability": 0.426, "market": "Correct Score"},
        ]
        result = build_accumulator(candidates, 2.00, max_legs=4)
        self.assertTrue(result["target_reached"])
        self.assertGreaterEqual(result["combined_odds"], 2.00)
        self.assertLessEqual(result["combined_odds"], 2.40)
        self.assertEqual(len(result["selections"]), 2)
        self.assertNotIn(22.00, [x["odds"] for x in result["selections"]])

    def test_ranking_is_validated(self):
        with self.assertRaises(ValueError):
            build_accumulator(self.candidates, 2.0, ranking="random")


if __name__ == "__main__":
    unittest.main()
