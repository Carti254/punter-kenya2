import unittest
from unittest.mock import patch
from bookmaker_odds import canonical_team, find_event, _number, _eat_time, fetch_sportybet


class BookmakerOddsTests(unittest.TestCase):
    def test_canonical_team_ignores_common_suffixes(self):
        self.assertEqual(canonical_team("Arsenal FC"), canonical_team("Arsenal"))

    def test_exact_home_away_fixture_matches(self):
        games = [{"home": "Brighton", "away": "Brentford", "markets": []}]
        self.assertEqual(len(find_event(games, "Brighton", "Brentford")), 1)

    def test_reversed_fixture_is_rejected(self):
        games = [{"home": "Brentford", "away": "Brighton", "markets": []}]
        self.assertEqual(find_event(games, "Brighton", "Brentford"), [])

    def test_provider_name_with_extra_club_words_matches(self):
        games = [{"home": "Brighton and Hove Albion", "away": "AFC Wimbledon", "markets": []}]
        self.assertEqual(len(find_event(games, "Brighton", "Wimbledon")), 1)

    def test_ambiguous_fixture_is_rejected(self):
        games = [
            {"home": "Arsenal FC", "away": "Chelsea FC"},
            {"home": "Arsenal", "away": "Chelsea"},
        ]
        self.assertEqual(find_event(games, "Arsenal", "Chelsea"), [])

    def test_invalid_or_non_decimal_price_is_rejected(self):
        self.assertIsNone(_number("suspended"))
        self.assertIsNone(_number(1))
        self.assertEqual(_number("2.15"), 2.15)

    def test_unparseable_time_is_not_fabricated(self):
        self.assertIsNone(_eat_time("not-a-date"))

    @patch("bookmaker_odds._get")
    def test_upcoming_feed_sends_required_market_catalogue(self, mocked_get):
        mocked_get.return_value = {
            "bizCode": 10000,
            "data": {"tournaments": [], "totalNum": 0},
        }
        fetch_sportybet()
        params = mocked_get.call_args.kwargs["params"]
        self.assertEqual(params["sportId"], "sr:sport:1")
        self.assertIn("marketId", params)
        self.assertEqual(params["pageSize"], 100)
        self.assertEqual(params["todayGames"], "false")
        self.assertEqual(params["timeline"], 720)


if __name__ == "__main__":
    unittest.main()
