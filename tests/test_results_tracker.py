import os
import unittest
from results_tracker import grade_selection
from database import get_connection
from schema_migrations import run_migrations


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

    def test_double_chance(self):
        self.assertEqual(grade_selection("Double Chance", "1X", 1, 1), "WON")
        self.assertEqual(grade_selection("Double Chance", "X2", 2, 0), "LOST")

    def test_draw_no_bet_draw_is_void(self):
        self.assertEqual(grade_selection("Draw No Bet", "Home", 1, 1), "VOID")
        self.assertEqual(grade_selection("DNB", "Away", 0, 2), "WON")

    def test_btts_and_odd_even(self):
        self.assertEqual(grade_selection("BTTS", "Yes", 1, 1), "WON")
        self.assertEqual(grade_selection("BTTS", "No", 0, 1), "WON")
        self.assertEqual(grade_selection("Odd/Even", "Even", 2, 2), "WON")

    def test_correct_score(self):
        self.assertEqual(grade_selection("Correct Score", "2-1", 2, 1), "WON")
        self.assertEqual(grade_selection("Correct Score", "0-0", 2, 1), "LOST")

    def test_unknown_or_unsupported_market_fails_closed(self):
        with self.assertRaises(ValueError):
            grade_selection("Corners", "Over 8.5", 2, 1)
        with self.assertRaises(ValueError):
            grade_selection("Asian Handicap", "Home -0.25", 2, 1)


@unittest.skipUnless(os.getenv("PUNTER_MYSQL_INTEGRATION") == "1", "requires disposable MySQL service")
class MySQLMigrationIntegrationTests(unittest.TestCase):
    def test_legacy_matches_table_is_repaired_and_migrations_are_idempotent(self):
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE IF EXISTS punter_bet_records")
                cursor.execute("DROP TABLE IF EXISTS punter_slips")
                cursor.execute("DROP TABLE IF EXISTS punter_schema_migrations")
                cursor.execute("DROP TABLE IF EXISTS matches")
                cursor.execute("CREATE TABLE matches (id BIGINT NOT NULL PRIMARY KEY) ENGINE=InnoDB")
        finally:
            connection.close()

        first = run_migrations()
        self.assertEqual(first["latest_version"], 3)
        self.assertEqual(first["applied"], [1, 2])

        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) AS n FROM information_schema.columns
                    WHERE table_schema=DATABASE() AND table_name='matches'
                      AND column_name='last_synced_at'
                """)
                self.assertEqual(int(cursor.fetchone()["n"]), 1)
                cursor.execute("""
                    SELECT COUNT(*) AS n FROM information_schema.tables
                    WHERE table_schema=DATABASE()
                      AND table_name IN ('punter_bet_records', 'punter_slips')
                """)
                self.assertEqual(int(cursor.fetchone()["n"]), 2)
        finally:
            connection.close()

        second = run_migrations()
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["latest_version"], 3)




if __name__ == "__main__":
    unittest.main()


class UtcMatchDateTests(unittest.TestCase):
    def test_utc_match_date_uses_source_calendar_day(self):
        from datetime import datetime, timezone, timedelta
        from results_tracker import utc_match_date
        # 00:30 EAT on 9 November is 21:30 UTC on 8 November.
        kickoff = datetime(2026, 11, 9, 0, 30, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(utc_match_date(kickoff).isoformat(), "2026-11-08")

    def test_naive_fixture_datetime_is_treated_as_utc(self):
        from datetime import datetime
        from results_tracker import utc_match_date
        self.assertEqual(utc_match_date(datetime(2026, 11, 8, 23, 30)).isoformat(), "2026-11-08")
