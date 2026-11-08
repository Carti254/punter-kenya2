"""Integrated evidence and market-value layer for accumulator construction.

This module only evaluates evidence already present in candidate rows. It does not
invent injuries, xG, lineups, odds history or specialist statistics. Those sources
must be supplied by the existing provider/model modules and are reported as
unavailable when absent.
"""
from __future__ import annotations

import math
from collections import Counter


def assess_candidate(row: dict, marginal_ev_floor: float = -0.03) -> dict | None:
    """Validate a priced model candidate and assign an auditable value tier.

    Tier A: positive raw expected value.
    Tier B: near-fair fallback, no worse than the configured EV floor.
    Below the floor: excluded.
    """
    try:
        odds = float(row.get("odds"))
        probability = float(row.get("probability"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(odds) and odds > 1 and math.isfinite(probability) and 0 < probability < 1):
        return None

    market = str(row.get("market") or "")
    selection = str(row.get("selection") or "")
    combined_text = f"{market} {selection} {row.get('specifier') or ''}".casefold()
    if "btts" in combined_text or "both teams to score" in combined_text:
        return None
    if "under 2.5" in combined_text or ("under" in combined_text and "2.5" in str(row.get("specifier") or "")):
        return None

    implied = 1.0 / odds
    edge = probability - implied
    ev = probability * odds - 1.0
    floor = float(marginal_ev_floor)
    if not math.isfinite(floor) or floor > 0:
        raise ValueError("marginal_ev_floor must be finite and no greater than zero.")
    if ev < floor:
        return None

    item = dict(row)
    item.update({
        "odds": odds,
        "probability": probability,
        "implied_probability": implied,
        "fair_odds": 1.0 / probability,
        "edge": edge,
        "ev": ev,
        "value_tier": "A" if ev > 0 else "B",
        "value_label": "Positive model-estimated value" if ev > 0 else "Near-fair fallback; positive value not established",
        "research_checks": [
            "fixture matched to bookmaker event",
            "current bookmaker price retrieved",
            "market probability supported by PUNTER model",
            "implied probability and raw expected value calculated",
        ],
        "research_gaps": [
            name for name, present in (
                ("verified starting lineups", bool(row.get("lineup_evidence"))),
                ("injury and suspension evidence", bool(row.get("team_news_evidence"))),
                ("xG and shot-quality evidence", bool(row.get("xg_evidence"))),
                ("live odds movement history", bool(row.get("odds_history_evidence"))),
                ("independent specialist market model", bool(row.get("specialist_evidence"))),
            ) if not present
        ],
    })
    return item


def prepare_candidate_pool(rows: list[dict], marginal_ev_floor: float = -0.03) -> tuple[list[dict], dict]:
    """Create a tiered candidate pool; A selections rank ahead of B fallbacks."""
    assessed = []
    rejected = 0
    for row in rows or []:
        item = assess_candidate(row, marginal_ev_floor=marginal_ev_floor)
        if item is None:
            rejected += 1
        else:
            assessed.append(item)

    assessed.sort(
        key=lambda x: (
            0 if x["value_tier"] == "A" else 1,
            x["ev"],
            x["probability"],
            x["odds"],
        ),
        reverse=False,
    )
    # Sort Tier A and B independently in descending quality, then concatenate.
    tier_a = sorted((x for x in assessed if x["value_tier"] == "A"),
                    key=lambda x: (x["ev"], x["probability"], x["odds"]), reverse=True)
    tier_b = sorted((x for x in assessed if x["value_tier"] == "B"),
                    key=lambda x: (x["ev"], x["probability"], x["odds"]), reverse=True)
    pool = tier_a + tier_b
    counts = Counter(x["value_tier"] for x in pool)
    return pool, {
        "scanned": len(rows or []),
        "eligible": len(pool),
        "tier_a": counts.get("A", 0),
        "tier_b": counts.get("B", 0),
        "rejected": rejected,
        "marginal_ev_floor": marginal_ev_floor,
        "research_scope": "Current fixture match, current bookmaker price, supported model probability and price-value calculation",
        "not_verified_by_this_scan": [
            "lineups and injury news",
            "xG and shot-quality data",
            "tactical video analysis",
            "cross-bookmaker closing-line movement",
            "cross-fixture statistical correlation",
        ],
    }


def candidate_research_summary(pool: list[dict]) -> str:
    counts = Counter(x.get("value_tier") for x in pool or [])
    return (
        f"Research screening: {len(pool or [])} eligible candidates "
        f"(Tier A positive estimated value: {counts.get('A', 0)}; "
        f"Tier B near-fair fallback: {counts.get('B', 0)})."
    )
