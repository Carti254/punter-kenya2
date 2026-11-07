import unittest
from bookmaker_odds import canonical_team, find_event, _number, _eat_time


class BookmakerOddsTests(unittest.TestCase):
    def test_canonical_team_ignores_common_suffixes(self):
        self.assertEqual(canonical_team("Arsenal FC"), canonical_team("Arsenal"))

    def test_exact_home_away_fixture_matches(self):
        games = [{"home": "Brighton", "away": "Brentford", "markets": []}]
        self.assertEqual(len(find_event(games, "Brighton", "Brentford")), 1)

    def test_reversed_fixture_is_rejected(self):
        games = [{"home": "Brentford", "away": "Brighton", "markets": []}]
        self.assertEqual(find_event(games, "Brighton", "Brentford"), [])

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


if __name__ == "__main__":
    unittest.main()
