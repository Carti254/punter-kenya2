import unittest
from unittest.mock import patch
from sportybet_client import SportyBetError, _resolve_market, _find_fixture, _revalidate_selections, prepare_slip_from_candidates, create_booking


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


    def test_booking_requires_explicit_success_code(self):
        leg = {"eventId": "event-1", "marketId": "market-1", "specifier": "total=6.5",
               "outcomeId": "out-over", "fixture": "Home FC vs Away FC", "odds": 1.2}
        payload = {"data": {"shareCode": "ABC123", "outcomes": [leg], "unavailableOutcomes": []}}
        with patch("sportybet_client._revalidate_selections", return_value=[leg]), \
             patch("sportybet_client.SESSION.post", return_value=FakeResponse(payload)):
            with self.assertRaisesRegex(SportyBetError, "did not confirm"):
                create_booking([leg])


    def test_booking_refuses_partial_acceptance(self):
        leg = {"eventId": "event-1", "marketId": "market-1", "specifier": "total=6.5",
               "outcomeId": "out-over", "fixture": "Home FC vs Away FC"}
        payload = {"bizCode": 10000, "data": {"shareCode": "ABC123", "outcomes": [], "unavailableOutcomes": []}}
        with patch("sportybet_client._revalidate_selections", return_value=[leg]), \
             patch("sportybet_client.SESSION.post", return_value=FakeResponse(payload)):
            with self.assertRaises(SportyBetError):
                create_booking([leg])


    def test_ai_candidate_preserves_exact_native_ids(self):
        candidate = {
            "event_id": "event-1", "market_id": "market-1",
            "outcome_id": "out-under", "specifier": "total=6.5",
            "odds": 4.5, "market": "Over/Under", "selection": "Under 6.5",
        }
        with patch("sportybet_client._get_fixtures", return_value=[self.event]):
            resolved = prepare_slip_from_candidates([candidate])
        self.assertEqual(resolved[0]["eventId"], "event-1")
        self.assertEqual(resolved[0]["marketId"], "market-1")
        self.assertEqual(resolved[0]["outcomeId"], "out-under")

    def test_ai_candidate_rejects_changed_price(self):
        candidate = {
            "event_id": "event-1", "market_id": "market-1",
            "outcome_id": "out-under", "specifier": "total=6.5",
            "odds": 4.0, "market": "Over/Under", "selection": "Under 6.5",
        }
        with patch("sportybet_client._get_fixtures", return_value=[self.event]):
            with self.assertRaisesRegex(SportyBetError, "odds changed"):
                prepare_slip_from_candidates([candidate])

    def test_ai_candidate_rejects_missing_native_ids(self):
        with patch("sportybet_client._get_fixtures", return_value=[self.event]):
            with self.assertRaisesRegex(SportyBetError, "native IDs"):
                prepare_slip_from_candidates([{"odds": 4.5}])



    def test_betika_list_malformed_envelope_is_not_empty_day(self):
        from bookmaker_odds import _betika_items
        with self.assertRaisesRegex(ValueError, "recognised matches array"):
            _betika_items({"data": {"unexpected": []}})

    def test_betika_detail_normalizes_prices_and_native_ids(self):
        from bookmaker_odds import _parse_betika_game
        from bookmaker_odds import EAT
        from datetime import datetime
        kickoff = datetime.now(EAT).replace(microsecond=0).isoformat()
        base = {"parent_match_id": 123, "home_team": "Home FC",
                "away_team": "Away FC", "start_time": kickoff}
        detail = (
            {"home_team": "Home FC", "away_team": "Away FC",
             "start_time": kickoff, "competition_name": "Test League"},
            [{"sub_type_id": 1, "name": "1X2", "odds": [
                {"id": 11, "display": "Home", "odd_value": "1.85"},
            ]}],
        )
        game = _parse_betika_game(base, detail)
        self.assertEqual(game["bookmaker"], "Betika")
        self.assertEqual(game["event_id"], "123")
        self.assertEqual(game["markets"][0]["market_id"], "1")
        self.assertEqual(game["markets"][0]["outcome_id"], "11")
        self.assertEqual(game["markets"][0]["odds"], 1.85)

    def test_betika_skips_virtual_competitions(self):
        from bookmaker_odds import fetch_betika, EAT
        from datetime import datetime
        kickoff = datetime.now(EAT).replace(microsecond=0).isoformat()
        listing = {"data": [{"parent_match_id": 555, "home_team": "Virtual A",
                             "away_team": "Virtual B", "start_time": kickoff,
                             "competition_name": "Premier-Zoom"}]}
        with patch("bookmaker_odds._get", return_value=listing) as mocked:
            games = fetch_betika(datetime.now(EAT).date())
        self.assertEqual(games, [])
        mocked.assert_called_once()

    def test_betika_fetches_list_then_match_detail(self):
        from bookmaker_odds import fetch_betika, EAT
        from datetime import datetime
        kickoff = datetime.now(EAT).replace(microsecond=0).isoformat()
        listing = {"data": [{"parent_match_id": 123, "home_team": "Home FC",
                             "away_team": "Away FC", "start_time": kickoff}]}
        detail = {"data": {
            "meta": {"home_team": "Home FC", "away_team": "Away FC",
                     "start_time": kickoff, "competition_name": "Test League"},
            "data": [{"sub_type_id": 1, "name": "1X2", "odds": [
                {"id": 11, "display": "Home", "odd_value": "1.85"}]}],
        }}
        with patch("bookmaker_odds._get", side_effect=[listing, detail]):
            games = fetch_betika(datetime.now(EAT).date())
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["home"], "Home FC")



    def test_live_smoke_uses_bounded_read_only_feed_calls(self):
        from bookmaker_odds import smoke_test_bookmakers
        game = {"home": "Home", "away": "Away", "markets": [{"odds": 1.8}]}
        with patch("bookmaker_odds.fetch_betika", return_value=[game]) as betika, \
             patch("bookmaker_odds.fetch_sportybet", return_value=[game]) as sporty:
            result = smoke_test_bookmakers()
        self.assertTrue(result["Betika"]["request_ok"])
        self.assertTrue(result["Betika tomorrow"]["request_ok"])
        self.assertTrue(result["SportyBet"]["request_ok"])
        self.assertEqual(betika.call_count, 2)
        sporty.assert_called_once()



if __name__ == "__main__":
    unittest.main()
