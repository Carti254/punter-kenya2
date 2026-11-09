import unittest

from candidate_risk import assess_candidate_risk


class CandidateRiskTests(unittest.TestCase):
    def test_severe_model_disagreement_is_rejected(self):
        result = assess_candidate_risk({
            "external_model_disagreement": 0.27,
            "model_confidence": "MODERATE",
            "home_sample": 8,
            "away_sample": 9,
        })
        self.assertTrue(result["risk_rejected"])
        self.assertEqual(result["risk_level"], "HIGH")
        self.assertTrue(any("disagreement" in flag for flag in result["risk_flags"]))

    def test_missing_data_is_reported_not_fabricated(self):
        result = assess_candidate_risk({})
        self.assertFalse(result["risk_rejected"])
        self.assertTrue(any("line-ups" in gap for gap in result["risk_gaps"]))
        self.assertTrue(any("xG" in gap for gap in result["risk_gaps"]))

    def test_low_confidence_is_flagged(self):
        result = assess_candidate_risk({"model_confidence": "LOW"})
        self.assertFalse(result["risk_rejected"])
        self.assertEqual(result["risk_level"], "MEDIUM")
        self.assertTrue(result["risk_flags"])

    def test_high_ensemble_upset_risk_is_rejected(self):
        result = assess_candidate_risk({
            "upset_risk": {"level": "HIGH", "draw_probability": 0.34},
            "external_model_disagreement": 0.26,
            "model_confidence": "LOW",
        })
        self.assertTrue(result["risk_rejected"])
        self.assertEqual(result["risk_level"], "HIGH")

    def test_xg_disagreement_is_used_as_a_risk_flag(self):
        result = assess_candidate_risk({
            "lambda_home": 1.8, "lambda_away": 0.9,
            "xg_evidence": {
                "home_xg_for": 0.8, "away_xg_against": 0.7,
                "away_xg_for": 1.7, "home_xg_against": 1.6,
            },
        })
        self.assertTrue(any("xG-versus-goal-model" in flag for flag in result["risk_flags"]))

    def test_converging_risk_flags_reject_candidate(self):
        result = assess_candidate_risk({
            "model_confidence": "LOW",
            "home_sample": 3,
            "away_sample": 4,
        })
        self.assertTrue(result["risk_rejected"])
        self.assertEqual(result["risk_level"], "HIGH")


if __name__ == "__main__":
    unittest.main()
