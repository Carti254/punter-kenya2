import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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


    def test_scanner_excludes_btts_and_under_25_from_slip_candidates(self):
        today_eat = datetime.now(timezone.utc).astimezone(ZoneInfo("Africa/Nairobi")).date().isoformat()
        game = {
            "bookmaker": "SportyBet", "kickoff_eat": f"{today_eat} 23:00",
            "event_id": "event-1", "home": "Brighton", "away": "Brentford",
            "competition": "Test League",
            "markets": [
                {"market": "Both Teams To Score", "selection": "Yes", "odds": 1.95,
                 "market_id": "btts", "outcome_id": "yes"},
                {"market": "Total Goals Over/Under", "selection": "Under 2.5", "odds": 2.10,
                 "market_id": "totals", "outcome_id": "under25", "specifier": "total=2.5"},
                {"market": "Total Goals Over/Under", "selection": "Over 1.5", "odds": 1.35,
                 "market_id": "totals", "outcome_id": "over15", "specifier": "total=1.5"},
            ],
        }
        with patch("bot.fetch_sportybet_odds", return_value=[game]):
            rows, _ = scan_value_candidates(("today",), "all")
        labels = {(row["market"], row["selection"]) for row in rows}
        self.assertNotIn(("Both Teams To Score", "Yes"), labels)
        self.assertNotIn(("Total Goals Over/Under", "Under 2.5"), labels)
        self.assertIn(("Total Goals Over/Under", "Over 1.5"), labels)



if __name__ == "__main__":
    unittest.main()
