import unittest

from sportybet_client import SportyBetError, _resolve_market


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


if __name__ == "__main__":
    unittest.main()
