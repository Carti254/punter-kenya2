"""Integrated evidence and market-value layer for accumulator construction.

This module only evaluates evidence already present in candidate rows. It does not
invent injuries, xG, lineups, odds history or specialist statistics. Those sources
must be supplied by the existing provider/model modules and are reported as
unavailable when absent.
"""
from __future__ import annotations

import math
import time
from collections import Counter

from dixon_coles_engine import selected_outcome_probability
from advanced_models import elo_expected, monte_carlo_scoreline

_SPECIALIST_REPORT_CACHE = {}
_SPECIALIST_CACHE_TTL = 900


def assess_candidate(row: dict, marginal_ev_floor: float = 0.03, use_dixon_coles: bool = False) -> dict | None:
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

    # The live scanner labels forecasts LOW when the combined verified
    # recent-form sample is too small. Do not let a high quoted price turn
    # that weak forecast into an accumulator leg.
    confidence = str(row.get("model_confidence") or "").strip().upper()
    if confidence not in {"MODERATE", "HIGH"}:
        return None

    # An independently supplied external model is a disagreement check, not a
    # replacement probability. Large conflicts are withheld rather than averaged
    # into a deceptively precise number.
    try:
        external_disagreement = float(row.get("external_model_disagreement"))
    except (TypeError, ValueError, OverflowError):
        external_disagreement = None
    if external_disagreement is not None:
        if not math.isfinite(external_disagreement) or external_disagreement > 0.10:
            return None
    # Recent form is fundamental: both teams need at least five verified
    # matches. Venue splits are valuable supplementary evidence, but missing
    # venue history must downgrade the research grade rather than erase an
    # otherwise well-supported candidate.
    for sample_key, minimum in (("home_sample", 5), ("away_sample", 5)):
        try:
            sample_size = int(row.get(sample_key))
        except (TypeError, ValueError, OverflowError):
            return None
        if sample_size < minimum:
            return None
    try:
        home_venue_n = int(row.get("home_home_sample") or 0)
    except (TypeError, ValueError, OverflowError):
        home_venue_n = 0
    try:
        away_venue_n = int(row.get("away_away_sample") or 0)
    except (TypeError, ValueError, OverflowError):
        away_venue_n = 0
    venue_evidence_complete = home_venue_n >= 3 and away_venue_n >= 3

    market = str(row.get("market") or "")
    selection = str(row.get("selection") or "")
    combined_text = f"{market} {selection} {row.get('specifier') or ''}".casefold()
    if "btts" in combined_text or "both teams to score" in combined_text:
        return None
    if "under 2.5" in combined_text or ("under" in combined_text and "2.5" in str(row.get("specifier") or "")):
        return None
    # 2UP/early-payout is a distinct settlement market, not an ordinary 1X2
    # away win. It must never inherit a full-time result probability.
    if any(token in combined_text for token in ("2up", "two up", "early payout", "two-goal lead")):
        return None

    # Re-score supported 1X2 selections with the Dixon-Coles low-score
    # correction, but only when PUNTER already supplied both expected-goal rates.
    # Never manufacture goal rates or apply this model to unrelated markets.
    probability_source = str(row.get("probability_source") or "PUNTER statistical model")
    base_probability = probability
    market_key = " ".join(market.casefold().replace("-", " ").split())
    is_1x2 = any(token in market_key for token in (
        "1x2", "match result", "full time result", "fulltime result"
    ))
    is_punter_scoreline_probability = probability_source.startswith(
        ("PUNTER venue-weighted", "PUNTER statistical model")
    )
    if (use_dixon_coles and is_1x2 and is_punter_scoreline_probability
            and row.get("lambda_home") is not None and row.get("lambda_away") is not None
            and not probability_source.startswith("Dixon-Coles + walk-forward temperature calibration")):
        try:
            dc_probability = selected_outcome_probability(
                row.get("lambda_home"), row.get("lambda_away"), selection,
                str(row.get("home") or ""), str(row.get("away") or ""),
            )
        except (TypeError, ValueError, ArithmeticError):
            dc_probability = None
        if dc_probability is not None and 0 < dc_probability < 1:
            probability = dc_probability
            probability_source = "Dixon-Coles Poisson (PUNTER expected-goal rates; rho=-0.05 experimental)"

    # Independent computational cross-checks from the same source-derived
    # expected-goal rates. These do not create new factual evidence or calibration.
    monte_carlo = None
    mc_selected = None
    try:
        if row.get("lambda_home") is not None and row.get("lambda_away") is not None:
            monte_carlo = row.get("monte_carlo") or monte_carlo_scoreline(
                row.get("lambda_home"), row.get("lambda_away"), simulations=10000
            )
            selection_key = selection.casefold().strip()
            if selection_key in {"1", "home", str(row.get("home") or "").casefold()}:
                mc_selected = monte_carlo.get("home")
            elif selection_key in {"x", "draw"}:
                mc_selected = monte_carlo.get("draw")
            elif selection_key in {"2", "away", str(row.get("away") or "").casefold()}:
                mc_selected = monte_carlo.get("away")
    except (TypeError, ValueError, ArithmeticError):
        monte_carlo = None
    elo_probability = None
    try:
        if row.get("elo_home_rating") is not None and row.get("elo_away_rating") is not None:
            elo_probability = elo_expected(row["elo_home_rating"], row["elo_away_rating"], 60.0)
    except (TypeError, ValueError):
        elo_probability = None

    implied = 1.0 / odds
    edge = probability - implied
    ev = probability * odds - 1.0
    floor = float(marginal_ev_floor)
    if not math.isfinite(floor):
        raise ValueError("marginal_ev_floor must be finite.")
    if ev < floor:
        return None

    item = dict(row)
    home_n = int(row.get("home_sample") or 0)
    away_n = int(row.get("away_sample") or 0)
    home_venue_n = int(row.get("home_home_sample") or 0)
    away_venue_n = int(row.get("away_away_sample") or 0)
    venue_evidence_complete = home_venue_n >= 3 and away_venue_n >= 3
    h2h_n = int(row.get("h2h_count") or 0)
    standings = row.get("standings_evidence") or {}
    standings_available = bool(standings.get("home") and standings.get("away"))
    early_goals = row.get("early_goal_evidence") or {}
    early_home, early_away = early_goals.get("home") or {}, early_goals.get("away") or {}
    early_goals_available = (
        int(early_home.get("sample") or 0) >= 3
        and int(early_away.get("sample") or 0) >= 3
    )
    lineup_sides = (row.get("lineup_evidence") or {}).get("sides") or []
    lineups_available = len(lineup_sides) >= 2
    odds_history = row.get("odds_history_evidence") or {}
    odds_history_available = (
        odds_history.get("status") == "observed"
        and int(odds_history.get("observations") or 0) >= 2
    )
    tactical = row.get("tactical_evidence") or {}
    tactical_available = bool(tactical.get("home") and tactical.get("away"))
    other_bookmaker_available = bool(
        row.get("best_other_bookmaker") or row.get("other_bookmaker_prices")
    )

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
    if standings_available:
        checks.append("current league table positions and points checked (API-Football)")
    if early_goals_available:
        checks.append("first-goal-before-10-minute rates checked from recent match events (API-Football)")
    if probability_source.startswith("Dixon-Coles"):
        checks.append("Dixon-Coles low-score correction applied to the 1X2 probability")
    if monte_carlo:
        checks.append(f"10,000-run Monte Carlo scoreline cross-check performed (selection estimate {mc_selected:.1%})" if mc_selected is not None else "10,000-run Monte Carlo scoreline simulation performed; exact selection mapping unavailable")
    if elo_probability is not None:
        checks.append(f"Chronological competition Elo ratings available (home {float(row['elo_home_rating']):.0f}; away {float(row['elo_away_rating']):.0f}); home expected score {elo_probability:.1%}")
    item.update({
        "odds": odds,
        "probability": probability,
        "base_probability": base_probability if probability != base_probability else row.get("base_probability"),
        "probability_source": probability_source,
        "dixon_coles_adjusted": probability_source.startswith("Dixon-Coles"),
        "research_grade": "A" if venue_evidence_complete else "B",
        "research_grade_reason": (
            "Recent-form and venue-split minimums satisfied."
            if venue_evidence_complete else
            "Recent-form minimums satisfied; one or both venue-specific samples are incomplete. Venue evidence is a disclosed research gap, not an automatic rejection."
        ),
        "implied_probability": implied,
        "fair_odds": 1.0 / probability,
        "edge": edge,
        "ev": ev,
        "value_tier": "A" if ev > 0 else ("B" if abs(ev) < 1e-9 else "C"),
        "value_label": "Positive model-estimated value" if ev > 0 else ("Break-even estimate; positive value not established" if abs(ev) < 1e-9 else "Negative model-estimated value; retained only as a clearly labelled fallback"),
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
            "home_home_gf": row.get("home_home_gf"),
            "home_home_ga": row.get("home_home_ga"),
            "away_away_gf": row.get("away_away_gf"),
            "away_away_ga": row.get("away_away_ga"),
            "home_recent_matches": row.get("home_recent_matches") or [],
            "away_recent_matches": row.get("away_recent_matches") or [],
            "h2h_count": h2h_n,
            "h2h_results": row.get("h2h_results") or [],
            "expected_home_goals": row.get("lambda_home"),
            "expected_away_goals": row.get("lambda_away"),
            "specialist_evidence": row.get("specialist_evidence"),
            "standings_evidence": row.get("standings_evidence"),
            "early_goal_evidence": row.get("early_goal_evidence"),
            "elo_home_rating": row.get("elo_home_rating"),
            "elo_away_rating": row.get("elo_away_rating"),
            "elo_matches_used": row.get("elo_matches_used"),
            "monte_carlo": monte_carlo,
            "calibration_evidence": row.get("calibration_evidence"),
            "monte_carlo_selected_probability": mc_selected,
            "elo_home_expected_score": elo_probability,
        },
        "research_gaps": [
            name for name, present in (
                ("minimum recent-form sample", home_n >= 5 and away_n >= 5),
                ("minimum home/away venue sample", home_venue_n >= 3 and away_venue_n >= 3),
                ("head-to-head history", h2h_n > 0),
                ("current league table position and points", standings_available),
                ("cross-competition team-strength comparison", bool(row.get("league_strength_evidence"))),
                ("early-goal timing statistics (first 5/10 minutes)", early_goals_available),
                ("chronological Elo rating evidence", elo_probability is not None),
                ("Monte Carlo scoreline cross-check", monte_carlo is not None),
                ("verified starting lineups for both teams", lineups_available),
                ("injury and suspension evidence", bool(row.get("team_news_evidence"))),
                ("xG and shot-quality evidence", bool(row.get("xg_evidence"))),
                ("live odds movement history", odds_history_available),
                ("independent specialist market model", bool(row.get("specialist_evidence"))),
                ("cross-bookmaker exact price comparison", other_bookmaker_available),
                ("empirical cross-fixture correlation data", bool(row.get("correlation_evidence"))),
                ("out-of-sample probability calibration evidence", bool(row.get("calibration_evidence"))),
                ("tactical matchup evidence for both teams", tactical_available),
            ) if not present
        ],
    })
    return item


def candidate_rejection_bucket(row: dict, marginal_ev_floor: float = 0.03) -> str:
    """Return the first auditable reason a raw market row fails the research gate."""
    try:
        odds = float(row.get("odds"))
        probability = float(row.get("probability"))
    except (TypeError, ValueError, OverflowError):
        return "missing_probability_or_price"
    if not (math.isfinite(odds) and odds > 1 and math.isfinite(probability) and 0 < probability < 1):
        return "invalid_probability_or_price"
    if str(row.get("model_confidence") or "").strip().upper() not in {"MODERATE", "HIGH"}:
        return "low_or_missing_model_confidence"
    try:
        disagreement = float(row.get("external_model_disagreement"))
    except (TypeError, ValueError, OverflowError):
        disagreement = None
    if disagreement is not None and (not math.isfinite(disagreement) or disagreement > 0.10):
        return "external_model_disagreement_over_10pct"
    for key, minimum in (("home_sample", 5), ("away_sample", 5)):
        try:
            if int(row.get(key)) < minimum:
                return "insufficient_" + key
        except (TypeError, ValueError, OverflowError):
            return "missing_" + key
    market_text = " ".join(str(row.get(k) or "") for k in ("market", "selection", "specifier")).casefold()
    if "btts" in market_text or "both teams to score" in market_text:
        return "excluded_btts"
    if "under 2.5" in market_text or ("under" in market_text and "2.5" in str(row.get("specifier") or "")):
        return "excluded_under_2_5"
    if any(token in market_text for token in ("2up", "two up", "early payout", "two-goal lead")):
        return "unsupported_2up_market"
    if probability * odds - 1.0 < float(marginal_ev_floor):
        return "estimated_ev_below_3pct_floor"
    return "other_assessment_rejection"


def prepare_candidate_pool(rows: list[dict], marginal_ev_floor: float = 0.03, use_dixon_coles: bool = False) -> tuple[list[dict], dict]:
    """Create a tiered candidate pool; A selections rank ahead of B fallbacks."""
    assessed = []
    rejected = 0
    rejection_reasons = Counter()
    for row in rows or []:
        item = assess_candidate(row, marginal_ev_floor=marginal_ev_floor, use_dixon_coles=use_dixon_coles)
        if item is None:
            rejected += 1
            rejection_reasons[candidate_rejection_bucket(row, marginal_ev_floor)] += 1
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
    tier_c = sorted((x for x in assessed if x["value_tier"] == "C"),
                    key=lambda x: (x["ev"], x["probability"], x["odds"]), reverse=True)
    pool = tier_a + tier_b + tier_c
    counts = Counter(x["value_tier"] for x in pool)
    return pool, {
        "scanned": len(rows or []),
        "eligible": len(pool),
        "tier_a": counts.get("A", 0),
        "tier_b": counts.get("B", 0),
        "tier_c": counts.get("C", 0),
        "research_grade_a": sum(1 for x in pool if x.get("research_grade") == "A"),
        "research_grade_b": sum(1 for x in pool if x.get("research_grade") == "B"),
        "venue_evidence_incomplete": sum(1 for x in pool if x.get("research_grade") == "B"),
        "rejected": rejected,
        "rejection_reasons": dict(rejection_reasons.most_common()),
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
        f"Tier B break-even: {counts.get('B', 0)}; "
        f"Tier C negative estimated value: {counts.get('C', 0)}). "
        f"Evidence coverage: recent form {form}; venue splits {venue}; H2H {h2h}."
    )


def enrich_with_specialist_models(rows: list[dict], api_key: str | None = None, max_fixtures: int = 4) -> tuple[list[dict], dict]:
    """Optionally enrich matching corner/card/shot/first-half prices using API-Football.

    This is deliberately opt-in: without API_FOOTBALL_KEY, candidates are returned
    unchanged and the report says why. Only exact fixture/date/name matches are used.
    Specialist estimates replace the base probability only for the exact same
    market/selection/line; no nearest-line or fuzzy fixture substitution is allowed.
    """
    import os
    import re
    from collections import defaultdict
    from datetime import datetime
    from specialist_models import specialist_council, specialist_price_assessments

    key = api_key or os.getenv("API_FOOTBALL_KEY")
    output = [dict(row) for row in (rows or [])]
    if not key:
        return output, {"enabled": False, "fixtures_checked": 0, "matched_prices": 0,
                        "reason": "API_FOOTBALL_KEY is not configured; specialist models were not run."}

    grouped = defaultdict(list)
    for row in output:
        home, away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
        raw_date = (row.get("match_date") or row.get("date") or row.get("day") or row.get("kickoff") or
                    row.get("start_time") or row.get("commence_time") or row.get("commenceTime"))
        if not home or not away or not raw_date:
            continue
        try:
            match_date = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            match = re.search(r"\\d{4}-\\d{2}-\\d{2}", str(raw_date))
            if not match:
                continue
            match_date = match.group(0)
        grouped[(home, away, match_date)].append(row)

    checked = matched = failures = 0
    skipped_by_quota_cap = 0
    for index, ((home, away, match_date), candidates) in enumerate(grouped.items()):
        if index >= max(1, int(max_fixtures)):
            skipped_by_quota_cap = len(grouped) - index
            break
        checked += 1
        market_rows = []
        for row in candidates:
            market_rows.append({"market": row.get("market"), "selection": row.get("selection"),
                                "odds": row.get("odds")})
        game = {"home": home, "away": away, "bookmaker": "SportyBet", "markets": market_rows}
        try:
            cache_key = (home.casefold(), away.casefold(), match_date)
            cached = _SPECIALIST_REPORT_CACHE.get(cache_key)
            if cached and time.time() - cached[0] < _SPECIALIST_CACHE_TTL:
                report = cached[1]
            else:
                report = specialist_council(home, away, match_date, api_key=key, last=5)
                _SPECIALIST_REPORT_CACHE[cache_key] = (time.time(), report)
            assessments = specialist_price_assessments(report, [game])
        except Exception:
            failures += 1
            continue
        for row in candidates:
            if report.get("lineup_evidence"):
                row["lineup_evidence"] = report["lineup_evidence"]
            if report.get("team_news_evidence"):
                row["team_news_evidence"] = report["team_news_evidence"]
            if report.get("xg_evidence"):
                row["xg_evidence"] = report["xg_evidence"]
            if report.get("tactical_evidence"):
                row["tactical_evidence"] = report["tactical_evidence"]
            if report.get("standings_evidence"):
                row["standings_evidence"] = report["standings_evidence"]
            if report.get("early_goal_evidence"):
                row["early_goal_evidence"] = report["early_goal_evidence"]
            if report.get("lineup_evidence") or report.get("team_news_evidence"):
                providers = set(row.get("external_evidence_sources") or [])
                providers.add("api-football")
                row["external_evidence_sources"] = sorted(providers)
            market_name = str(row.get("market") or "").casefold()
            selection = str(row.get("selection") or "").strip().casefold()
            try:
                odds = float(row.get("odds"))
            except (TypeError, ValueError):
                continue
            match = next((a for a in assessments
                          if str(a.get("market_name") or "").casefold() == market_name
                          and str(a.get("selection") or "").strip().casefold() == selection
                          and abs(float(a.get("odds") or 0) - odds) < 1e-9), None)
            if not match:
                continue
            row["base_probability"] = row.get("probability")
            row["probability"] = float(match["probability"])
            row["probability_source"] = "API-Football recent-form specialist Poisson baseline (experimental)"
            row["specialist_evidence"] = {
                "provider": report.get("provider"), "model": "recent-form Poisson baseline",
                "market": match.get("market"), "line": match.get("line"),
                "expected_total": next((p.get("expected_total") for p in report.get("panels", [])
                                        if p.get("market") == match.get("market")), None),
                "edge": match.get("edge"), "expected_roi": match.get("expected_roi"),
                "warning": report.get("warning"),
            }
            matched += 1
    return output, {"enabled": True, "fixtures_checked": checked, "matched_prices": matched,
                    "fixture_failures": failures, "fixtures_skipped_by_free_quota_cap": skipped_by_quota_cap,
                    "reason": "Exact-match specialist evidence applied where provider data and matching bookmaker lines were available. The free API-Football tier is quota-limited; specialist enrichment is capped per autobuild run."}


def apply_walk_forward_calibration(rows: list[dict], validation: dict) -> tuple[list[dict], dict]:
    """Apply temperature calibration only when an out-of-sample test beats raw scores.

    Uses walk-forward holdout metrics generated from earlier training dates. If the
    validation sample is too small or calibration fails to improve both Brier and
    log loss, probabilities remain untouched and the reason is returned.
    """
    output = [dict(row) for row in (rows or [])]
    if not validation or not validation.get("ready"):
        return output, {"applied": False, "reason": "Insufficient walk-forward history; calibration not applied."}
    try:
        # Calibration is valid only for the exact venue-weighted Dixon-Coles
        # candidate being deployed. It must first beat both the current model
        # and prevalence baseline, then improve its own raw Brier and log loss.
        candidate_is_better = (
            float(validation["candidate_brier"]) < float(validation["raw_brier"])
            and float(validation["candidate_logloss"]) < float(validation["raw_logloss"])
            and float(validation["candidate_brier"]) < float(validation["baseline_brier"])
            and float(validation["candidate_logloss"]) < float(validation["baseline_logloss"])
        )
        calibration_improves_candidate = (
            float(validation["candidate_calibrated_brier"]) < float(validation["candidate_brier"])
            and float(validation["candidate_calibrated_logloss"]) < float(validation["candidate_logloss"])
        )
        if not candidate_is_better:
            return output, {"applied": False, "reason": "Venue-weighted Dixon-Coles candidate did not beat both the current model and baseline on both holdout metrics."}
        if not calibration_improves_candidate:
            return output, {"applied": False, "reason": "Temperature calibration did not improve both holdout metrics for the candidate model."}
        temperature = float(validation["temperature"])
        from prediction_validation import apply_temperature
        from dixon_coles_engine import selected_outcome_probability
        adjusted = 0
        for row in output:
            market = str(row.get("market") or "").casefold()
            source = str(row.get("probability_source") or "")
            if not any(token in market for token in ("1x2", "match result", "full time result", "fulltime result")):
                continue
            if not source.startswith(("PUNTER venue-weighted", "PUNTER statistical model")):
                continue
            try:
                if row.get("lambda_home") is None or row.get("lambda_away") is None:
                    continue
                home, away = str(row.get("home") or ""), str(row.get("away") or "")
                vector = [
                    selected_outcome_probability(row["lambda_home"], row["lambda_away"], "1", home, away),
                    selected_outcome_probability(row["lambda_home"], row["lambda_away"], "X", home, away),
                    selected_outcome_probability(row["lambda_home"], row["lambda_away"], "2", home, away),
                ]
                vector = apply_temperature(vector, temperature)
                selection = str(row.get("selection") or "").strip().casefold()
                if selection in {"1", "home", "home win", home.casefold()}:
                    probability = vector[0]
                elif selection in {"x", "draw", "tie"}:
                    probability = vector[1]
                elif selection in {"2", "away", "away win", away.casefold()}:
                    probability = vector[2]
                else:
                    continue
                if not 0 < float(probability) < 1:
                    continue
                row["probability"] = float(probability)
                row["probability_source"] = "Dixon-Coles + walk-forward temperature calibration"
                row["calibration_evidence"] = {
                    "source": validation.get("source"),
                    "temperature": temperature,
                    "train": validation.get("train"),
                    "holdout": validation.get("holdout"),
                    "current_model_brier": validation.get("raw_brier"),
                    "current_model_logloss": validation.get("raw_logloss"),
                    "candidate_brier": validation.get("candidate_brier"),
                    "candidate_logloss": validation.get("candidate_logloss"),
                    "candidate_calibrated_brier": validation.get("candidate_calibrated_brier"),
                    "candidate_calibrated_logloss": validation.get("candidate_calibrated_logloss"),
                    "applied": True,
                }
                adjusted += 1
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return output, {"applied": adjusted > 0, "adjusted_candidates": adjusted,
                        "temperature": temperature,
                        "reason": "Walk-forward temperature calibration applied to exact 1X2 selections." if adjusted else
                                  "Validation passed, but no exact supported 1X2 candidates were available."}
    except Exception as exc:
        return output, {"applied": False, "reason": f"Calibration application failed: {type(exc).__name__}"}
