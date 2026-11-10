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

if __name__ == "__main__":
    unittest.main()
