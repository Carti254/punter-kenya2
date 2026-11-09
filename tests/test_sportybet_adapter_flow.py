import time
import unittest
from unittest.mock import patch

import sportybet_client as client


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class SportyBetAdapterFlowTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "eventId": "evt-123",
            "homeTeamName": "Home FC",
            "awayTeamName": "Away FC",
            "estimateStartTime": str(int(time.time() * 1000) + 3_600_000),
            "markets": [],
        }
        self.detail = {
            "bizCode": 10000,
            "data": {"markets": [{
                "id": "1", "desc": "1X2", "specifier": None,
                "outcomes": [{"id": "11", "desc": "Home", "odds": "1.75", "isActive": 1}],
            }]},
        }
        self.index = {
            "bizCode": 10000,
            "data": {"tournaments": [{"name": "Test League", "events": [self.event]}]},
        }

    def test_native_id_preview_revalidation_and_share_code_flow(self):
        candidate = {
            "event_id": "evt-123", "market_id": "1", "outcome_id": "11",
            "specifier": None, "odds": 1.75, "home": "Home FC", "away": "Away FC",
        }
        with patch.object(client.SESSION, "get", side_effect=[
            FakeResponse(self.index), FakeResponse(self.detail),
            FakeResponse(self.index), FakeResponse(self.detail),
        ]) as get_mock, patch.object(client.SESSION, "post", return_value=FakeResponse({
            "bizCode": 10000,
            "data": {"shareCode": "AB12CD", "outcomes": [{"id": "11"}]},
        })) as post_mock:
            resolved = client.prepare_slip_from_candidates([candidate])
            result = client.create_booking(resolved)

        self.assertEqual(result["bookingCode"], "AB12CD")
        self.assertEqual(len(result["revalidatedSelections"]), 1)
        self.assertEqual(get_mock.call_count, 4)
        self.assertEqual(post_mock.call_count, 1)

    def test_fixture_index_requests_full_list_and_two_day_window(self):
        with patch.object(client.SESSION, "get", return_value=FakeResponse(self.index)) as get_mock:
            client._get_fixtures()
        params = get_mock.call_args.kwargs["params"]
        self.assertEqual(params["option"], "2")
        self.assertNotIn("timeline", params)


if __name__ == "__main__":
    unittest.main()
