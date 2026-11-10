import unittest
from autobuild_research import assess_candidate, prepare_candidate_pool

class AutobuildResearchTests(unittest.TestCase):
    def candidate(self, **overrides):
        row = {"home":"Home FC","away":"Away FC","market":"1X2","selection":"Home",
               "odds":2.1,"bookmaker":"SportyBet","event_id":"e1","eventId":"e1",
               "market_id":"m1","marketId":"m1","outcome_id":"o1","outcomeId":"o1",
               "fixture_match_verified":True}
        row.update(overrides)
        return row

    def test_valid_executable_price_is_retained_without_model_fields(self):
        item = assess_candidate(self.candidate())
        self.assertIsNotNone(item)
        self.assertNotIn("model_probability", item)
        self.assertNotIn("expected_value", item)

    def test_invalid_price_is_rejected(self):
        self.assertIsNone(assess_candidate(self.candidate(odds=4.0)))

    def test_missing_native_id_is_rejected(self):
        self.assertIsNone(assess_candidate(self.candidate(outcomeId=None, outcome_id=None)))

    def test_unverified_fixture_is_rejected(self):
        self.assertIsNone(assess_candidate(self.candidate(fixture_match_verified=False)))

    def test_btts_and_under_25_are_rejected(self):
        self.assertIsNone(assess_candidate(self.candidate(market="Both Teams To Score", selection="Yes")))
        self.assertIsNone(assess_candidate(self.candidate(market="Total Goals", selection="Under 2.5")))

    def test_supported_markets_are_retained(self):
        self.assertIsNotNone(assess_candidate(self.candidate(market="Double Chance", selection="Draw or Away")))
        self.assertIsNotNone(assess_candidate(self.candidate(market="Total Goals", selection="Over 1.5")))

    def test_pool_reports_eligible_and_rejected_counts(self):
        rows, stats = prepare_candidate_pool([self.candidate(), self.candidate(odds=4.0)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["rejected"], 1)

if __name__ == "__main__":
    unittest.main()
