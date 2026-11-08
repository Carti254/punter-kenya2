import unittest
from datetime import date
from unittest.mock import patch

import premium_tracking


class AutomaticPriceCaptureTests(unittest.TestCase):
    @patch("premium_tracking.save_price_observations", return_value=17)
    @patch("bookmaker_odds.fetch_all")
    def test_default_capture_checks_today_and_tomorrow(self, fetch_all, save_prices):
        fetch_all.side_effect = [
            {"bookmakers": {"Betika": {"games": [{"home": "A", "away": "B"}]}}, "errors": {}},
            {"bookmakers": {"SportyBet": {"games": [{"home": "C", "away": "D"}]}}, "errors": {}},
        ]
        with patch("premium_tracking.datetime") as clock:
            clock.now.return_value.date.return_value = date(2026, 11, 8)
            result = premium_tracking.capture_current_prices()
        self.assertEqual(fetch_all.call_count, 2)
        self.assertEqual(result["dates"], ["2026-11-08", "2026-11-09"])
        self.assertEqual(result["events"], 2)
        self.assertEqual(result["saved"], 17)
        self.assertEqual(save_prices.call_count, 1)
        self.assertEqual(len(save_prices.call_args.args[0]), 2)

    @patch("premium_tracking.save_price_observations", return_value=3)
    @patch("bookmaker_odds.fetch_all")
    def test_explicit_date_only_fetches_one_day(self, fetch_all, save_prices):
        fetch_all.return_value = {"bookmakers": {}, "errors": {"SportyBet": "rate limited"}}
        result = premium_tracking.capture_current_prices(date(2026, 11, 10))
        fetch_all.assert_called_once_with(date(2026, 11, 10))
        self.assertEqual(result["dates"], ["2026-11-10"])
        self.assertEqual(result["saved"], 3)
        self.assertIn("SportyBet", result["errors"]["2026-11-10"])


if __name__ == "__main__":
    unittest.main()
