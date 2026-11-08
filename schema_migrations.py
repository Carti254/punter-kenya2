"""Versioned, idempotent MySQL schema migrations for PUNTER KENYA."""
from database import get_connection

LATEST_VERSION = 2


def _table_exists(cursor, table_name):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (table_name,),
    )
    return int(cursor.fetchone()["n"]) > 0


def _column_exists(cursor, table_name, column_name):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
        (table_name, column_name),
    )
    return int(cursor.fetchone()["n"]) > 0


def run_migrations():
    """Apply safe, repeatable schema upgrades; DDL is idempotent for MySQL."""
    connection = get_connection()
    applied_now = []
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

            # Repair the previously reported missing-column deployment even if
            # an older migration marker was already written.
            if _table_exists(cursor, "matches") and not _column_exists(cursor, "matches", "last_synced_at"):
                cursor.execute("ALTER TABLE matches ADD COLUMN last_synced_at DATETIME NULL")

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
                cursor.execute("INSERT INTO punter_schema_migrations (version) VALUES (1)")
                applied_now.append(1)

            if 2 not in applied:
                # Ensure tables introduced by the slip/results features exist
                # even on databases whose v1 marker predates those features.
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
                    CREATE TABLE IF NOT EXISTS punter_slips (
                        slip_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        bookmaker VARCHAR(80) NOT NULL DEFAULT 'Manual',
                        selections_json LONGTEXT NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_punter_slips_user_status (user_id, status)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("INSERT INTO punter_schema_migrations (version) VALUES (2)")
                applied_now.append(2)

        return {"applied": applied_now, "latest_version": LATEST_VERSION}
    finally:
        connection.close()
