import unittest
from accumulator_engine import build_accumulator

def row(home, away, odds, event, market="1X2", selection="Home"):
    return {"home": home, "away": away, "odds": odds, "event_id": event,
            "eventId": event, "market": market, "selection": selection,
            "market_id": "m1", "marketId": "m1", "outcome_id": "o1",
            "outcomeId": "o1", "bookmaker": "SportyBet",
            "fixture_match_verified": True, "day": "2026-11-10"}

class AccumulatorEngineTests(unittest.TestCase):
    def test_reaches_target_using_distinct_verified_fixtures(self):
        result = build_accumulator([row("A","B",1.5,"e1"), row("C","D",1.4,"e2"),
                                    row("E","F",1.8,"e3")], 2.0, max_legs=4)
        self.assertTrue(result["target_reached"])
        self.assertGreaterEqual(result["combined_odds"], 2.0)
        self.assertLessEqual(result["combined_odds"], 2.4)
        self.assertEqual(len({x["event_id"] for x in result["selections"]}), len(result["selections"]))

    def test_does_not_return_partial_below_target(self):
        result = build_accumulator([row("A","B",1.5,"e1")], 2.0)
        self.assertFalse(result["target_reached"])
        self.assertEqual(result["selections"], [])
        self.assertEqual(result["combined_odds"], 1.0)

    def test_rejects_missing_native_ids_and_unverified_fixtures(self):
        bad = row("A","B",1.8,"e1")
        bad.pop("outcomeId")
        unverified = row("C","D",1.8,"e2")
        unverified["fixture_match_verified"] = False
        result = build_accumulator([bad, unverified], 2.0)
        self.assertFalse(result["target_reached"])

    def test_rejects_banned_markets_and_under_25(self):
        candidates = [
            row("A","B",1.8,"e1","Both Teams To Score","Yes"),
            row("C","D",1.8,"e2","Total Goals","Under 2.5"),
            row("E","F",1.8,"e3","Total Goals","Over 1.5"),
            row("G","H",1.8,"e4","Total Goals","Over 1.5"),
        ]
        result = build_accumulator(candidates, 3.0)
        self.assertTrue(result["target_reached"])
        self.assertTrue(all(x["selection"] == "Over 1.5" for x in result["selections"]))

    def test_only_one_selection_per_fixture(self):
        first = row("A","B",1.5,"e1")
        second = row("A","B",1.8,"e1","Double Chance","Draw or Away")
        result = build_accumulator([first, second, row("C","D",1.5,"e2")], 2.0)
        self.assertTrue(result["target_reached"])
        self.assertEqual(len(result["selections"]), 2)
        self.assertEqual(len({x["event_id"] for x in result["selections"]}), 2)

    def test_quality_ranking_prefers_higher_joint_probability(self):
        def qualified(home, odds, probability, event):
            item = row(home, "Away", odds, event)
            item.update({"model_probability": probability,
                         "expected_value": probability * odds - 1,
                         "model_home_sample": 4, "model_away_sample": 4})
            return item
        candidates = [
            qualified("Low A", 1.40, 0.65, "e1"),
            qualified("Low B", 1.50, 0.62, "e2"),
            qualified("High A", 1.40, 0.78, "e3"),
            qualified("High B", 1.50, 0.75, "e4"),
        ]
        result = build_accumulator(candidates, 2.0, ranking="quality")
        self.assertTrue(result["target_reached"])
        self.assertEqual({x["event_id"] for x in result["selections"]}, {"e3", "e4"})
        self.assertGreater(result["combined_probability"], 0.5)

    def test_quality_mode_can_choose_more_than_two_legs(self):
        candidates = []
        for i in range(3):
            item = row(f"Team{i}", f"Opponent{i}", 1.30, f"e{i}")
            item.update({"model_probability": 0.80, "expected_value": 0.00,
                         "model_home_sample": 10, "model_away_sample": 10})
            candidates.append(item)
        result = build_accumulator(candidates, 2.0, ranking="quality", max_legs=5)
        self.assertTrue(result["target_reached"])
        self.assertEqual(len(result["selections"]), 3)

    def test_quality_mode_rejects_unassessed_candidates(self):
        result = build_accumulator([row("A", "B", 1.8, "e1"),
                                    row("C", "D", 1.8, "e2")],
                                   2.0, ranking="quality")
        self.assertFalse(result["target_reached"])

if __name__ == "__main__":
    unittest.main()
