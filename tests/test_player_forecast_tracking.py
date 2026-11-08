import unittest

from forecast_tracker import forecast_fixture_ids, player_prop_actual, _binary_market_outcome


class PlayerForecastTrackingTests(unittest.TestCase):
    def test_fixture_ids_include_team_and_player_forecasts(self):
        rows = [
            {"fixture_key": "api-football:100"},
            {"fixture_key": "api-football-player:100:9001"},
            {"fixture_key": "api-football-player:101:9002"},
            {"fixture_key": "api-football:100"},
            {"fixture_key": "bad-key"},
        ]
        self.assertEqual(forecast_fixture_ids(rows), [100, 101])

    def test_player_metrics_are_read_from_provider_fields(self):
        stats = {
            "shots": {"total": 4, "on": 2},
            "goals": {"total": 1, "assists": 1},
            "cards": {"yellow": 1, "red": 0},
        }
        self.assertEqual(player_prop_actual(stats, "player_shots"), 4)
        self.assertEqual(player_prop_actual(stats, "player_shots_on_target"), 2)
        self.assertEqual(player_prop_actual(stats, "player_goals"), 1)
        self.assertEqual(player_prop_actual(stats, "player_assists"), 1)
        self.assertEqual(player_prop_actual(stats, "player_yellow_cards"), 1)
        self.assertEqual(player_prop_actual(stats, "player_red_cards"), 0)

    def test_missing_or_invalid_player_metrics_are_not_guessed(self):
        self.assertIsNone(player_prop_actual({}, "player_shots"))
        self.assertIsNone(player_prop_actual({"shots": {"total": "N/A"}}, "player_shots"))
        self.assertIsNone(player_prop_actual({"shots": {"total": 3}}, "player_goals"))

    def test_player_over_under_grading_and_push(self):
        self.assertEqual(_binary_market_outcome("player_shots", "Over 2.5", 3), 1)
        self.assertEqual(_binary_market_outcome("player_shots", "Under 2.5", 3), 0)
        self.assertIsNone(_binary_market_outcome("player_shots", "Over 3", 3))


if __name__ == "__main__":
    unittest.main()
