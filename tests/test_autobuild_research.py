import unittest

from autobuild_research import assess_candidate, prepare_candidate_pool


class AutobuildResearchTests(unittest.TestCase):
    def candidate(self, **overrides):
        row = {
            "home": "Home FC", "away": "Away FC", "market": "1X2",
            "selection": "Home", "odds": 2.10, "probability": 0.52,
            "bookmaker": "SportyBet",
        }
        row.update(overrides)
        return row

    def test_positive_ev_is_tier_a(self):
        row = assess_candidate(self.candidate())
        self.assertEqual(row["value_tier"], "A")
        self.assertGreater(row["ev"], 0)
        self.assertAlmostEqual(row["implied_probability"], 1 / 2.10)

    def test_break_even_is_retained_but_not_called_positive_value(self):
        row = assess_candidate(self.candidate(odds=2.0, probability=0.5))
        self.assertEqual(row["value_tier"], "B")
        self.assertIn("not established", row["value_label"])

    def test_negative_ev_is_rejected_by_zero_floor(self):
        self.assertIsNone(assess_candidate(self.candidate(odds=2.0, probability=0.49)))

    def test_user_exclusions_are_enforced(self):
        self.assertIsNone(assess_candidate(self.candidate(market="Both Teams To Score")))
        self.assertIsNone(assess_candidate(self.candidate(market="Total Goals", selection="Under 2.5")))

    def test_pool_reports_tiers_and_rejections(self):
        rows, stats = prepare_candidate_pool([
            self.candidate(),
            self.candidate(home="Other", away="Club", odds=2.0, probability=0.5),
            self.candidate(home="Third", away="Side", odds=2.0, probability=0.4),
        ], marginal_ev_floor=0.0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats["tier_a"], 1)
        self.assertEqual(stats["tier_b"], 1)
        self.assertEqual(stats["rejected"], 1)


if __name__ == "__main__":
    unittest.main()
