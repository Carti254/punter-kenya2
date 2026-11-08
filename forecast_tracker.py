"""Persist and score model forecasts without inventing match outcomes.

Forecast outcomes are only written after an explicit result is supplied or a trusted
settlement adapter calls settle_forecast. Prices are optional and missing odds stay NULL.
"""
from __future__ import annotations

import math
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
    finally:
        connection.close()
