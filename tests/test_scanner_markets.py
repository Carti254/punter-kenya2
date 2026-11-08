import unittest
from unittest.mock import patch
from bot import (
    _odds_market_key, _selection_probability_key, _market_probability,
    _market_probability_for_row, scan_value_candidates,
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


    def test_scanner_does_not_hide_btts_or_under_25(self):
        raw = {"id": "fixture-1"}
        fixture = {
            "home_team": "Brighton", "away_team": "Brentford",
            "competition": "Premier League", "kickoffUtc": "2026-11-20T20:00:00Z",
        }
        game = {
            "bookmaker": "SportyBet", "kickoff_eat": "2026-11-20 23:00",
            "event_id": "event-1",
            "markets": [
                {"market": "Both Teams To Score", "selection": "Yes", "odds": 1.95,
                 "market_id": "btts", "outcome_id": "yes"},
                {"market": "Total Goals Over/Under", "selection": "Under 2.5", "odds": 2.10,
                 "market_id": "totals", "outcome_id": "under25", "specifier": "total=2.5"},
            ],
        }
        prediction = {
            "available": True, "lambda_home": 1.7, "lambda_away": 1.1,
            "home_win": 0.50, "draw": 0.25, "away_win": 0.25,
        }
        with patch("bot.fetch_openmatchday_fixture_rows", return_value=[raw]), \\
             patch("bot.upcoming_fixtures", return_value=[(raw, fixture)]), \\
             patch("bot.build_openmatchday_prediction", return_value=prediction), \\
             patch("bot.fetch_bookmaker_odds", return_value={
                 "bookmakers": {"SportyBet": {"games": [game]}}, "errors": {}
             }), \\
             patch("bot.find_bookmaker_event", return_value=[game]):
            rows, _ = scan_value_candidates(("today",), "all")
        labels = {(row["market"], row["selection"]) for row in rows}
        self.assertIn(("Both Teams To Score", "Yes"), labels)
        self.assertIn(("Total Goals Over/Under", "Under 2.5"), labels)

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
