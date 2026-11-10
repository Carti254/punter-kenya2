"""Eligibility gate for model-assessed, executable SportyBet selections."""
from __future__ import annotations
from collections import Counter
import math

MIN_VALUE_EDGE = 0.03
MIN_PROBABILITY = 0.55

def assess_candidate(row: dict, *args, **kwargs):
    try:
        odds = float(row.get("odds"))
        probability = float(row.get("model_probability"))
        value = float(row.get("expected_value"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(x) for x in (odds, probability, value)):
        return None
    if odds <= 1 or not 0 < probability <= 1:
        return None
    if value < MIN_VALUE_EDGE or probability < MIN_PROBABILITY:
        return None
    if str(row.get("bookmaker") or "").strip().casefold().replace(" ", "") != "sportybet":
        return None
    if not (row.get("eventId") or row.get("event_id") or row.get("sportybet_event_id")):
        return None
    if not (row.get("marketId") or row.get("market_id")):
        return None
    if not (row.get("outcomeId") or row.get("outcome_id")):
        return None
    if row.get("fixture_match_verified") is not True:
        return None
    text = " ".join(str(row.get(k) or "") for k in ("market", "selection", "specifier")).casefold()
    if any(x in text for x in ("btts", "both teams to score", "under 2.5", "draw no bet",
                               " dnb", "2up", "early payout", "handicap", "corner",
                               "card", "booking", "shots", "player", "correct score",
                               "half time", "first half")):
        return None
    return {**row, "odds": odds, "model_probability": probability,
            "expected_value": value,
            "selection_method": "Recent-results Poisson estimate; uncalibrated"}

def candidate_rejection_bucket(row: dict, *args, **kwargs):
    return "failed_model_value_or_execution_checks"

def prepare_candidate_pool(rows: list[dict], *args, **kwargs):
    pool = []
    reasons = Counter()
    for row in rows or []:
        item = assess_candidate(row)
        if item is None:
            reasons[candidate_rejection_bucket(row)] += 1
        else:
            pool.append(item)
    pool.sort(key=lambda x: (-float(x["expected_value"]), -float(x["model_probability"])))
    return pool, {"scanned": len(rows or []), "eligible": len(pool),
                  "rejected": sum(reasons.values()), "rejection_reasons": dict(reasons),
                  "research_scope": "Single recent-results Poisson probability and positive-value model."}

def candidate_research_summary(pool):
    return f"{len(pool or [])} SportyBet prices passed model probability, value and execution checks."

def enrich_with_specialist_models(rows, *args, **kwargs):
    return [dict(x) for x in (rows or [])], {"enabled": False, "reason": "No specialist models are used."}

def apply_walk_forward_calibration(rows, validation):
    return [dict(x) for x in (rows or [])], {"applied": False, "reason": "No calibration is claimed until sufficient settled forecasts exist."}
