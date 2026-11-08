"""Premium tracking: persistent odds observations, closing-line value and rollover progress.

All bookmaker access is read-only. CLV is calculated only where an exact
fixture/market/selection match exists; missing observations are reported, not guessed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from database import get_connection


def save_price_observations(games):
    if not games:
        return 0
    connection = get_connection()
    saved = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS punter_odds_snapshots (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    bookmaker VARCHAR(40) NOT NULL,
                    event_id VARCHAR(100) NOT NULL,
                    home_team VARCHAR(180) NOT NULL,
                    away_team VARCHAR(180) NOT NULL,
                    kickoff_eat VARCHAR(32) NULL,
                    market_name VARCHAR(180) NOT NULL,
                    selection_name VARCHAR(180) NOT NULL,
                    handicap_value VARCHAR(80) NULL,
                    decimal_odds DECIMAL(10,3) NOT NULL,
                    fetched_at DATETIME NOT NULL,
                    INDEX idx_odds_event (bookmaker, event_id),
                    INDEX idx_odds_fixture (home_team, away_team),
                    INDEX idx_odds_time (fetched_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            captured = datetime.now(timezone.utc).replace(tzinfo=None)
            for game in games:
                for market in game.get("markets") or []:
                    try:
                        odds = float(market.get("odds"))
                    except (TypeError, ValueError):
                        continue
                    if not 1 < odds < 10000:
                        continue
                    cursor.execute("""
                        INSERT INTO punter_odds_snapshots
                        (bookmaker,event_id,home_team,away_team,kickoff_eat,
                         market_name,selection_name,handicap_value,decimal_odds,fetched_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        str(game.get("bookmaker") or "Unknown")[:40],
                        str(game.get("event_id") or "")[:100],
                        str(game.get("home") or "Unknown")[:180],
                        str(game.get("away") or "Unknown")[:180],
                        str(game.get("kickoff_eat") or "")[:32] or None,
                        str(market.get("market") or "Unknown")[:180],
                        str(market.get("selection") or "Unknown")[:180],
                        str(market.get("handicap"))[:80] if market.get("handicap") is not None else None,
                        odds, captured,
                    ))
                    saved += 1
        return saved
    finally:
        connection.close()


def capture_current_prices(day=None):
    """Fetch current read-only feeds and persist every returned price."""
    from bookmaker_odds import fetch_all
    result = fetch_all(day)
    games = []
    for provider in (result.get("bookmakers") or {}).values():
        games.extend(provider.get("games") or [])
    return {
        "saved": save_price_observations(games),
        "events": len(games),
        "errors": result.get("errors") or {},
    }


def closing_line_value(user_id):
    """Compare each logged bet with the latest exact matching price on its match date."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, bookmaker, home_team, away_team, market, selection,
                       decimal_odds, match_date, created_at
                FROM punter_bet_records
                WHERE user_id=%s AND status IN ('WON','LOST')
                ORDER BY match_date DESC, id DESC
                LIMIT 500
            """, (int(user_id),))
            bets = cursor.fetchall()
            rows = []
            for bet in bets:
                cursor.execute("""
                    SELECT bookmaker, decimal_odds, fetched_at
                    FROM punter_odds_snapshots
                    WHERE LOWER(home_team)=LOWER(%s)
                      AND LOWER(away_team)=LOWER(%s)
                      AND LOWER(market_name)=LOWER(%s)
                      AND LOWER(selection_name)=LOWER(%s)
                      AND fetched_at <= DATE_ADD(%s, INTERVAL 1 DAY)
                    ORDER BY fetched_at DESC LIMIT 1
                """, (bet["home_team"], bet["away_team"], bet["market"],
                      bet["selection"], bet["match_date"]))
                close = cursor.fetchone()
                if not close:
                    continue
                taken = float(bet["decimal_odds"])
                closing = float(close["decimal_odds"])
                rows.append({
                    "bet_id": int(bet["id"]), "bookmaker": close["bookmaker"],
                    "taken_odds": taken, "closing_odds": closing,
                    "clv": (1.0 / closing) - (1.0 / taken),
                    "captured_at": str(close["fetched_at"]),
                })
        if not rows:
            return {"matched": 0, "bets_checked": len(bets), "average_clv": None, "positive": 0, "negative": 0}
        values = [r["clv"] for r in rows]
        return {
            "matched": len(rows), "bets_checked": len(bets),
            "average_clv": sum(values) / len(values),
            "positive": sum(v > 0 for v in values),
            "negative": sum(v < 0 for v in values),
            "observations": rows[:20],
        }
    finally:
        connection.close()


def rollover_status(user_id):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT rollover_days,rollover_target,rollover_started_on,updated_at FROM punter_user_settings WHERE user_id=%s", (int(user_id),))
            plan = cursor.fetchone()
            if not plan or not plan.get("rollover_days"):
                return {"configured": False}
            cursor.execute("""
                SELECT day_number,result,actual_odds,recorded_at
                FROM punter_rollover_progress
                WHERE user_id=%s AND plan_started=%s
                ORDER BY day_number
            """, (int(user_id), plan.get("rollover_started_on") or plan["updated_at"].date()))
            entries = cursor.fetchall()
        completed = len(entries)
        wins = sum(r["result"] == "WON" for r in entries)
        losses = sum(r["result"] == "LOST" for r in entries)
        return {
            "configured": True, "days": int(plan["rollover_days"]),
            "target": float(plan["rollover_target"]), "completed": completed,
            "remaining": max(0, int(plan["rollover_days"]) - completed),
            "wins": wins, "losses": losses, "entries": entries,
            "status": "BROKEN" if losses or any(r["result"] == "MISSED" for r in entries) else "COMPLETED" if completed >= int(plan["rollover_days"]) else "IN_PROGRESS",
        }
    finally:
        connection.close()


def record_rollover_day(user_id, result, actual_odds=None):
    result = str(result).strip().upper()
    if result not in {"WON", "LOST", "MISSED"}:
        raise ValueError("Result must be WON, LOST or MISSED.")
    odds = None if actual_odds is None else float(actual_odds)
    if odds is not None and (odds <= 1 or odds >= 10000):
        raise ValueError("Actual odds must be greater than 1.00.")
    status = rollover_status(user_id)
    if not status["configured"]:
        raise ValueError("Set a rollover plan first with /rollover DAYS TARGET_ODDS.")
    if status["status"] != "IN_PROGRESS":
        raise ValueError("This rollover is already broken or completed. Start a new plan.")
    day_number = status["completed"] + 1
    if day_number > status["days"]:
        raise ValueError("All rollover days are already recorded.")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT updated_at FROM punter_user_settings WHERE user_id=%s", (int(user_id),))
            plan_started = cursor.fetchone()["updated_at"].date()
            cursor.execute("""
                INSERT INTO punter_rollover_progress
                (user_id,plan_started,day_number,target_odds,result,actual_odds)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (int(user_id), plan_started, day_number, status["target"], result, odds))
        return {"day": day_number, "days": status["days"], "result": result, "status": "BROKEN" if result in {"LOST", "MISSED"} else "IN_PROGRESS"}
    finally:
        connection.close()
