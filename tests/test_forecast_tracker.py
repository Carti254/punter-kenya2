import unittest

from forecast_tracker import _binary_market_outcome


class ForecastSettlementTests(unittest.TestCase):
    def test_over_under_market_results(self):
        self.assertEqual(_binary_market_outcome("corners", "Over 8.5", 10), 1)
        self.assertEqual(_binary_market_outcome("corners", "Over 8.5", 7), 0)
        self.assertEqual(_binary_market_outcome("corners", "Under 8.5", 7), 1)
        self.assertEqual(_binary_market_outcome("corners", "Under 8.5", 10), 0)

    def test_integer_line_push_is_not_misgraded(self):
        self.assertIsNone(_binary_market_outcome("corners", "Over 8", 8))
        self.assertIsNone(_binary_market_outcome("corners", "Under 8", 8))

    def test_unsupported_selection_is_not_graded(self):
        self.assertIsNone(_binary_market_outcome("corners", "1X2 Home", 8))


if __name__ == "__main__":
    unittest.main()
