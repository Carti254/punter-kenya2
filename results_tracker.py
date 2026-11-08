"""Auditable bet logging, automatic result matching and settlement helpers."""
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone
from database import get_connection


def grade_selection(market, selection, home_goals, away_goals, home_team=None, away_team=None):
    """Grade supported full-time markets from a confirmed score.

    Asian quarter-lines, cards/corners, half-time and player markets are deliberately
    rejected because a full-time score alone cannot settle them reliably.
    """
    import re

    home, away = int(home_goals), int(away_goals)
    if home < 0 or away < 0:
        raise ValueError("Full-time goals cannot be negative.")
    total = home + away
    key = "".join(ch for ch in str(market).casefold() if ch.isalnum())
    pick = re.sub(r"\s+", " ", str(selection).strip().casefold())

    if key in {"1x2", "matchresult", "fulltimeresult", "result", "matchwinner"}:
        actual = "home" if home > away else "draw" if home == away else "away"
        home_names = {"1", "home"}
        away_names = {"2", "away"}
        if home_team:
            home_names.add(str(home_team).strip().casefold())
        if away_team:
            away_names.add(str(away_team).strip().casefold())
        wanted = "home" if pick in home_names else "draw" if pick in {"x", "draw"} else "away" if pick in away_names else None
        if wanted is None:
            raise ValueError("Unsupported 1X2 selection.")
        return "WON" if wanted == actual else "LOST"

    if key in {"doublechance", "matchdoublechance"}:
        actual = "1" if home > away else "x" if home == away else "2"
        allowed = {"1x": {"1", "x"}, "x1": {"1", "x"},
                   "12": {"1", "2"}, "21": {"1", "2"},
                   "x2": {"x", "2"}, "2x": {"x", "2"}}
        if pick not in allowed:
            raise ValueError("Use double-chance selection 1X, 12 or X2.")
        return "WON" if actual in allowed[pick] else "LOST"

    if key in {"drawnobet", "dnb"}:
        if pick in {"1", "home"} or (home_team and pick == home_team.strip().casefold()):
            side = "home"
        elif pick in {"2", "away"} or (away_team and pick == away_team.strip().casefold()):
            side = "away"
        else:
            raise ValueError("Draw No Bet selection must be Home or Away.")
        if home == away:
            return "VOID"
        return "WON" if (home > away) == (side == "home") else "LOST"

    if key in {"overunder", "totalgoals", "goals", "total"}:
        match = re.fullmatch(r"(over|under|o|u)\s*(\d+(?:\.\d+)?)", pick)
        if not match:
            raise ValueError("Use a selection such as Over 2.5 or Under 3.5.")
        side = "over" if match.group(1) in {"over", "o"} else "under"
        line = Decimal(match.group(2))
        won = Decimal(total) > line if side == "over" else Decimal(total) < line
        return "VOID" if Decimal(total) == line else "WON" if won else "LOST"

    if key in {"bothteamstoscore", "btts", "ggng"}:
        if pick not in {"yes", "no"}:
            raise ValueError("Both Teams To Score selection must be Yes or No.")
        actual = home > 0 and away > 0
        return "WON" if actual == (pick == "yes") else "LOST"

    if key in {"oddoreven", "totalgoalsoddoreven", "totaloddoreven"}:
        if pick not in {"odd", "even"}:
            raise ValueError("Selection must be Odd or Even.")
        return "WON" if (total % 2 == 1) == (pick == "odd") else "LOST"

    if key in {"correctscore", "exactscore"}:
        match = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", pick)
        if not match:
            raise ValueError("Correct-score selection must look like 2-1.")
        return "WON" if (home, away) == (int(match.group(1)), int(match.group(2))) else "LOST"

    raise ValueError("Unsupported market for full-time-score settlement. This market is left pending rather than guessed.")


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


def utc_match_date(kickoff):
    """Return the UTC calendar date used by open-matchday result CSVs."""
    if isinstance(kickoff, date) and not isinstance(kickoff, datetime):
        return kickoff
    if isinstance(kickoff, str):
        kickoff = datetime.fromisoformat(kickoff.strip().replace("Z", "+00:00"))
    if not isinstance(kickoff, datetime):
        raise TypeError("Kick-off must be a date, datetime or ISO timestamp.")
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    else:
        kickoff = kickoff.astimezone(timezone.utc)
    return kickoff.date()
