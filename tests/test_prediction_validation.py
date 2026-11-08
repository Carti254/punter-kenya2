import unittest
from prediction_validation import brier_score, log_loss, reliability_bins, fit_temperature, apply_temperature


class PredictionValidationTests(unittest.TestCase):
    def test_perfect_forecast_scores(self):
        p = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        y = [0, 1, 2]
        self.assertAlmostEqual(brier_score(p, y), 0.0)
        self.assertAlmostEqual(log_loss(p, y), 0.0, places=10)

    def test_reliability_bins_report_counts(self):
        rows = reliability_bins([[.8, .1, .1], [.2, .7, .1]], [0, 1], bins=5)
        self.assertTrue(rows)
        self.assertTrue(all(row["count"] > 0 for row in rows))

    def test_temperature_keeps_probability_sum(self):
        calibrated = apply_temperature([.7, .2, .1], 1.2)
        self.assertAlmostEqual(sum(calibrated), 1.0)
        self.assertGreater(calibrated[0], calibrated[1])

    def test_fit_requires_historical_sample(self):
        with self.assertRaises(ValueError):
            fit_temperature([[.6, .3, .1]], [0])


if __name__ == "__main__":
    unittest.main()
