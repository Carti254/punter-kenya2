import unittest
from unittest.mock import Mock

from football_stats_provider import FootballStatsError, fetch_match_market_data


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))


class FootballStatsProviderTests(unittest.TestCase):
    def test_exact_fixture_fetches_full_half_player_and_event_data(self):
        fixture = {
            "fixture": {"id": 987, "status": {"short": "FT"}},
            "teams": {"home": {"name": "Manchester United"}, "away": {"name": "Chelsea"}},
            "score": {"halftime": {"home": 1, "away": 0}},
        }
        session = FakeSession([
            {"response": [fixture], "errors": []},
            {"response": [{"team": {"name": "Manchester United"}, "statistics": []}], "errors": []},
            {"response": [{"team": {"name": "Manchester United"}, "statistics": []}], "errors": []},
            {"response": [{"team": {"name": "Manchester United"}, "players": []}], "errors": []},
            {"response": [], "errors": []},
        ])
        result = fetch_match_market_data("Man Utd", "Chelsea", "2026-11-08", "test-key", session)
        self.assertEqual(result["fixture_id"], 987)
        self.assertEqual(len(result["statistics_full_time"]), 1)
        self.assertEqual(len(result["statistics_half_time"]), 1)
        self.assertEqual(len(result["players"]), 1)
        self.assertEqual(len(session.calls), 5)
        self.assertEqual(session.calls[2][1]["params"]["half"], "true")
        self.assertEqual(session.calls[0][1]["headers"]["x-apisports-key"], "test-key")

    def test_ambiguous_fixture_is_rejected_without_guessing(self):
        fixture = {
            "fixture": {"id": 987},
            "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}},
        }
        session = FakeSession([{"response": [fixture, fixture], "errors": []}])
        with self.assertRaisesRegex(FootballStatsError, "found 2"):
            fetch_match_market_data("Arsenal", "Chelsea", "2026-11-08", "test-key", session)
        self.assertEqual(len(session.calls), 1)

    def test_missing_key_fails_closed(self):
        with self.assertRaisesRegex(FootballStatsError, "API_FOOTBALL_KEY"):
            fetch_match_market_data("Arsenal", "Chelsea", "2026-11-08", api_key="", session=Mock())


if __name__ == "__main__":
    unittest.main()
