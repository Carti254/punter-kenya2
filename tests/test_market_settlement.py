import unittest

from market_settlement import grade_market_evidence


class MarketEvidenceSettlementTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            "fixture": {"score": {"halftime": {"home": 1, "away": 0}}},
            "statistics_full_time": [
                {"team": {"name": "Home FC"}, "statistics": [
                    {"type": "Corner Kicks", "value": 6},
                    {"type": "Yellow Cards", "value": 2},
                    {"type": "Red Cards", "value": 0},
                ]},
                {"team": {"name": "Away FC"}, "statistics": [
                    {"type": "Corner Kicks", "value": 4},
                    {"type": "Yellow Cards", "value": 3},
                    {"type": "Red Cards", "value": 1},
                ]},
            ],
            "statistics_half_time": [],
        }

    def test_half_time_result(self):
        self.assertEqual(grade_market_evidence("Half Time Result", "Home", self.evidence), "WON")
        self.assertEqual(grade_market_evidence("Half Time Result", "Draw", self.evidence), "LOST")

    def test_half_time_total_goals(self):
        self.assertEqual(grade_market_evidence("Half Time Over/Under", "Over 0.5", self.evidence), "WON")

    def test_total_corners(self):
        self.assertEqual(grade_market_evidence("Total Corners", "Over 9.5", self.evidence), "WON")
        self.assertEqual(grade_market_evidence("Total Corners", "Under 10.5", self.evidence), "WON")

    def test_yellow_and_red_cards_separately(self):
        self.assertEqual(grade_market_evidence("Total Yellow Cards", "Over 4.5", self.evidence), "WON")
        self.assertEqual(grade_market_evidence("Total Red Cards", "Over 0.5", self.evidence), "WON")

    def test_missing_statistics_fail_closed(self):
        evidence = dict(self.evidence, statistics_full_time=[])
        with self.assertRaises(ValueError):
            grade_market_evidence("Total Corners", "Over 8.5", evidence)

    def test_integer_line_fails_closed(self):
        with self.assertRaises(ValueError):
            grade_market_evidence("Total Corners", "Over 10", self.evidence)

    def test_player_market_not_guessed(self):
        with self.assertRaises(ValueError):
            grade_market_evidence("Player Shots", "Player X Over 0.5", self.evidence)


if __name__ == "__main__":
    unittest.main()
