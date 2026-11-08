import unittest
from unittest.mock import patch
from sportybet_client import SportyBetError, _resolve_market, _find_fixture, _revalidate_selections, create_booking


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class SportyBetTotalOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "eventId": "event-1",
            "homeTeamName": "Home FC",
            "awayTeamName": "Away FC",
            "estimateStartTime": 1999999999000,
            "markets": [{
                "id": "market-1",
                "desc": "Over/Under",
                "specifier": "total=6.5",
                "outcomes": [
                    {"id": "out-over", "desc": "Over 6.5", "odds": 1.2, "isActive": 1},
                    {"id": "out-under", "desc": "Under 6.5", "odds": 4.5, "isActive": 1},
                ],
            }],
        }

    def test_resolves_full_under_line_label(self):
        result = _resolve_market(self.event, "Over/Under", "Under 6.5")
        self.assertEqual(result["outcomeId"], "out-under")
        self.assertEqual(result["selection"], "Under 6.5")

    def test_resolves_full_over_line_label(self):
        result = _resolve_market(self.event, "Over/Under", "Over 6.5")
        self.assertEqual(result["outcomeId"], "out-over")

    def test_does_not_confuse_opposite_total_side(self):
        with self.assertRaises(SportyBetError):
            _resolve_market(self.event, "Over/Under", "Under 5.5")

    def test_fixture_separator_is_parsed(self):
        found = _find_fixture([self.event], "Home FC vs Away FC")
        self.assertEqual(found["eventId"], "event-1")

    def test_rejects_one_sided_fixture_match(self):
        wrong = dict(self.event, homeTeamName="Home FC", awayTeamName="Different Club")
        with self.assertRaises(SportyBetError):
            _find_fixture([wrong], "Home FC vs Away FC")

    def test_suspended_outcome_is_rejected(self):
        event = dict(self.event)
        event["markets"] = [dict(self.event["markets"][0])]
        event["markets"][0]["outcomes"] = [dict(self.event["markets"][0]["outcomes"][0], isActive=0)]
        with self.assertRaises(SportyBetError):
            _resolve_market(event, "Over/Under", "Over 6.5")

    def test_booking_revalidation_rejects_changed_price(self):
        event = {
            "eventId": "event-1", "estimateStartTime": 1999999999000,
            "markets": [{"id": "market-1", "specifier": "total=6.5",
                         "outcomes": [{"id": "out-over", "isActive": 1, "odds": 1.5}]}],
        }
        resolved = [{
            "eventId": "event-1", "marketId": "market-1", "specifier": "total=6.5",
            "outcomeId": "out-over", "fixture": "Home FC vs Away FC", "odds": 1.2,
        }]
        with patch("sportybet_client._get_fixtures", return_value=[event]):
            with self.assertRaisesRegex(SportyBetError, "odds changed"):
                _revalidate_selections(resolved)

    def test_booking_revalidation_rejects_missing_selection(self):
        resolved = [{
            "eventId": "event-1", "marketId": "market-1", "specifier": "total=6.5",
            "outcomeId": "out-over", "fixture": "Home FC vs Away FC",
        }]
        with patch("sportybet_client._get_fixtures", return_value=[]):
            with self.assertRaises(SportyBetError):
                _revalidate_selections(resolved)

    def test_booking_refuses_partial_acceptance(self):
        leg = {"eventId": "event-1", "marketId": "market-1", "specifier": "total=6.5",
               "outcomeId": "out-over", "fixture": "Home FC vs Away FC"}
        payload = {"bizCode": 10000, "data": {"shareCode": "ABC123", "outcomes": [], "unavailableOutcomes": []}}
        with patch("sportybet_client._revalidate_selections", return_value=[leg]), \
             patch("sportybet_client.SESSION.post", return_value=FakeResponse(payload)):
            with self.assertRaises(SportyBetError):
                create_booking([leg])


if __name__ == "__main__":
    unittest.main()
