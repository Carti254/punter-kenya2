import unittest
from unittest.mock import Mock

from api_football_provider import ApiFootballClient, ProviderError


class ApiFootballProviderTests(unittest.TestCase):
    def client(self, fixtures):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [
            Mock(status_code=200, json=lambda: {"response": fixtures, "errors": []}),
            Mock(status_code=200, json=lambda: {"response": [], "errors": []}),
            Mock(status_code=204, json=lambda: {}),
            Mock(status_code=200, json=lambda: {"response": [], "errors": []}),
            Mock(status_code=200, json=lambda: {"response": [], "errors": []}),
        ]
        return ApiFootballClient("test-key", session), session

    def fixture(self, home="Arsenal", away="Chelsea"):
        return {
            "fixture": {"id": 123, "status": {"short": "FT"}},
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "goals": {"home": 2, "away": 1},
            "score": {"halftime": {"home": 1, "away": 0}},
        }

    def test_exact_fixture_match_and_evidence_coverage(self):
        client, session = self.client([self.fixture()])
        evidence = client.match_evidence("2026-11-08", "Arsenal", "Chelsea")
        self.assertEqual(evidence["provider_fixture_id"], 123)
        self.assertEqual(evidence["full_time"], {"home": 2, "away": 1})
        self.assertEqual(evidence["half_time"], {"home": 1, "away": 0})
        self.assertFalse(evidence["coverage"]["team_statistics"])
        self.assertEqual(session.headers["x-apisports-key"], "test-key")

    def test_ambiguous_or_missing_fixture_is_rejected(self):
        client, _ = self.client([self.fixture(), self.fixture()])
        with self.assertRaises(ProviderError):
            client.find_fixture("2026-11-08", "Arsenal", "Chelsea")

    def test_missing_api_key_is_rejected(self):
        with self.assertRaises(ProviderError):
            ApiFootballClient(api_key="")


if __name__ == "__main__":
    unittest.main()
