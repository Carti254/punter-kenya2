"""Persist and score model forecasts without inventing match outcomes."""
from __future__ import annotations

import math
import os
import re
from database import get_connection


def record_forecast(fixture_key, market, selection, probability, decimal_odds=None,
                   model_version="specialist-poisson-v1"):
    probability = float(probability)
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("Forecast probability must be between 0 and 1.")
    odds = None if decimal_odds is None else float(decimal_odds)
    if odds is not None and (not math.isfinite(odds) or odds <= 1 or odds >= 10000):
        raise ValueError("Decimal odds must be greater than 1.00.")
    if not str(fixture_key).strip() or not str(market).strip() or not str(selection).strip():
        raise ValueError("Fixture, market and selection are required.")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id FROM punter_forecast_results
                WHERE fixture_key=%s AND market=%s AND selection=%s
                  AND model_version=%s AND forecast_at >= UTC_DATE()
                ORDER BY id DESC LIMIT 1
            """, (str(fixture_key)[:500], str(market)[:120], str(selection)[:180],
                  str(model_version)[:80]))
            existing = cursor.fetchone()
            if existing:
                return int(existing["id"])
            cursor.execute("""
                INSERT INTO punter_forecast_results
                (fixture_key,market,selection,probability,decimal_odds,model_version)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (str(fixture_key)[:500], str(market)[:120], str(selection)[:180],
                  probability, odds, str(model_version)[:80]))
            return int(cursor.lastrowid)
    finally:
        connection.close()


def settle_forecast(forecast_id, outcome):
    raw = str(outcome).strip().upper()
    if raw not in {"WON", "LOST"}:
        raise ValueError("Outcome must be WON or LOST.")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE punter_forecast_results
                SET outcome=%s, settled_at=CURRENT_TIMESTAMP
                WHERE id=%s AND outcome IS NULL
            """, (1 if raw == "WON" else 0, int(forecast_id)))
            if cursor.rowcount != 1:
                raise ValueError("Forecast not found or already settled.")
        return {"id": int(forecast_id), "outcome": raw}
    finally:
        connection.close()


def _binary_market_outcome(market, selection, actual):
    match = re.fullmatch(r"\s*(over|under)\s+(\d+(?:\.\d+)?)\s*", str(selection), re.I)
    if not match:
        return None
    direction, line = match.group(1).casefold(), float(match.group(2))
    if actual == line:
        return None  # push/void is not represented as a win or loss
    won = actual > line if direction == "over" else actual < line
    return 1 if won else 0


def refresh_specialist_forecasts(api_key=None, session=None, limit=300):
    """Settle stored specialist over/under forecasts only from completed API-Football fixtures."""
    import requests
    from football_stats_provider import _get
    session = session or requests
    key = api_key or os.getenv("API_FOOTBALL_KEY")
    if not key:
        return {"checked": 0, "settled": 0, "pending": 0, "unavailable": "API_FOOTBALL_KEY not configured"}
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id,fixture_key,market,selection
                FROM punter_forecast_results
                WHERE outcome IS NULL AND fixture_key LIKE 'api-football:%'
                ORDER BY forecast_at LIMIT %s
            """, (max(1, min(int(limit), 1000)),))
            forecasts = cursor.fetchall()
    finally:
        connection.close()
    fixture_ids = sorted({int(str(row["fixture_key"]).split(":", 1)[1]) for row in forecasts
                          if str(row["fixture_key"]).split(":", 1)[-1].isdigit()})
    settled_ids, seen = set(), {}
    stat_aliases = {
        "corners": ("corner kicks", "corners"),
        "cards": ("yellow cards",),
        "shots": ("total shots", "shots total"),
        "shots_on_target": ("shots on goal", "shots on target"),
    }
    for fixture_id in fixture_ids:
        fixture_rows = _get("fixtures", {"id": fixture_id}, key, session)
        if len(fixture_rows) != 1:
            continue
        fixture = fixture_rows[0]
        status = ((fixture.get("fixture") or {}).get("status") or {}).get("short")
        if status not in {"FT", "AET", "PEN"}:
            continue
        teams = fixture.get("teams") or {}
        home_id = (teams.get("home") or {}).get("id")
        away_id = (teams.get("away") or {}).get("id")
        if not home_id or not away_id:
            continue
        stat_rows = _get("fixtures/statistics", {"fixture": fixture_id}, key, session)
        by_id = {}
        for team_row in stat_rows:
            team_id = (team_row.get("team") or {}).get("id")
            values = {}
            for item in team_row.get("statistics") or []:
                try:
                    values[str(item.get("type") or "").casefold()] = float(str(item.get("value")).replace("%", ""))
                except (TypeError, ValueError):
                    continue
            by_id[team_id] = values
        for row in forecasts:
            if row["fixture_key"] != f"api-football:{fixture_id}":
                continue
            market = str(row["market"]).casefold()
            if market == "half_time_goals":
                half = (fixture.get("score") or {}).get("halftime") or {}
                if half.get("home") is None or half.get("away") is None:
                    continue
                actual = float(half["home"]) + float(half["away"])
            elif market in stat_aliases:
                aliases = stat_aliases[market]
                home_values, away_values = by_id.get(home_id, {}), by_id.get(away_id, {})
                h = next((home_values[a] for a in aliases if a in home_values), None)
                a = next((away_values[a] for a in aliases if a in away_values), None)
                if h is None or a is None:
                    continue
                actual = h + a
            else:
                continue
            outcome = _binary_market_outcome(market, row["selection"], actual)
            if outcome is not None:
                seen[int(row["id"])] = outcome
    if seen:
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                for forecast_id, outcome in seen.items():
                    cursor.execute("""
                        UPDATE punter_forecast_results
                        SET outcome=%s, settled_at=CURRENT_TIMESTAMP
                        WHERE id=%s AND outcome IS NULL
                    """, (outcome, forecast_id))
                    if cursor.rowcount:
                        settled_ids.add(forecast_id)
        finally:
            connection.close()
    return {
        "checked": len(forecasts), "settled": len(settled_ids),
        "pending": max(0, len(forecasts) - len(settled_ids)),
    }


def forecast_performance(limit=1000):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id,fixture_key,market,selection,probability,decimal_odds,
                       outcome,model_version,forecast_at,settled_at
                FROM punter_forecast_results
                WHERE outcome IS NOT NULL
                ORDER BY settled_at DESC,id DESC LIMIT %s
            """, (max(1, min(int(limit), 5000)),))
            rows = cursor.fetchall()
        if not rows:
            return {"count": 0, "brier": None, "log_loss": None, "by_market": {}}
        eps = 1e-15
        errors = []
        groups = {}
        for row in rows:
            p = max(eps, min(1 - eps, float(row["probability"])))
            y = int(row["outcome"])
            errors.append((p-y)**2)
            groups.setdefault(row["market"], []).append((p-y)**2)
        return {
            "count": len(rows),
            "brier": sum(errors) / len(errors),
            "log_loss": -sum(
                (int(r["outcome"]) * math.log(max(eps, min(1-eps, float(r["probability"]))))
                 + (1-int(r["outcome"])) * math.log(max(eps, min(1-eps, 1-float(r["probability"])))))
                for r in rows
            ) / len(rows),
            "by_market": {k: {"count": len(v), "brier": sum(v)/len(v)} for k, v in groups.items()},
        }
