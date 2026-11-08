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


if __name__ == "__main__":
    unittest.main()
