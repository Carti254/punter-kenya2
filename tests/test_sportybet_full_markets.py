import unittest
from datetime import datetime, timedelta, timezone

from unittest.mock import patch

from bookmaker_odds import EAT, fetch_sportybet


def _index_payload():
    kickoff = datetime.now(EAT) + timedelta(hours=3)
    kickoff_ms = int(kickoff.astimezone(timezone.utc).timestamp() * 1000)
    payload = {
        "bizCode": 10000,
        "data": {
            "totalNum": 1,
            "tournaments": [{
                "name": "Test League",
                "events": [{
                    "eventId": "sr:match:test-1",
                    "homeTeamName": "Home FC",
                    "awayTeamName": "Away FC",
                    "estimateStartTime": kickoff_ms,
                    "markets": [{
                        "id": "1",
                        "desc": "Match Result",
                        "status": 0,
                        "outcomes": [{"id": "1", "desc": "Home", "odds": "1.50", "isActive": 1}],
                    }],
                }],
            }],
        },
    }
    return payload, kickoff.date()


class SportyBetFullMarketsTests(unittest.TestCase):
    def test_fetches_event_detail_catalogue_and_uses_its_markets(self):
        index, target_day = _index_payload()
        detail = {
            "bizCode": 10000,
            "data": {
                "markets": [{
                    "id": "18",
                    "desc": "Total Goals",
                    "specifier": "total=1.5",
                    "status": 0,
                    "outcomes": [{"id": "12", "desc": "Over", "odds": "1.75", "isActive": 1}],
                }],
            },
        }
        empty_page = {"bizCode": 10000, "data": {"totalNum": 1, "tournaments": []}}
        with patch(
            "bookmaker_odds._get",
            side_effect=[index, empty_page, empty_page, index, empty_page, detail],
        ) as mocked:
            games = fetch_sportybet(target_day)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["market_source"], "event_detail")
        self.assertEqual(games[0]["markets"][0]["market_id"], "18")
        self.assertEqual(games[0]["markets"][0]["odds"], 1.75)
        self.assertEqual(mocked.call_count, 6)
        index_call = mocked.call_args_list[0]
        self.assertNotIn("marketId", index_call.kwargs["params"])
        self.assertEqual(index_call.kwargs["params"]["option"], "2")

    def test_falls_back_to_index_markets_if_event_detail_has_no_prices(self):
        index, target_day = _index_payload()
        detail = {"bizCode": 10000, "data": {"markets": []}}
        empty_page = {"bizCode": 10000, "data": {"totalNum": 1, "tournaments": []}}
        with patch("bookmaker_odds._get", side_effect=[index, empty_page, empty_page, index, empty_page, detail]):
            games = fetch_sportybet(target_day)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["market_source"], "index_fallback")
        self.assertTrue(games[0]["detail_error"])
        self.assertEqual(games[0]["markets"][0]["market_id"], "1")


if __name__ == "__main__":
    unittest.main()
