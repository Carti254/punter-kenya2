import unittest
from bot import (
    _odds_market_key, _selection_probability_key, _market_probability,
    _market_probability_for_row,
)


class ScannerMarketTests(unittest.TestCase):
    def setUp(self):
        self.model = {
            "home_win": .50, "draw": .25, "away_win": .25,
            "over_15": .80, "over_25": .60,
            "lambda_home": 1.7, "lambda_away": 1.1,
        }

    def test_market_catalogue_recognises_market_families(self):
        for market in (
            "1X2", "Total Goals Over Under", "Both Teams To Score",
            "Correct Score", "Double Chance", "Draw No Bet",
            "Asian Handicap", "Home Team Total Goals", "Corners",
            "Cards", "Player Goals", "First Half Result",
        ):
            self.assertIsNotNone(_odds_market_key(market), market)

    def test_selection_normalisation(self):
        self.assertEqual(_selection_probability_key("1x2", "1"), "1")
        self.assertEqual(_selection_probability_key("goals", "Over 1.5"), "over 1.5")

    def test_legacy_model_probability_lookup(self):
        self.assertEqual(_market_probability(self.model, "over15"), .80)

    def test_scoreline_derived_markets(self):
        cases = [
            ({"market": "1X2", "selection": "1"}, .50),
            ({"market": "Double Chance", "selection": "1X"}, .75),
            ({"market": "Both Teams To Score", "selection": "Yes"}, "positive"),
            ({"market": "Total Goals Over Under", "selection": "Over 1.5"}, "positive"),
            ({"market": "Correct Score", "selection": "1:0"}, "positive"),
        ]
        for row, expected in cases:
            probability, reason = _market_probability_for_row(self.model, row)
            if expected == "positive":
                self.assertIsNotNone(probability)
                self.assertGreater(probability, 0)
            else:
                self.assertAlmostEqual(probability, expected, places=2)


if __name__ == "__main__":
    unittest.main()
