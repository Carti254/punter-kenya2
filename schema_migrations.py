"""Versioned, idempotent MySQL schema migrations for PUNTER KENYA."""
from database import get_connection

LATEST_VERSION = 4


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

            if 3 not in applied:
                # Persist source and raw evidence for auditable non-score settlements.
                if _table_exists(cursor, "punter_bet_records"):
                    if not _column_exists(cursor, "punter_bet_records", "settlement_source"):
                        cursor.execute("ALTER TABLE punter_bet_records ADD COLUMN settlement_source VARCHAR(40) NULL")
                    if not _column_exists(cursor, "punter_bet_records", "settlement_evidence_json"):
                        cursor.execute("ALTER TABLE punter_bet_records ADD COLUMN settlement_evidence_json LONGTEXT NULL")
                cursor.execute("INSERT INTO punter_schema_migrations (version) VALUES (3)")
                applied_now.append(3)


            if 4 not in applied:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS punter_user_settings (
                        user_id BIGINT NOT NULL PRIMARY KEY,
                        bankroll DECIMAL(14,2) NULL,
                        preferences_json LONGTEXT NULL,
                        rollover_days INT NULL,
                        rollover_target DECIMAL(10,3) NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS punter_price_snapshots (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        fixture_key VARCHAR(500) NOT NULL,
                        bookmaker VARCHAR(80) NOT NULL,
                        market VARCHAR(120) NOT NULL,
                        selection VARCHAR(180) NOT NULL,
                        decimal_odds DECIMAL(10,3) NOT NULL,
                        captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_price_fixture_market (fixture_key(191), market),
                        INDEX idx_price_captured (captured_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS punter_forecast_results (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        fixture_key VARCHAR(500) NOT NULL,
                        market VARCHAR(120) NOT NULL,
                        selection VARCHAR(180) NOT NULL,
                        probability DECIMAL(8,6) NOT NULL,
                        decimal_odds DECIMAL(10,3) NULL,
                        outcome TINYINT NULL,
                        model_version VARCHAR(80) NOT NULL,
                        forecast_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        settled_at TIMESTAMP NULL,
                        INDEX idx_forecast_fixture (fixture_key(191)),
                        INDEX idx_forecast_market (market, settled_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cursor.execute("INSERT INTO punter_schema_migrations (version) VALUES (4)")
                applied_now.append(4)

        return {"applied": applied_now, "latest_version": LATEST_VERSION}
    finally:
        connection.close()


if __name__ == "__main__":
    # Run explicitly from the live service's deployment console when needed.
    # Credentials are read only from DB_* environment variables and never printed.
    from database import test_connection

    info = test_connection()
    print("Connected to configured MySQL database.")
    print("MySQL version:", info.get("database_version"))
    result = run_migrations()
    print("Migration result:", result)

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version FROM punter_schema_migrations ORDER BY version")
            versions = [int(row["version"]) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name='matches' "
                "AND column_name='last_synced_at'"
            )
            last_synced_present = int(cursor.fetchone()["n"]) > 0
        if versions and max(versions) >= LATEST_VERSION:
            print("Schema migration versions:", versions)
        else:
            raise SystemExit("Migration verification failed: latest version is not recorded.")
        if last_synced_present:
            print("matches.last_synced_at: present")
        else:
            print("matches.last_synced_at: table absent or optional column not applicable")
    finally:
        connection.close()
