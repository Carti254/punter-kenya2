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
    match = re.fullmatch(r"\\s*(\\d+(?:\\.\\d+)?)\\s*", str(value))
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
    match = re.fullmatch(r"\\s*(over|under|o|u)\\s*(\\d+(?:\\.\\d+)?)\\s*", str(selection), re.I)
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

    full_rows = _stat_rows(evidence.get("statistics_full_time"))
    half_rows = _stat_rows(evidence.get("statistics_half_time"))
    if "corner" in key:
        stat_key, rows = "cornerkicks", full_rows
    elif "yellow" in key and "card" in key:
        stat_key, rows = "yellowcards", full_rows
    elif "red" in key and "card" in key:
        stat_key, rows = "redcards", full_rows
    elif ("half" in key or key.startswith("ht")) and "corner" in key:
        stat_key, rows = "cornerkicks", half_rows
    else:
        raise ValueError("Market is not yet supported by the evidence grader; left pending.")

    if not rows:
        raise ValueError("Required match statistics are unavailable; left pending.")
    values = [team.get(stat_key) for team in rows.values()]
    if any(value is None for value in values):
        raise ValueError("Provider omitted one or more required team statistics; left pending.")
    return _grade_total(sum(values), selection)
