import unittest
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

    def test_actual_home_team_name_is_accepted(self):
        self.assertEqual(grade_selection("1X2", "Arsenal", 2, 0, "Arsenal", "Chelsea"), "WON")

    def test_actual_away_team_name_is_accepted(self):
        self.assertEqual(grade_selection("1X2", "Chelsea", 2, 0, "Arsenal", "Chelsea"), "LOST")

    def test_negative_score_is_rejected(self):
        with self.assertRaises(ValueError):
            grade_selection("1X2", "Home", -1, 0)

    def test_unknown_market_fails_closed(self):
        with self.assertRaises(ValueError):
            grade_selection("Corners", "Over 8.5", 2, 1)


if __name__ == "__main__":
    unittest.main()
