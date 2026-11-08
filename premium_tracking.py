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
    """Fetch and persist read-only prices for today and tomorrow in Nairobi time.

    An explicit day limits capture to that date; the scheduled job captures both
    dates. Provider errors are returned per date and never hide successful feeds.
    """
    from bookmaker_odds import fetch_all
    if day is None:
        today = datetime.now(ZoneInfo("Africa/Nairobi")).date()
        days = (today, today + timedelta(days=1))
    else:
        days = (day,)
    games, errors, event_count = [], {}, 0
    for target_day in days:
        result = fetch_all(target_day)
        date_games = []
        for provider in (result.get("bookmakers") or {}).values():
            date_games.extend(provider.get("games") or [])
        games.extend(date_games)
        event_count += len(date_games)
        errors[target_day.isoformat()] = result.get("errors") or {}
    saved = save_price_observations(games)
    return {
        "saved": saved,
        "events": event_count,
        "dates": [d.isoformat() for d in days],
        "errors": errors,
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
                    SELECT bookmaker, decimal_odds, fetched_at, kickoff_eat
                    FROM punter_odds_snapshots
                    WHERE LOWER(home_team)=LOWER(%s)
                      AND LOWER(away_team)=LOWER(%s)
                      AND LOWER(market_name)=LOWER(%s)
                      AND LOWER(selection_name)=LOWER(%s)
                      AND LOWER(bookmaker)=LOWER(%s)
                      AND fetched_at >= %s
                      AND fetched_at <= DATE_ADD(%s, INTERVAL 1 DAY)
                    ORDER BY fetched_at DESC
                """, (bet["home_team"], bet["away_team"], bet["market"],
                      bet["selection"], bet["bookmaker"], bet["created_at"], bet["match_date"]))
                snapshots = cursor.fetchall()
                close = None
                # "Closing" means the last observed price before kick-off, never
                # a post-match quote. Snapshot timestamps are stored as UTC-naive;
                # kickoff_eat is stored in Nairobi local time.
                for snapshot in snapshots:
                    try:
                        kickoff_local = datetime.strptime(snapshot["kickoff_eat"], "%Y-%m-%d %H:%M").replace(
                            tzinfo=ZoneInfo("Africa/Nairobi"))
                        kickoff_utc = kickoff_local.astimezone(timezone.utc).replace(tzinfo=None)
                    except (TypeError, ValueError):
                        continue
                    if snapshot["fetched_at"] < kickoff_utc:
                        close = snapshot
                        break
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
            plan_started = plan.get("rollover_started_on") or plan["updated_at"].date()
            # Any prior rollover day without an explicit result is a missed day.
            # Today remains open so the user can still record its outcome.
            today = datetime.now(ZoneInfo("Africa/Nairobi")).date()
            elapsed_days = max(0, (today - plan_started).days)
            missed_through = min(int(plan["rollover_days"]), elapsed_days)
            for day_number in range(1, missed_through + 1):
                cursor.execute("""
                    INSERT IGNORE INTO punter_rollover_progress
                    (user_id,plan_started,day_number,target_odds,result,actual_odds)
                    VALUES (%s,%s,%s,%s,'MISSED',NULL)
                """, (int(user_id), plan_started, day_number, plan["rollover_target"]))
            cursor.execute("""
                SELECT day_number,result,actual_odds,recorded_at
                FROM punter_rollover_progress
                WHERE user_id=%s AND plan_started=%s
                ORDER BY day_number
            """, (int(user_id), plan_started))
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
    if result == "WON" and (odds is None or odds < status["target"]):
        raise ValueError(f"A WON day requires actual combined odds of at least {status['target']:.2f}.")
    day_number = status["completed"] + 1
    if day_number > status["days"]:
        raise ValueError("All rollover days are already recorded.")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COALESCE(rollover_started_on,DATE(updated_at)) AS plan_started FROM punter_user_settings WHERE user_id=%s", (int(user_id),))
            plan_started = cursor.fetchone()["plan_started"]
            cursor.execute("""
                INSERT INTO punter_rollover_progress
                (user_id,plan_started,day_number,target_odds,result,actual_odds)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (int(user_id), plan_started, day_number, status["target"], result, odds))
        return {"day": day_number, "days": status["days"], "result": result, "status": "BROKEN" if result in {"LOST", "MISSED"} else "IN_PROGRESS"}
    finally:
        connection.close()
