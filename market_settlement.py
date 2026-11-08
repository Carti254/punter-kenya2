'''Fail-closed market grading from API-Football match evidence.

Only standard, unambiguous markets are supported. Provider gaps, bookmaker-specific
card points, Asian quarter lines and ambiguous player selections remain pending.
'''
import re


def _number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(value))
    return float(match.group(1)) if match else None


def _stat_rows(rows):
    result = {}
    for team in rows or []:
        name = str(((team.get("team") or {}).get("name")) or "").casefold()
        stats = {}
        for item in team.get("statistics") or []:
            key = re.sub(r"[^a-z]", "", str(item.get("type") or "").casefold())
            value = _number(item.get("value"))
            if key and value is not None:
                stats[key] = value
        if name:
            result[name] = stats
    return result


def _line_pick(selection):
    match = re.fullmatch(r"\s*(over|under|o|u)\s*(\d+(?:\.\d+)?)\s*", str(selection), re.I)
    if not match:
        raise ValueError("Use a standard Over/Under selection such as Over 8.5.")
    side = "over" if match.group(1).casefold() in {"over", "o"} else "under"
    line = float(match.group(2))
    if line % 1 == 0:
        raise ValueError("Integer-line push settlement requires bookmaker-specific rules; left pending.")
    return side, line


def _grade_total(value, selection):
    if value is None:
        raise ValueError("Required statistic is not supplied by the provider; left pending.")
    side, line = _line_pick(selection)
    return "WON" if (value > line if side == "over" else value < line) else "LOST"


def grade_market_evidence(market, selection, evidence):
    """Grade standard HT, corners and explicit card-count markets.

    evidence is the normalized object returned by football_stats_provider.
    """
    key = re.sub(r"[^a-z0-9]", "", str(market).casefold())
    pick = str(selection).strip().casefold()
    fixture = evidence.get("fixture") or {}
    score = fixture.get("score") or {}
    halftime = score.get("halftime") or {}
    ht_home, ht_away = _number(halftime.get("home")), _number(halftime.get("away"))

    if key in {"halftimeresult", "ht1x2", "halftime1x2"}:
        if ht_home is None or ht_away is None:
            raise ValueError("Half-time score is unavailable; left pending.")
        actual = "home" if ht_home > ht_away else "draw" if ht_home == ht_away else "away"
        wanted = "home" if pick in {"1", "home"} else "draw" if pick in {"x", "draw"} else "away" if pick in {"2", "away"} else None
        if wanted is None:
            raise ValueError("Unsupported half-time result selection.")
        return "WON" if wanted == actual else "LOST"

    if key in {"halftimeoverunder", "halftimetotalgoals", "htoverunder"}:
        if ht_home is None or ht_away is None:
            raise ValueError("Half-time score is unavailable; left pending.")
        return _grade_total(ht_home + ht_away, selection)

    if "player" in key or "goalscorer" in key:
        if "shotontarget" in key:
            stat_path, stat_label = ("shots", "on"), "player shots on target"
        elif "shot" in key:
            stat_path, stat_label = ("shots", "total"), "player shots"
        elif "assist" in key:
            stat_path, stat_label = ("goals", "assists"), "player assists"
        elif "yellow" in key and "card" in key:
            stat_path, stat_label = ("cards", "yellow"), "player yellow cards"
        elif "red" in key and "card" in key:
            stat_path, stat_label = ("cards", "red"), "player red cards"
        elif "goal" in key or "goalscorer" in key:
            stat_path, stat_label = ("goals", "total"), "player goals"
        else:
            raise ValueError("Player market type is not supported; left pending.")

        groups = evidence.get("players") or []
        candidates = []
        for group in groups:
            for item in group.get("players") or []:
                player = item.get("player") or {}
                stats_list = item.get("statistics") or []
                if player.get("name") and stats_list:
                    candidates.append((str(player["name"]), stats_list[0]))
        if "over" in pick or "under" in pick or re.search(r"\b[ou]\s*\d", pick):
            match = re.fullmatch(r"\s*(.+?)\s+(over|under|o|u)\s*(\d+(?:\.\d+)?)\s*", str(selection), re.I)
            if not match:
                raise ValueError("Player selection must include an exact name and Over/Under line.")
            player_name, side_text, line_text = match.groups()
            matching = [row for row in candidates if re.sub(r"[^a-z0-9]", "", row[0].casefold()) == re.sub(r"[^a-z0-9]", "", player_name.casefold())]
            if len(matching) != 1:
                raise ValueError("Player name did not resolve to exactly one provider player; left pending.")
            stat = matching[0][1]
            value = ((stat.get(stat_path[0]) or {}).get(stat_path[1]))
            if value is None:
                raise ValueError(f"Provider omitted {stat_label}; left pending.")
            side = "over" if side_text.casefold() in {"over", "o"} else "under"
            line = float(line_text)
            if line % 1 == 0:
                raise ValueError("Integer-line player market requires bookmaker-specific push rules; left pending.")
            return "WON" if (float(value) > line if side == "over" else float(value) < line) else "LOST"

        if "goalscorer" not in key and "goal" not in key:
            raise ValueError("Player selection needs an explicit Over/Under line; left pending.")
        player_name = re.sub(r"\s+(to score|anytime|first goalscorer)$", "", str(selection).strip(), flags=re.I)
        matching = [row for row in candidates if re.sub(r"[^a-z0-9]", "", row[0].casefold()) == re.sub(r"[^a-z0-9]", "", player_name.casefold())]
        if len(matching) != 1:
            raise ValueError("Player name did not resolve to exactly one provider player; left pending.")
        value = ((matching[0][1].get("goals") or {}).get("total"))
        if value is None:
            raise ValueError("Provider omitted player goal data; left pending.")
        return "WON" if float(value) > 0 else "LOST"

    full_rows = _stat_rows(evidence.get("statistics_full_time"))
    half_rows = _stat_rows(evidence.get("statistics_half_time"))
    if ("half" in key or key.startswith("ht")) and "corner" in key:
        stat_key, rows = "cornerkicks", half_rows
    elif "corner" in key:
        stat_key, rows = "cornerkicks", full_rows
    elif "yellow" in key and "card" in key:
        stat_key, rows = "yellowcards", full_rows
    elif "red" in key and "card" in key:
        stat_key, rows = "redcards", full_rows
    else:
        raise ValueError("Market is not yet supported by the evidence grader; left pending.")

    if not rows:
        raise ValueError("Required match statistics are unavailable; left pending.")
    values = [team.get(stat_key) for team in rows.values()]
    if any(value is None for value in values):
        raise ValueError("Provider omitted one or more required team statistics; left pending.")
    return _grade_total(sum(values), selection)



def settle_bet_with_market_evidence(user_id, bet_id, evidence, source="api-football"):
    """Settle one pending record with explicit provider evidence and audit payload."""
    import json
    from decimal import Decimal
    from database import get_connection
    from results_tracker import grade_selection

    fixture = evidence.get("fixture") or {}
    status = ((fixture.get("fixture") or {}).get("status") or {}).get("short")
    if status not in {"FT", "AET", "PEN"}:
        raise ValueError("Provider fixture is not confirmed finished; left pending.")
    goals = fixture.get("goals") or {}
    fulltime = ((fixture.get("score") or {}).get("fulltime") or {})
    home_goals = goals.get("home") if goals.get("home") is not None else fulltime.get("home")
    away_goals = goals.get("away") if goals.get("away") is not None else fulltime.get("away")
    if home_goals is None or away_goals is None:
        raise ValueError("Confirmed full-time score is missing; left pending.")
    home_goals, away_goals = int(home_goals), int(away_goals)

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM punter_bet_records WHERE id=%s AND user_id=%s AND status='PENDING'",
                (int(bet_id), int(user_id)),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Pending bet record not found.")
            try:
                result = grade_selection(row["market"], row["selection"], home_goals, away_goals,
                                         row["home_team"], row["away_team"])
            except ValueError:
                result = grade_market_evidence(row["market"], row["selection"], evidence)
            stake = Decimal(str(row["stake"]))
            odds = Decimal(str(row["decimal_odds"]))
            pnl = stake * (odds - 1) if result == "WON" else Decimal("0") if result == "VOID" else -stake
            audit = json.dumps(evidence, separators=(",", ":"), default=str)
            cursor.execute(
                """UPDATE punter_bet_records
                   SET status=%s, actual_home_goals=%s, actual_away_goals=%s,
                       profit_loss=%s, settlement_source=%s,
                       settlement_evidence_json=%s, settled_at=CURRENT_TIMESTAMP
                   WHERE id=%s AND user_id=%s AND status='PENDING'""",
                (result, home_goals, away_goals, str(pnl), str(source)[:40],
                 audit, int(bet_id), int(user_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Bet record changed during settlement; refresh and retry.")
            return {"status": result, "profit_loss": pnl}
    finally:
        connection.close()
