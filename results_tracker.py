"""Auditable bet logging and settlement helpers.

Prediction accuracy and betting returns are deliberately kept separate.
"""
from decimal import Decimal, InvalidOperation
from database import get_connection


def grade_selection(market, selection, home_goals, away_goals):
    market_key = "".join(ch for ch in str(market).casefold() if ch.isalnum())
    pick = str(selection).strip().casefold()
    total = int(home_goals) + int(away_goals)
    home, away = int(home_goals), int(away_goals)

    if market_key in {"1x2", "matchresult", "fulltimeresult", "result"}:
        actual = "home" if home > away else "draw" if home == away else "away"
        wanted = "home" if pick in {"1", "home"} else "draw" if pick in {"x", "draw"} else "away" if pick in {"2", "away"} else None
        if wanted is None:
            raise ValueError("Unsupported 1X2 selection.")
        return "WON" if wanted == actual else "LOST"

    if market_key in {"overunder", "totalgoals", "goals"}:
        import re
        match = re.fullmatch(r"(over|under|o|u)\\s*(\\d+(?:\\.\\d+)?)", pick)
        if not match:
            raise ValueError("Use a selection such as Over 2.5 or Under 3.5.")
        side = "over" if match.group(1) in {"over", "o"} else "under"
        line = Decimal(match.group(2))
        won = Decimal(total) > line if side == "over" else Decimal(total) < line
        push = Decimal(total) == line
        return "VOID" if push else "WON" if won else "LOST"

    raise ValueError("This tracker currently settles 1X2 and standard total-goals markets only.")


def record_bet(user_id, bookmaker, home, away, market, selection, odds, stake, match_date=None):
    try:
        price, amount = Decimal(str(odds)), Decimal(str(stake))
    except InvalidOperation:
        raise ValueError("Odds and stake must be valid numbers.")
    if price <= 1 or amount <= 0:
        raise ValueError("Decimal odds must exceed 1.00 and stake must be positive.")
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


def settle_bet(user_id, bet_id, home_goals, away_goals):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT * FROM punter_bet_records
                WHERE id=%s AND user_id=%s AND status='PENDING'
            """, (int(bet_id), int(user_id)))
            row = cursor.fetchone()
            if not row:
                raise ValueError("Pending bet record not found.")
            status = grade_selection(row["market"], row["selection"], home_goals, away_goals)
            stake, odds = Decimal(str(row["stake"])), Decimal(str(row["decimal_odds"]))
            pnl = (stake * (odds - 1)) if status == "WON" else Decimal("0") if status == "VOID" else -stake
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
                       COALESCE(SUM(CASE WHEN status IN ('WON','LOST','VOID') THEN stake ELSE 0 END),0) AS settled_stakes
                FROM punter_bet_records WHERE user_id=%s
            """, (int(user_id),))
            return cursor.fetchone()
    finally:
        connection.close()
