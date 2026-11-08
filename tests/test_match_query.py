import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class MatchQueryRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_team_matcher_tolerates_small_spelling_error(self):
        self.assertTrue(bot.team_name_matches_query("Brenford", "Brentford"))
        self.assertTrue(bot.team_name_matches_query("Brighton", "Brighton and Hove Albion"))

    def test_analysis_fixture_resolves_both_names_with_typo(self):
        raw = {
            "id": "fixture-123",
            "homeTeamName": "Brighton",
            "awayTeamName": "Brentford",
            "leagueName": "Premier League",
            "kickoffUtc": "2026-11-20T20:00:00Z",
            "matchStatus": "Scheduled",
        }
        with patch("bot.fetch_matchintel_fixture_rows", return_value=[raw]):
            found_raw, fixture, error = bot.find_analysis_fixture("Brighton vs Brenford")
        self.assertIsNone(error)
        self.assertEqual(found_raw["id"], "fixture-123")
        self.assertEqual(fixture["home_team"], "Brighton")
        self.assertEqual(fixture["away_team"], "Brentford")

    async def test_analyze_menu_preserves_vs_separator(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(text="Brighton vs Brenford"),
            effective_user=SimpleNamespace(id=123),
        )
        context = SimpleNamespace(user_data={"slip_flow": "analyze"}, args=[])
        with patch("bot.deepdive_command", new_callable=AsyncMock) as deepdive:
            handled = await bot.process_slip_flow(update, context)
        self.assertTrue(handled)
        self.assertEqual(context.args, ["Brighton vs Brenford"])
        deepdive.assert_awaited_once_with(update, context)


if __name__ == "__main__":
    unittest.main()
