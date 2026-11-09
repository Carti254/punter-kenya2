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

    def test_below_target_partial_is_not_returned_as_a_slip(self):
        result = build_accumulator([self.candidates[0]], 5.0, max_legs=2)
        self.assertFalse(result["target_reached"])
        self.assertEqual(result["selections"], [])
        self.assertEqual(result["combined_odds"], 1.0)
        self.assertIn("No lower-odds substitute", result["reason"])

    def test_closest_below_target_candidate_is_not_returned(self):
        candidates = [
            {"home": "Safe", "away": "Choice", "odds": 1.50, "probability": 0.99, "market": "1X2"},
            {"home": "Closer", "away": "Choice", "odds": 2.20, "probability": 0.60, "market": "1X2"},
        ]
        result = build_accumulator(candidates, 2.50, max_legs=2)
        self.assertFalse(result["target_reached"])
        self.assertEqual(result["selections"], [])
        self.assertEqual(result["combined_odds"], 1.0)

    def test_eight_eligible_markets_cannot_fall_back_to_one_1_67_selection(self):
        candidates = []
        for fixture in range(4):
            for market in ("1X2", "Double Chance"):
                candidates.append({
                    "home": f"Home {fixture}", "away": f"Away {fixture}",
                    "odds": 1.67, "probability": 0.644, "market": market,
                })
        result = build_accumulator(candidates, 2.00, max_legs=4)
        self.assertFalse(result["target_reached"])
        self.assertEqual(result["selections"], [])
        self.assertEqual(result["combined_odds"], 1.0)

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

    def test_large_target_is_built_from_multiple_moderate_price_legs(self):
        candidates = [
            {"home": f"Small {i}", "away": f"Opponent {i}", "odds": 2.20, "probability": 0.45, "market": "1X2"}
            for i in range(1, 7)
        ]
        candidates.extend([
            {"home": "Villarreal", "away": "Getafe", "odds": 10.0, "probability": 0.034, "market": "Over 5"},
            {"home": "Freiburg", "away": "Union Berlin", "odds": 5.0, "probability": 0.167, "market": "1X2"},
        ])
        result = build_accumulator(candidates, 50.0, max_legs=8, ranking="odds")
        self.assertTrue(result["target_reached"])
        self.assertGreaterEqual(result["combined_odds"], 50.0)
        self.assertLessEqual(result["combined_odds"], 60.0)
        self.assertGreaterEqual(len(result["selections"]), 5)
        self.assertTrue(all(x["odds"] <= 4.0 for x in result["selections"]))
        self.assertNotIn(10.0, [x["odds"] for x in result["selections"]])
        self.assertNotIn(5.0, [x["odds"] for x in result["selections"]])

    def test_minimum_leg_count_is_respected_even_when_three_legs_reach_target(self):
        candidates = [
            {"home": f"Club {i}", "away": f"Rival {i}", "odds": 2.20, "probability": 0.45, "market": "1X2"}
            for i in range(1, 9)
        ]
        result = build_accumulator(candidates, 50.0, max_legs=8, ranking="odds", min_legs=5)
        self.assertTrue(result["target_reached"])
        self.assertGreaterEqual(len(result["selections"]), 5)
        self.assertAlmostEqual(
            result["combined_probability"],
            __import__("math").prod(x["probability"] for x in result["selections"]),
        )

    def test_engine_can_build_more_than_20_legs_up_to_50(self):
        candidates = [
            {"home": f"Home {i}", "away": f"Away {i}", "odds": 1.15,
             "probability": 0.92, "market": "1X2"}
            for i in range(1, 31)
        ]
        result = build_accumulator(candidates, 50.0, max_legs=50, ranking="value")
        self.assertTrue(result["target_reached"])
        self.assertGreater(len(result["selections"]), 20)
        self.assertLessEqual(len(result["selections"]), 50)

    def test_minimum_legs_cannot_exceed_maximum(self):
        with self.assertRaises(ValueError):
            build_accumulator(self.candidates, 2.0, max_legs=3, min_legs=4)

    def test_permanent_market_exclusions_are_enforced(self):
        candidates = [
            {"home": "A", "away": "B", "odds": 1.8, "probability": 0.8, "market": "Both Teams To Score", "selection": "Yes"},
            {"home": "C", "away": "D", "odds": 1.8, "probability": 0.8, "market": "Total Goals", "selection": "Under 2.5"},
            {"home": "E", "away": "F", "odds": 1.8, "probability": 0.8, "market": "Total Goals", "selection": "Over 1.5"},
        ]
        result = build_accumulator(candidates, 1.8)
        self.assertEqual(len(result["selections"]), 1)
        self.assertEqual(result["selections"][0]["selection"], "Over 1.5")

    def test_same_teams_on_different_dates_are_distinct_fixtures(self):
        candidates = [
            {"home": "A", "away": "B", "day": "2026-11-09", "odds": 1.5, "probability": 0.8},
            {"home": "A", "away": "B", "day": "2026-11-10", "odds": 1.5, "probability": 0.8},
        ]
        result = build_accumulator(candidates, 2.0)
        self.assertTrue(result["target_reached"])
        self.assertEqual(len(result["selections"]), 2)

    def test_same_competition_same_day_gets_dependence_haircut(self):
        candidates = [
            {"home": "A", "away": "B", "day": "2026-11-09", "league": "League X",
             "odds": 2.0, "probability": 0.60, "market": "1X2"},
            {"home": "C", "away": "D", "day": "2026-11-09", "league": "League X",
             "odds": 2.0, "probability": 0.60, "market": "1X2"},
        ]
        result = build_accumulator(candidates, 4.0, max_legs=2)
        self.assertEqual(len(result["selections"]), 2)
        self.assertAlmostEqual(result["naive_independent_probability"], 0.36)
        self.assertAlmostEqual(result["combined_probability"], 0.36 * 0.97)
        self.assertAlmostEqual(result["correlation_adjustment_factor"], 0.97)

    def test_ranking_is_validated(self):
        with self.assertRaises(ValueError):
            build_accumulator(self.candidates, 2.0, ranking="random")


if __name__ == "__main__":
    unittest.main()
