import unittest

from free_football_sources import (
    _line_probability,
    _same_team,
    _extract_prediction_1x2,
    _external_probability,
    _match_event,
)


class FreeFootballSourceTests(unittest.TestCase):
    def test_team_name_matching_is_conservative(self):
        self.assertTrue(_same_team("Manchester United FC", "Manchester United"))
        self.assertTrue(_same_team("Atletico Madrid", "Atlético Madrid"))
        self.assertFalse(_same_team("Manchester City", "Manchester United"))

    def test_bsd_corner_over_probability_uses_exact_line(self):
        prediction = {"markets": {"corners": {"prob_over_95": 61.5}}}
        self.assertAlmostEqual(
            _line_probability(prediction, "Total Corners", "Over 9.5"),
            0.615,
        )

    def test_bsd_corner_under_is_complement(self):
        prediction = {"markets": {"corners": {"prob_over_85": 70}}}
        self.assertAlmostEqual(
            _line_probability(prediction, "Corners", "Under 8.5"),
            0.30,
        )

    def test_unknown_corner_line_is_not_guessed(self):
        prediction = {"markets": {"corners": {"prob_over_95": 61.5}}}
        self.assertIsNone(_line_probability(prediction, "Corners", "Over 10.5"))

    def test_bsd_1x2_probabilities_are_normalized_to_unit_scale(self):
        prediction = {"markets": {"match_result": {
            "prob_home": 45, "prob_draw": 30, "prob_away": 25
        }}}
        self.assertEqual(_extract_prediction_1x2(prediction), {
            "prob_home": 0.45, "prob_draw": 0.30, "prob_away": 0.25
        })

    def test_bsd_result_probability_maps_to_exact_selection(self):
        prediction = {"markets": {"match_result": {
            "prob_home": 45, "prob_draw": 30, "prob_away": 25
        }}}
        self.assertAlmostEqual(_external_probability(prediction, "1X2", "Home"), 0.45)
        self.assertAlmostEqual(_external_probability(prediction, "1X2", "Draw"), 0.30)

    def test_bsd_goal_total_probability_maps_to_exact_line(self):
        prediction = {"markets": {"over_under": {"prob_over_25": 44.8}}}
        self.assertAlmostEqual(_external_probability(prediction, "Total Goals", "Over 2.5"), 0.448)
        self.assertAlmostEqual(_external_probability(prediction, "Total Goals", "Under 2.5"), 0.552)

    def test_fixture_match_requires_home_away_orientation(self):
        rows = [{
            "home_team": {"name": "Arsenal"},
            "away_team": {"name": "Chelsea"},
            "event_date": "2026-11-09T15:00:00Z",
        }]
        from datetime import date
        self.assertIsNotNone(_match_event(rows, "Arsenal", "Chelsea", date(2026, 11, 9), "bsd"))
        self.assertIsNone(_match_event(rows, "Chelsea", "Arsenal", date(2026, 11, 9), "bsd"))


if __name__ == "__main__":
    unittest.main()
