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
        self.assertEqual(first["latest_version"], 2)
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
        self.assertEqual(second["latest_version"], 2)




if __name__ == "__main__":
    unittest.main()
