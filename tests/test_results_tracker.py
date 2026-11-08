import unittest
from decimal import Decimal
from results_tracker import grade_selection


class BetSettlementTests(unittest.TestCase):
    def test_home_win(self):
        self.assertEqual(grade_selection("1X2", "Home", 2, 0), "WON")

    def test_draw_loss_for_away(self):
        self.assertEqual(grade_selection("1X2", "Away", 1, 1), "LOST")

    def test_over_under(self):
        self.assertEqual(grade_selection("Over/Under", "Over 2.5", 2, 1), "WON")
        self.assertEqual(grade_selection("Over/Under", "Under 2.5", 2, 1), "LOST")

    def test_integer_line_push_is_void(self):
        self.assertEqual(grade_selection("Over/Under", "Under 3.0", 2, 1), "VOID")

    def test_unknown_market_fails_closed(self):
        with self.assertRaises(ValueError):
            grade_selection("Corners", "Over 8.5", 2, 1)


if __name__ == "__main__":
    unittest.main()
