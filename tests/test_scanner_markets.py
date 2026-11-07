import unittest
from bot import _odds_market_key, _selection_probability_key, _market_probability


class ScannerMarketTests(unittest.TestCase):
    def test_supported_markets(self):
        self.assertEqual(_odds_market_key("1X2"), "1x2")
        self.assertEqual(_odds_market_key("Total Goals Over Under"), "goals")

    def test_unsupported_and_disallowed_markets(self):
        for market in ("Both Teams To Score", "BTTS", "Correct Score", "Double Chance", "Handicap"):
            self.assertIsNone(_odds_market_key(market))

    def test_1x2_selection_mapping(self):
        self.assertEqual(_selection_probability_key("1x2", "1"), "home")
        self.assertEqual(_selection_probability_key("1x2", "X"), "draw")
        self.assertEqual(_selection_probability_key("1x2", "2"), "away")

    def test_over_goals_mapping_only(self):
        self.assertEqual(_selection_probability_key("goals", "Over 1.5"), "over15")
        self.assertEqual(_selection_probability_key("goals", "Over 2.5"), "over25")
        self.assertIsNone(_selection_probability_key("goals", "Under 2.5"))

    def test_probability_reads_model_output(self):
        model = {"home_win": .5, "draw": .25, "away_win": .25, "over_15": .8, "over_25": .6}
        self.assertEqual(_market_probability(model, "over15"), .8)


if __name__ == "__main__":
    unittest.main()
