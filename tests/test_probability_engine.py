import unittest
from datetime import date

from probability_engine import (
    _market_probability,
    _prepare_history,
    _indexed_team_rows,
    MIN_SELECTION_PROBABILITY, MIN_TEAM_MATCHES, MIN_VALUE_EDGE,
    assess_candidate_detailed, assess_candidates_with_diagnostics,
)


class ProbabilityEngineTests(unittest.TestCase):
    def test_relaxed_screening_thresholds_are_applied(self):
        self.assertEqual(MIN_SELECTION_PROBABILITY, 0.52)
        self.assertEqual(MIN_TEAM_MATCHES, 4)
        self.assertEqual(MIN_VALUE_EDGE, 0.00)

    def test_1x2_accepts_the_actual_sportybet_team_name(self):
        prediction = {"home": 0.52, "draw": 0.27, "away": 0.21}
        row = {
            "market": "1X2",
            "selection": "Leganes",
            "home": "Leganes",
            "away": "CF Trival Valderas Alcorcon",
        }
        probability, label = _market_probability(row, prediction)
        self.assertEqual(probability, 0.52)
        self.assertEqual(label, "1X2 home")

    def test_1x2_accepts_away_team_name(self):
        prediction = {"home": 0.52, "draw": 0.27, "away": 0.21}
        row = {"market": "Match Result", "selection": "Away FC",
               "home": "Home FC", "away": "Away FC"}
        probability, label = _market_probability(row, prediction)
        self.assertEqual(probability, 0.21)
        self.assertEqual(label, "1X2 away")

    def test_double_chance_and_over_markets_are_mapped(self):
        prediction = {"home": 0.50, "draw": 0.28, "away": 0.22,
                      "over15": 0.78, "over25": 0.54}
        probability, _ = _market_probability(
            {"market": "Double Chance", "selection": "Draw or Away"}, prediction)
        self.assertAlmostEqual(probability, 0.50)
        probability, _ = _market_probability(
            {"market": "Over/Under", "selection": "Over 1.5"}, prediction)
        self.assertEqual(probability, 0.78)

    def test_btts_and_under_25_are_not_modelled(self):
        prediction = {"home": 0.5, "draw": 0.25, "away": 0.25,
                      "over15": 0.8, "over25": 0.6}
        self.assertIsNone(_market_probability(
            {"market": "Both Teams To Score", "selection": "Yes"}, prediction)[0])
        self.assertIsNone(_market_probability(
            {"market": "Total Goals", "selection": "Under 2.5"}, prediction)[0])

    def test_history_is_indexed_by_team_and_excludes_future_results(self):
        history = [
            {"date": "2026-11-01", "home": "Home FC", "away": "Other FC",
             "ft_home": "2", "ft_away": "0"},
            {"date": "2026-11-12", "home": "Home FC", "away": "Other FC",
             "ft_home": "0", "ft_away": "4"},
        ]
        index, baseline = _prepare_history(history)
        rows = _indexed_team_rows(index, "Home", date(2026, 11, 10))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gf"], 2)
        self.assertEqual(baseline[0], 2)


    def test_diagnostics_explain_invalid_candidate_instead_of_silent_none(self):
        item, reason = assess_candidate_detailed(
            {"kickoff": "not-a-date", "odds": "1.80"}, []
        )
        self.assertIsNone(item)
        self.assertEqual(reason, "invalid_fixture_date")

    def test_batch_diagnostics_count_every_candidate(self):
        rows = [
            {"kickoff": "not-a-date", "odds": "1.80"},
            {"kickoff": "2026-11-11", "odds": "1.80", "home": "A", "away": "B"},
        ]
        qualified, diagnostics = assess_candidates_with_diagnostics(rows, [])
        self.assertEqual(qualified, [])
        self.assertEqual(diagnostics["scanned"], 2)
        self.assertEqual(diagnostics["rejected"], 2)
        self.assertEqual(diagnostics["reasons"]["invalid_fixture_date"], 1)
        self.assertEqual(diagnostics["reasons"]["history_baseline_below_30"], 1)


if __name__ == "__main__":
    unittest.main()
