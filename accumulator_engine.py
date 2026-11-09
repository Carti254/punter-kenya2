"""Constrained accumulator construction from priced, model-supported candidates.

Searches for a target-odds combination while allowing only one leg per fixture.
The objective maximises the product of model probability and offered odds (expected
payout factor) under an independence assumption; that assumption is surfaced explicitly.
"""
from __future__ import annotations

import math


def build_accumulator(candidates, target_odds, max_legs=20, beam_width=1800, max_candidates=250, ranking="value", min_legs=2):
    target = float(target_odds)
    if not math.isfinite(target) or target <= 1:
        raise ValueError("Target odds must be finite and greater than 1.")
    if not isinstance(max_legs, int) or not 1 <= max_legs <= 50:
        raise ValueError("max_legs must be an integer from 1 to 50.")
    if max_legs == 1 and min_legs == 2:
        return {
            "selections": [], "combined_odds": 1.0, "combined_probability": 0.0,
            "naive_independent_probability": 0.0, "correlation_adjustment_factor": 1.0,
            "expected_return_factor": 0.0, "expected_roi": -1.0,
            "target_reached": False, "independence_assumption": True,
            "reason": "An accumulator requires at least two distinct fixtures. No single-selection substitute was generated.",
        }
    if not isinstance(min_legs, int) or not 1 <= min_legs <= max_legs:
        raise ValueError("min_legs must be an integer from 1 to max_legs.")
    clean = []
    for row in candidates or []:
        try:
            odds, probability = float(row["odds"]), float(row["probability"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(odds) and odds > 1 and math.isfinite(probability) and 0 < probability < 1):
            continue
        home, away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
        if not home or not away:
            continue
        item = dict(row)
        item["odds"], item["probability"] = odds, probability
        market_text = (str(row.get("market") or "") + " " + str(row.get("selection") or "") + " " + str(row.get("specifier") or "")).casefold()
        # DNB includes a void-on-draw settlement. Until the accumulator models
        # push outcomes across all legs, multiplying conditional DNB probabilities
        # overstates accumulator hit probability and can misstate combined returns.
        if "draw no bet" in market_text or __import__("re").search(r"(?<![a-z])dnb(?![a-z])", market_text):
            continue
        push_market = any(term in market_text for term in (
            "total goals", "over under", "goals over", "goals under",
            "total match", "goal line", "asian handicap", "handicap",
        ))
        if push_market and __import__("re").search(r"(?<!\d)\d+\.0+(?!\d)|(?<!\d)\d+(?![\d.])", market_text):
            continue
        if "btts" in market_text or "both teams to score" in market_text:
            continue
        if "under 2.5" in market_text or ("under" in market_text and "2.5" in str(row.get("specifier") or "")):
            continue
        day = str(row.get("day") or row.get("match_date") or "")[:10]
        item["_fixture_key"] = (day, home.casefold(), away.casefold())
        item["_expected_factor"] = odds * probability
        # Aggressive target building may include high-variance, negative-EV legs.
        # Keep the model probability and expected factor visible to ranking/reporting;
        # the caller explicitly chooses the risk profile.
        clean.append(item)
    if ranking not in {"value", "probability", "odds"}:
        raise ValueError("ranking must be value, probability or odds.")
    # Grade A has complete recent-form and venue evidence. Grade B remains
    # eligible but is ranked behind A rather than being treated as equal.
    if ranking == "probability":
        clean.sort(key=lambda x: (x.get("research_grade") == "A", x["probability"], x["_expected_factor"]), reverse=True)
    elif ranking == "odds":
        clean.sort(key=lambda x: (x.get("research_grade") == "A", x["odds"], x["probability"]), reverse=True)
    else:
        clean.sort(key=lambda x: (x.get("research_grade") == "A", x["_expected_factor"], x["probability"], x["odds"]), reverse=True)
    # Large target odds must be built as a multi-match parlay, not one or two
    # extreme longshots. For targets >= 10, cap each leg at 4.00 and let the
    # accumulator add independent fixtures until the requested total is reached.
    per_leg_cap = 4.00 if target >= 10 else target * 1.20
    clean = [item for item in clean if item["odds"] <= per_leg_cap]
    clean = clean[:max_candidates]
    if not clean:
        return {"selections": [], "combined_odds": 1.0, "combined_probability": 0.0,
                "expected_return_factor": 0.0, "target_reached": False,
                "reason": f"No valid candidates within the {per_leg_cap:.2f} per-selection odds cap; no slip generated."}

    # A beam keeps this practical for Telegram requests with large fixture slates.
    states = [([], 1.0, 1.0, frozenset(), 0)]
    completed = []
    eligible_partials = []

    def completed_score(state):
        picks, odds_now, probability_now, _, _ = state
        grade_a_legs = sum(1 for pick in picks if pick.get("research_grade") == "A")
        # Once a combination reaches the requested odds band, retain the
        # highest-probability combinations first. Expected return and evidence
        # grade are tie-breakers, not a reason to prefer a fragile longshot.
        if ranking == "odds":
            return (probability_now, odds_now, grade_a_legs, -len(picks))
        return (probability_now, grade_a_legs, odds_now * probability_now, -len(picks))

    for _depth in range(max_legs):
        expanded = []
        for picks, odds_product, probability_product, used, start_index in states:
            for item_index in range(start_index, len(clean)):
                item = clean[item_index]
                fixture_key = item["_fixture_key"]
                if fixture_key in used:
                    continue
                new_odds = odds_product * item["odds"]
                new_probability = probability_product * item["probability"]
                new_picks = picks + [item]
                factor = new_odds * new_probability
                state = (new_picks, new_odds, new_probability, used | {fixture_key}, item_index + 1)
                if len(new_picks) >= min_legs and target <= new_odds <= target * 1.20:
                    completed.append(state)
                # Continue past the target until the requested number of legs is met.
                # Stop extending once the permitted 20% overshoot would be exceeded.
                if len(new_picks) < max_legs and new_odds < target * 1.20:
                    expanded.append(state)
                if len(new_picks) >= min_legs:
                    eligible_partials.append(state)
        # Bound completed-state memory on large fixture slates.
        if len(completed) > beam_width * 4:
            completed = sorted(completed, key=completed_score, reverse=True)[:beam_width * 2]
        if not expanded:
            break
        # Keep states near target and with stronger expected payout factors.
        def beam_score(state):
            picks, odds_now, probability_now, _, _ = state
            distance_bonus = -0.20 * abs(math.log(target / odds_now))
            grade_a_legs = sum(1 for pick in picks if pick.get("research_grade") == "A")
            if ranking == "odds":
                return (grade_a_legs, math.log(max(1e-15, probability_now)) + distance_bonus,
                        odds_now, -len(picks))
            return (grade_a_legs, math.log(max(1e-15, probability_now)) + distance_bonus,
                    odds_now * probability_now, -len(picks))
        expanded.sort(key=beam_score, reverse=True)
        states = expanded[:beam_width]

    if completed:
        # All completed states already satisfy the target band. Choose the
        # strongest combined hit probability first; target proximity must not
        # outrank selection quality. Use evidence grade/expected return as
        # tie-breakers, then prefer fewer legs.
        def completion_score(state):
            picks, odds_now, probability_now, _, _ = state
            grade_a_legs = sum(1 for pick in picks if pick.get("research_grade") == "A")
            return (probability_now, grade_a_legs, odds_now * probability_now,
                    -len(picks), -((odds_now / target) - 1.0))
        best = max(completed, key=completion_score)
    else:
        # Strict target rule: never return a lower-odds partial as a slip.
        return {
            "selections": [],
            "combined_odds": 1.0,
            "combined_probability": 0.0,
            "naive_independent_probability": 0.0,
            "correlation_adjustment_factor": 1.0,
            "expected_return_factor": 0.0,
            "expected_roi": -1.0,
            "target_reached": False,
            "independence_assumption": True,
            "reason": "No qualifying combination reached the requested target. No lower-odds substitute was generated.",
        }
    picks, odds_product, probability_product, _, _ = best
    selections = []
    for item in picks:
        selections.append({k: v for k, v in item.items() if not k.startswith("_")})

    # No arbitrary correlation haircut is applied. Without empirical pairwise
    # outcome data, the combined probability is explicitly the independence product.
    correlation_factor = 1.0
    naive_probability = probability_product
    adjusted_probability = naive_probability * correlation_factor
    return {
        "selections": selections,
        "combined_odds": odds_product,
        "combined_probability": adjusted_probability,
        "naive_independent_probability": naive_probability,
        "correlation_adjustment_factor": correlation_factor,
        "expected_return_factor": odds_product * adjusted_probability,
        "expected_roi": odds_product * adjusted_probability - 1,
        "target_reached": target <= odds_product <= target * 1.20 and len(selections) >= min_legs,
        "independence_assumption": True,
        "reason": None,
    }
