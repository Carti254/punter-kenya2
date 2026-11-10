"""Simple executable SportyBet price checks; no prediction model."""
from __future__ import annotations
from collections import Counter
import math

EXCLUDED = ("btts", "both teams to score", "under 2.5", "draw no bet", " dnb",
            "2up", "early payout", "handicap", "corner", "card", "booking",
            "shots", "player", "correct score", "half time", "first half", "second half")

def assess_candidate(row: dict, *args, **kwargs):
    try: odds = float(row.get("odds"))
    except (TypeError, ValueError, OverflowError): return None
    if not math.isfinite(odds) or not 1.15 <= odds <= 3.50: return None
    if str(row.get("bookmaker") or "").strip().casefold().replace(" ", "") != "sportybet": return None
    if not (row.get("eventId") or row.get("event_id")): return None
    if not (row.get("marketId") or row.get("market_id")): return None
    if not (row.get("outcomeId") or row.get("outcome_id")): return None
    if row.get("fixture_match_verified") is not True: return None
    if not str(row.get("home") or "").strip() or not str(row.get("away") or "").strip(): return None
    text = " ".join(str(row.get(k) or "") for k in ("market", "selection", "specifier")).casefold()
    if any(term in text for term in EXCLUDED): return None
    market = str(row.get("market") or "").casefold()
    selection = str(row.get("selection") or "").casefold()
    supported = any(term in market for term in ("1x2", "match result", "full time result", "fulltime result", "double chance")) or (("total goals" in market or "over/under" in market or "over under" in market) and ("over 1.5" in (market + " " + selection) or "over 2.5" in (market + " " + selection)))
    if not supported: return None
    return {**row, "odds": odds, "selection_method": "Executable SportyBet price and market-rule screen; no prediction"}

def candidate_rejection_bucket(row: dict, *args, **kwargs):
    try: odds = float(row.get("odds"))
    except (TypeError, ValueError, OverflowError): return "invalid_price"
    if not math.isfinite(odds) or not 1.15 <= odds <= 3.50: return "outside_odds_range"
    if not (row.get("eventId") or row.get("event_id")): return "missing_event_id"
    if not (row.get("marketId") or row.get("market_id")): return "missing_market_id"
    if not (row.get("outcomeId") or row.get("outcome_id")): return "missing_outcome_id"
    return "excluded_or_unsupported_market"

def prepare_candidate_pool(rows: list[dict], *args, **kwargs):
    pool, reasons = [], Counter()
    for row in rows or []:
        item = assess_candidate(row)
        if item is None: reasons[candidate_rejection_bucket(row)] += 1
        else: pool.append(item)
    pool.sort(key=lambda x: (float(x["odds"]), str(x.get("day") or ""), str(x.get("home") or ""), str(x.get("away") or "")))
    return pool, {"scanned": len(rows or []), "eligible": len(pool), "rejected": sum(reasons.values()), "rejection_reasons": dict(reasons), "research_scope": "Executable SportyBet price and market-rule checks only."}

def candidate_research_summary(pool):
    return f"{len(pool or [])} SportyBet prices passed executable market and ID checks."

def enrich_with_specialist_models(rows, *args, **kwargs):
    return [dict(x) for x in (rows or [])], {"enabled": False, "reason": "No prediction models are used."}

def apply_walk_forward_calibration(rows, validation):
    return [dict(x) for x in (rows or [])], {"applied": False, "reason": "No probability model is used."}
