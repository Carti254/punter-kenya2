"""Constrained accumulator construction from priced, model-supported candidates.

Searches for a target-odds combination while allowing only one leg per fixture.
The objective maximises the product of model probability and offered odds (expected
payout factor) under an independence assumption; that assumption is surfaced explicitly.
"""
from __future__ import annotations

import math


def build_accumulator(candidates, target_odds, max_legs=None, beam_width=1800, max_candidates=None, ranking="value", min_legs=1):
    target = float(target_odds)
    if not math.isfinite(target) or target <= 1:
        raise ValueError("Target odds must be finite and greater than 1.")
    if max_legs is not None and (not isinstance(max_legs, int) or max_legs < 1):
        raise ValueError("max_legs must be a positive integer or None.")
    if not isinstance(min_legs, int) or min_legs < 1:
        raise ValueError("min_legs must be a positive integer.")
    if max_legs is not None and min_legs > max_legs:
        raise ValueError("min_legs cannot exceed max_legs.")
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
        market_text = (str(row.get("market") or "") + " " + str(row.get("selection") or "") + " " + str(row.get("specifier") or "")).casefold()
        # Enforce the user's permanent market exclusions at the final selection boundary.
        if "btts" in market_text or "both teams to score" in market_text:
            continue
        if "under 2.5" in market_text or ("under" in market_text and "2.5" in str(row.get("specifier") or "")):
            continue
        item = dict(row)
        item["odds"], item["probability"] = odds, probability
        # Distinguish the same teams playing on different local dates.
        day = str(row.get("day") or row.get("match_date") or "")[:10]
        item["_fixture_key"] = (day, home.casefold(), away.casefold())
        item["_expected_factor"] = odds * probability
        # Aggressive target building may include high-variance, negative-EV legs.
        # Keep the model probability and expected factor visible to ranking/reporting;
        # the caller explicitly chooses the risk profile.
        clean.append(item)
    if ranking not in {"value", "probability", "odds"}:
        raise ValueError("ranking must be value, probability or odds.")
    if ranking == "probability":
        clean.sort(key=lambda x: (x["probability"], x["_expected_factor"]), reverse=True)
    elif ranking == "odds":
        # Prefer robust price/probability combinations over isolated longshots.
        clean.sort(key=lambda x: (x["_expected_factor"], x["probability"], x["odds"]), reverse=True)
    else:
        clean.sort(key=lambda x: (x["_expected_factor"], x["probability"], x["odds"]), reverse=True)
    # SportyBet Kenya's published betslip limit is 50 selections.
    # Keep the four strongest market alternatives per fixture to avoid expanding
    # thousands of near-identical beam-search states for the same match.
    by_fixture = {}
    for item in clean:
        alternatives = by_fixture.setdefault(item["_fixture_key"], [])
        if len(alternatives) < 4:
            alternatives.append(item)
    clean = [item for alternatives in by_fixture.values() for item in alternatives]
    fixture_count = len(by_fixture)
    max_legs = min(max_legs, fixture_count, 50) if max_legs is not None else min(fixture_count, 50)
    if min_legs > max_legs:
        return {"selections": [], "combined_odds": 1.0, "combined_probability": 0.0,
                "expected_return_factor": 0.0, "target_reached": False,
                "reason": f"Only {fixture_count} distinct eligible fixtures are available."}
    if max_candidates is not None:
        clean = clean[:max_candidates]
    if not clean:
        return {"selections": [], "combined_odds": 1.0, "combined_probability": 0.0,
                "expected_return_factor": 0.0, "target_reached": False,
                "reason": "No valid model-supported priced candidates; no slip generated."}

    # A beam keeps this practical for Telegram requests with large fixture slates.
    states = [([], 1.0, 1.0, frozenset(), 0)]
    completed = []
    eligible_partials = []

    def completed_score(state):
        if ranking == "probability":
            return (state[2], state[1] * state[2], -len(state[0]))
        if ranking == "odds":
            return (state[1] * state[2], state[2], -len(state[0]))
        return (state[1] * state[2], state[2], -len(state[0]))

    for _depth in range(max_legs):
        expanded = []
        for picks, odds_product, probability_product, used, start_index in states:
            for item_index in range(start_index, len(clean)):
                item = clean[item_index]
                fixture_key = item["_fixture_key"]
                if fixture_key in used:
                    continue
                new_odds = odds_product * item["odds"]
                # Conservative dependence haircut for legs in the same competition
                # on the same date. This is an explicit risk heuristic, NOT an
                # empirically estimated pairwise correlation coefficient.
                dependence_factor = 1.0
                item_league = str(item.get("league") or "").strip().casefold()
                item_day = str(item.get("day") or item.get("match_date") or "")[:10]
                for previous in picks:
                    previous_league = str(previous.get("league") or "").strip().casefold()
                    previous_day = str(previous.get("day") or previous.get("match_date") or "")[:10]
                    if item_league and previous_league == item_league and item_day and previous_day == item_day:
                        dependence_factor *= 0.97
                new_probability = probability_product * item["probability"] * dependence_factor
                new_picks = picks + [item]
                factor = new_odds * new_probability
                state = (new_picks, new_odds, new_probability, used | {fixture_key}, item_index + 1)
                if len(new_picks) >= min_legs and target <= new_odds <= target * 1.20:
                    completed.append(state)
                # Continue adding distinct fixtures while target tolerance allows.
                if len(new_picks) < max_legs and new_odds < target * 1.20:
                    expanded.append(state)
                if len(new_picks) >= min_legs and new_odds <= target * 1.20:
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
            if ranking == "probability":
                return (math.log(max(1e-15, probability_now)) + distance_bonus,
                        odds_now * probability_now, -len(picks))
            if ranking == "odds":
                return (math.log(max(1e-15, odds_now)) + distance_bonus,
                        odds_now * probability_now, -len(picks))
            return (math.log(max(1e-15, odds_now * probability_now)) + distance_bonus,
                    probability_now, -len(picks))
        expanded.sort(key=beam_score, reverse=True)
        states = expanded[:beam_width]

    if completed:
        # Minimise overshoot first; ranking is only a tie-breaker.
        def completion_score(state):
            picks, odds_now, probability_now, _, _ = state
            secondary = probability_now if ranking == "probability" else odds_now * probability_now
            if ranking == "odds":
                return (secondary, -((odds_now / target) - 1.0), probability_now, -len(picks))
            return (-((odds_now / target) - 1.0), secondary, probability_now, -len(picks))
        best = max(completed, key=completion_score)
    else:
        # Return the best available partial construction, never pretend target was met.
        all_states = eligible_partials or states
        if not all_states:
            all_states = [([], 1.0, 1.0, frozenset(), 0)]
        # A fallback is meant to be the closest sensible slip to the requested
        # target, not merely the single leg with the largest expected-return
        # factor. Only partials within the existing 20% tolerance are eligible;
        # this prevents a small target from being "filled" with an extreme longshot.
        def fallback_score(state):
            _, odds_now, probability_now, _, _ = state
            distance = -abs(math.log(target / odds_now))
            value = odds_now * probability_now
            if ranking == "probability":
                return (distance, probability_now, value, -len(state[0]))
            return (distance, value, probability_now, -len(state[0]))
        best = max(all_states, key=fallback_score)
    picks, odds_product, probability_product, _, _ = best
    naive_probability_product = math.prod(item["probability"] for item in picks)
    correlation_adjustment_factor = (
        probability_product / naive_probability_product if naive_probability_product > 0 else 1.0
    )
    selections = []
    for item in picks:
        selections.append({k: v for k, v in item.items() if not k.startswith("_")})
    competition_days = {}
    for item in selections:
        league = str(item.get("league") or "").strip()
        day = str(item.get("day") or "").strip()
        if league and day:
            competition_days.setdefault((league.casefold(), day), {"league": league, "day": day, "count": 0})
            competition_days[(league.casefold(), day)]["count"] += 1
    correlation_warnings = [
        f"{group['count']} legs from {group['league']} on {group['day']}"
        for group in competition_days.values() if group["count"] > 1
    ]
    return {
        "selections": selections,
        "combined_odds": odds_product,
        "combined_probability": probability_product,
        "naive_independent_probability": naive_probability_product,
        "correlation_adjustment_factor": correlation_adjustment_factor,
        "expected_return_factor": odds_product * probability_product,
        "expected_roi": odds_product * probability_product - 1,
        "target_reached": target <= odds_product <= target * 1.20,
        "independence_assumption": True,
        "correlation_warnings": correlation_warnings,
        "correlation_note": "Same-competition/date pairs receive a conservative 3% dependence haircut each. This is a heuristic, not measured correlation; other dependencies are not quantified.",
        "reason": None if target <= odds_product <= target * 1.20 else "No combination found within 20% above target.",
    }
