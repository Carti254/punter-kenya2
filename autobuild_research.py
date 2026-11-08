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
    home_n = int(row.get("home_sample") or 0)
    away_n = int(row.get("away_sample") or 0)
    home_venue_n = int(row.get("home_home_sample") or 0)
    away_venue_n = int(row.get("away_away_sample") or 0)
    h2h_n = int(row.get("h2h_count") or 0)
    checks = [
        "fixture matched to bookmaker event",
        "current bookmaker price retrieved",
        "market probability supported by PUNTER statistical model",
        "implied probability, fair odds and raw expected value calculated",
    ]
    if home_n >= 5 and away_n >= 5:
        checks.append(f"recent form and goals reviewed ({home_n} home-team matches; {away_n} away-team matches)")
    if home_venue_n >= 3 and away_venue_n >= 3:
        checks.append(f"home/away venue splits reviewed ({home_venue_n} home; {away_venue_n} away)")
    if h2h_n:
        checks.append(f"same-competition head-to-head reviewed ({h2h_n} meetings)")
    item.update({
        "odds": odds,
        "probability": probability,
        "implied_probability": implied,
        "fair_odds": 1.0 / probability,
        "edge": edge,
        "ev": ev,
        "value_tier": "A" if ev > 0 else "B",
        "value_label": "Positive model-estimated value" if ev > 0 else "Break-even estimate; positive value not established",
        "research_checks": checks,
        "research_evidence": {
            "model_confidence": row.get("model_confidence"),
            "home_form": row.get("home_form"),
            "away_form": row.get("away_form"),
            "home_sample": home_n,
            "away_sample": away_n,
            "home_scored_avg": row.get("home_scored_avg"),
            "home_conceded_avg": row.get("home_conceded_avg"),
            "away_scored_avg": row.get("away_scored_avg"),
            "away_conceded_avg": row.get("away_conceded_avg"),
            "home_home_sample": home_venue_n,
            "away_away_sample": away_venue_n,
            "h2h_count": h2h_n,
            "h2h_results": row.get("h2h_results") or [],
            "expected_home_goals": row.get("lambda_home"),
            "expected_away_goals": row.get("lambda_away"),
        },
        "research_gaps": [
            name for name, present in (
                ("minimum recent-form sample", home_n >= 5 and away_n >= 5),
                ("minimum home/away venue sample", home_venue_n >= 3 and away_venue_n >= 3),
                ("head-to-head history", h2h_n > 0),
                ("verified starting lineups", bool(row.get("lineup_evidence"))),
                ("injury and suspension evidence", bool(row.get("team_news_evidence"))),
                ("xG and shot-quality evidence", bool(row.get("xg_evidence"))),
                ("live odds movement history", bool(row.get("odds_history_evidence"))),
                ("independent specialist market model", bool(row.get("specialist_evidence"))),
                ("tactical matchup evidence", bool(row.get("tactical_evidence"))),
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
    form = sum(bool(x.get("research_evidence", {}).get("home_sample")) and bool(x.get("research_evidence", {}).get("away_sample")) for x in pool or [])
    venue = sum(bool(x.get("research_evidence", {}).get("home_home_sample")) and bool(x.get("research_evidence", {}).get("away_away_sample")) for x in pool or [])
    h2h = sum(bool(x.get("research_evidence", {}).get("h2h_count")) for x in pool or [])
    return (
        f"Research screening: {len(pool or [])} eligible candidates "
        f"(Tier A positive estimated value: {counts.get('A', 0)}; "
        f"Tier B break-even: {counts.get('B', 0)}). "
        f"Evidence coverage: recent form {form}; venue splits {venue}; H2H {h2h}."
    )
