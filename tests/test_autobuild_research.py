import unittest

from autobuild_research import assess_candidate, prepare_candidate_pool, apply_walk_forward_calibration


class AutobuildResearchTests(unittest.TestCase):
    def candidate(self, **overrides):
        row = {
            "home": "Home FC", "away": "Away FC", "market": "1X2",
            "selection": "Home", "odds": 2.10, "probability": 0.52,
            "bookmaker": "SportyBet",
            "model_confidence": "MODERATE",
            "home_sample": 10, "away_sample": 10,
            "home_home_sample": 5, "away_away_sample": 5,
        }
        row.update(overrides)
        return row

    def test_positive_ev_is_tier_a(self):
        row = assess_candidate(self.candidate())
        self.assertEqual(row["value_tier"], "A")
        self.assertGreater(row["ev"], 0)
        self.assertAlmostEqual(row["implied_probability"], 1 / 2.10)

    def test_break_even_is_retained_but_not_called_positive_value(self):
        row = assess_candidate(self.candidate(odds=2.0, probability=0.5), marginal_ev_floor=0.0)
        self.assertEqual(row["value_tier"], "B")
        self.assertIn("not established", row["value_label"])

    def test_dixon_coles_recalculates_1x2_when_goal_rates_exist(self):
        from dixon_coles_engine import selected_outcome_probability
        row = assess_candidate(self.candidate(lambda_home=1.8, lambda_away=0.9), use_dixon_coles=True)
        expected = selected_outcome_probability(1.8, 0.9, "Home", "Home FC", "Away FC")
        self.assertTrue(row["dixon_coles_adjusted"])
        self.assertEqual(row["probability_source"].split(" (")[0], "Dixon-Coles Poisson")
        self.assertAlmostEqual(row["probability"], expected)

    def test_walk_forward_calibration_is_not_applied_without_ready_holdout(self):
        rows = [self.candidate(lambda_home=1.8, lambda_away=0.9)]
        result, status = apply_walk_forward_calibration(rows, {"ready": False})
        self.assertFalse(status["applied"])
        self.assertEqual(result[0]["probability"], rows[0]["probability"])

    def test_walk_forward_calibration_requires_both_holdout_metrics_to_improve(self):
        rows = [self.candidate(lambda_home=1.8, lambda_away=0.9)]
        validation = {
            "ready": True, "temperature": 1.2, "train": 100, "holdout": 40,
            "raw_brier": 0.20, "raw_logloss": 0.60,
            "candidate_brier": 0.18, "candidate_logloss": 0.58,
            "candidate_calibrated_brier": 0.17, "candidate_calibrated_logloss": 0.56,
            "baseline_brier": 0.22, "baseline_logloss": 0.65,
            "source": "unit test",
        }
        result, status = apply_walk_forward_calibration(rows, validation)
        self.assertTrue(status["applied"])
        self.assertTrue(result[0]["probability_source"].startswith("Dixon-Coles + walk-forward"))
        audited = assess_candidate(result[0], use_dixon_coles=True)
        self.assertTrue(audited["probability_source"].startswith("Dixon-Coles + walk-forward"))
        self.assertTrue(audited["research_evidence"]["calibration_evidence"]["applied"])

    def test_walk_forward_calibration_requires_both_candidate_metrics_to_improve(self):
        rows = [self.candidate(lambda_home=1.8, lambda_away=0.9)]
        validation = {
            "ready": True, "temperature": 1.2, "train": 100, "holdout": 40,
            "raw_brier": 0.20, "raw_logloss": 0.60,
            "candidate_brier": 0.18, "candidate_logloss": 0.58,
            "candidate_calibrated_brier": 0.17, "candidate_calibrated_logloss": 0.59,
            "baseline_brier": 0.22, "baseline_logloss": 0.65,
            "source": "unit test",
        }
        result, status = apply_walk_forward_calibration(rows, validation)
        self.assertFalse(status["applied"])
        self.assertEqual(result[0]["probability"], rows[0]["probability"])

    def test_dixon_coles_is_not_applied_to_default_production_candidates(self):
        row = assess_candidate(self.candidate(lambda_home=1.8, lambda_away=0.9))
        self.assertFalse(row["dixon_coles_adjusted"] if "dixon_coles_adjusted" in row else False)
        self.assertEqual(row["probability"], 0.52)

    def test_dixon_coles_is_not_used_without_goal_rates(self):
        row = assess_candidate(self.candidate())
        self.assertFalse(row["dixon_coles_adjusted"] if "dixon_coles_adjusted" in row else False)
        self.assertEqual(row["probability"], 0.52)

    def test_low_confidence_forecast_is_rejected_even_when_ev_is_positive(self):
        row = self.candidate(model_confidence="LOW", home_sample=10, away_sample=10)
        self.assertIsNone(assess_candidate(row))

    def test_moderate_confidence_with_minimum_team_samples_is_eligible(self):
        row = self.candidate(model_confidence="MODERATE", home_sample=5, away_sample=5)
        self.assertIsNotNone(assess_candidate(row))

    def test_insufficient_verified_team_sample_is_rejected(self):
        row = self.candidate(model_confidence="MODERATE", home_sample=4, away_sample=10)
        self.assertIsNone(assess_candidate(row))

    def test_missing_venue_samples_are_rejected(self):
        row = self.candidate(home_home_sample=None, away_away_sample=5)
        self.assertIsNone(assess_candidate(row))

    def test_special_2up_market_is_rejected_as_not_ordinary_1x2(self):
        row = self.candidate(market="1X2 - 2UP", selection="Away")
        self.assertIsNone(assess_candidate(row))

    def test_negative_ev_is_rejected_by_zero_floor(self):
        self.assertIsNone(assess_candidate(self.candidate(odds=2.0, probability=0.49)))

    def test_default_accumulator_floor_requires_three_percent_ev(self):
        # Odds 2.00 at 51% probability is +2% EV and must not qualify.
        self.assertIsNone(assess_candidate(self.candidate(odds=2.0, probability=0.51)))
        # Odds 2.00 at 52% probability is +4% EV and qualifies.
        row = assess_candidate(self.candidate(odds=2.0, probability=0.52))
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["ev"], 0.03)

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
