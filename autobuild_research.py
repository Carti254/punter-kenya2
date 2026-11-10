"""Minimal execution-safety checks for SportyBet prices; no prediction or research model."""
from __future__ import annotations
from collections import Counter
import math

def assess_candidate(row: dict, *args, **kwargs):
    try: odds=float(row.get("odds"))
    except (TypeError,ValueError,OverflowError): return None
    if not math.isfinite(odds) or odds <= 1: return None
    if str(row.get("bookmaker") or "").strip().casefold().replace(" ","") != "sportybet": return None
    if not (row.get("eventId") or row.get("event_id") or row.get("sportybet_event_id")): return None
    if not (row.get("marketId") or row.get("market_id")): return None
    if not (row.get("outcomeId") or row.get("outcome_id")): return None
    if row.get("fixture_match_verified") is not True: return None
    text=" ".join(str(row.get(k) or "") for k in ("market","selection","specifier")).casefold()
    if any(x in text for x in ("btts","both teams to score","under 2.5","draw no bet"," dnb","2up","early payout","handicap","corner","card","booking","shots","player","correct score","half time","first half")): return None
    return {**row,"odds":odds,"selection_method":"SportyBet price-only; no prediction performed"}

def candidate_rejection_bucket(row: dict, *args, **kwargs):
    return "invalid_or_unsupported_sportybet_price"

def prepare_candidate_pool(rows: list[dict], *args, **kwargs):
    pool=[]; reasons=Counter()
    for row in rows or []:
        item=assess_candidate(row)
        if item is None: reasons[candidate_rejection_bucket(row)]+=1
        else: pool.append(item)
    pool.sort(key=lambda x:(float(x["odds"]),str(x.get("home")),str(x.get("away"))))
    return pool,{"scanned":len(rows or []),"eligible":len(pool),"rejected":sum(reasons.values()),
                 "rejection_reasons":dict(reasons),"research_scope":"SportyBet execution checks only; no match analysis or probability estimates."}

def candidate_research_summary(pool):
    return f"{len(pool or [])} executable SportyBet prices; no predictions calculated."

def enrich_with_specialist_models(rows, *args, **kwargs):
    return [dict(x) for x in (rows or [])],{"enabled":False,"reason":"All specialist prediction models removed."}

def apply_walk_forward_calibration(rows, validation):
    return [dict(x) for x in (rows or [])],{"applied":False,"reason":"Prediction calibration removed."}
