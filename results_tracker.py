"""Auditable bet logging, automatic result matching and settlement helpers."""
from decimal import Decimal, InvalidOperation
from database import get_connection


def grade_selection(market, selection, home_goals, away_goals, home_team=None, away_team=None):
    home, away = int(home_goals), int(away_goals)
    if home < 0 or away < 0:
        raise ValueError("Full-time goals cannot be negative.")
    total = home + away
    market_key = "".join(ch for ch in str(market).casefold() if ch.isalnum())
    pick = str(selection).strip().casefold()
    if market_key in {"1x2", "matchresult", "fulltimeresult", "result"}:
        actual = "home" if home > away else "draw" if home == away else "away"
        home_names, away_names = {"1", "home"}, {"2", "away"}
        if home_team:
            home_names.add(str(home_team).strip().casefold())
        if away_team:
            away_names.add(str(away_team).strip().casefold())
        wanted = "home" if pick in home_names else "draw" if pick in {"x", "draw"} else "away" if pick in away_names else None
        if wanted is None:
            raise ValueError("Unsupported 1X2 selection.")
        return "WON" if wanted == actual else "LOST"
    if market_key in {"overunder", "totalgoals", "goals"}:
        import re
        match = re.fullmatch(r"(over|under|o|u)\s*(\d+(?:\.\d+)?)", pick)
        if not match:
            raise ValueError("Use a selection such as Over 2.5 or Under 3.5.")
        side = "over" if match.group(1) in {"over", "o"} else "under"
        line = Decimal(match.group(2))
        won = Decimal(total) > line if side == "over" else Decimal(total) < line
        return "VOID" if Decimal(total) == line else "WON" if won else "LOST"
    raise ValueError("This tracker currently settles 1X2 and standard total-goals markets only.")


def record_bet(user_id, bookmaker, home, away, market, selection, odds, stake, match_date=None):
    try:
        price, amount = Decimal(str(odds)), Decimal(str(stake))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Odds and stake must be valid numbers.") from None
    if not price.is_finite() or not amount.is_finite() or price <= 1 or amount <= 0:
        raise ValueError("Decimal odds must be finite and exceed 1.00; stake must be finite and positive.")
    fixture_key = "|".join([str(match_date or ""), str(home).casefold(), str(away).casefold()])
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO punter_bet_records
                (user_id, bookmaker, fixture_key, match_date, home_team, away_team,
                 market, selection, decimal_odds, stake)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (int(user_id), str(bookmaker)[:40], fixture_key[:500], match_date,
                  str(home)[:180], str(away)[:180], str(market)[:120],
                  str(selection)[:180], str(price), str(amount)))
            return int(cursor.lastrowid)
    finally:
        connection.close()


def list_pending_bets(user_id=None):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            if user_id is None:
                cursor.execute("SELECT * FROM punter_bet_records WHERE status='PENDING' ORDER BY match_date ASC, id ASC")
            else:
                cursor.execute("SELECT * FROM punter_bet_records WHERE status='PENDING' AND user_id=%s ORDER BY match_date ASC, id ASC", (int(user_id),))
            return cursor.fetchall()
    finally:
        connection.close()


def settle_bet(user_id, bet_id, home_goals, away_goals):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM punter_bet_records WHERE id=%s AND user_id=%s AND status='PENDING'", (int(bet_id), int(user_id)))
            row = cursor.fetchone()
            if not row:
                raise ValueError("Pending bet record not found.")
            status = grade_selection(row["market"], row["selection"], home_goals, away_goals,
                                     row["home_team"], row["away_team"])
            stake, odds = Decimal(str(row["stake"])), Decimal(str(row["decimal_odds"]))
            pnl = stake * (odds - 1) if status == "WON" else Decimal("0") if status == "VOID" else -stake
            cursor.execute("""
                UPDATE punter_bet_records
                SET status=%s, actual_home_goals=%s, actual_away_goals=%s,
                    profit_loss=%s, settled_at=CURRENT_TIMESTAMP
                WHERE id=%s AND user_id=%s AND status='PENDING'
            """, (status, int(home_goals), int(away_goals), str(pnl), int(bet_id), int(user_id)))
            if cursor.rowcount != 1:
                raise RuntimeError("Bet record changed during settlement; refresh and retry.")
            return {"status": status, "profit_loss": pnl}
    finally:
        connection.close()


def bet_statistics(user_id):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) AS total,
                       SUM(status='PENDING') AS pending,
                       SUM(status='WON') AS won,
                       SUM(status='LOST') AS lost,
                       SUM(status='VOID') AS voided,
                       COALESCE(SUM(profit_loss),0) AS profit_loss,
                       COALESCE(SUM(CASE WHEN status IN ('WON','LOST') THEN stake ELSE 0 END),0) AS settled_stakes
                FROM punter_bet_records WHERE user_id=%s
            """, (int(user_id),))
            return cursor.fetchone()
    finally:
        connection.close()
