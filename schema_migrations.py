"""Versioned, idempotent MySQL schema migrations for PUNTER KENYA."""
from database import get_connection

LATEST_VERSION = 1


def run_migrations():
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS punter_schema_migrations (
                    version INT NOT NULL PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute("SELECT version FROM punter_schema_migrations")
            applied = {int(row["version"]) for row in cursor.fetchall()}

            if 1 not in applied:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS punter_bet_records (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        bookmaker VARCHAR(40) NOT NULL,
                        fixture_key VARCHAR(500) NOT NULL,
                        match_date DATE NULL,
                        home_team VARCHAR(180) NOT NULL,
                        away_team VARCHAR(180) NOT NULL,
                        market VARCHAR(120) NOT NULL,
                        selection VARCHAR(180) NOT NULL,
                        decimal_odds DECIMAL(10,3) NOT NULL,
                        stake DECIMAL(12,2) NOT NULL,
                        status VARCHAR(12) NOT NULL DEFAULT 'PENDING',
                        actual_home_goals INT NULL,
                        actual_away_goals INT NULL,
                        profit_loss DECIMAL(12,2) NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        settled_at TIMESTAMP NULL,
                        INDEX idx_bets_user_status (user_id, status),
                        INDEX idx_bets_fixture (fixture_key)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("""
                    SELECT COUNT(*) AS n FROM information_schema.tables
                    WHERE table_schema = DATABASE() AND table_name = 'matches'
                """)
                if int(cursor.fetchone()["n"]) > 0:
                    cursor.execute("""
                        SELECT COUNT(*) AS n FROM information_schema.columns
                        WHERE table_schema = DATABASE()
                          AND table_name = 'matches'
                          AND column_name = 'last_synced_at'
                    """)
                    if int(cursor.fetchone()["n"]) == 0:
                        cursor.execute("ALTER TABLE matches ADD COLUMN last_synced_at DATETIME NULL")
                cursor.execute("INSERT INTO punter_schema_migrations (version) VALUES (1)")
        return {"applied": sorted({1} - applied), "latest_version": LATEST_VERSION}
    finally:
        connection.close()
