"""Constrained accumulator construction from priced, model-supported candidates.

Searches for a target-odds combination while allowing only one leg per fixture.
The objective maximises the product of model probability and offered odds (expected
payout factor) under an independence assumption; that assumption is surfaced explicitly.
"""
from __future__ import annotations

import math


def build_accumulator(candidates, target_odds, max_legs=8, min_legs=1, beam_width=1800, max_candidates=60, ranking="value"):
    target = float(target_odds)
    if not math.isfinite(target) or target <= 1:
        raise ValueError("Target odds must be finite and greater than 1.")
    if not isinstance(max_legs, int) or not 1 <= max_legs <= 20:
        raise ValueError("max_legs must be an integer from 1 to 20.")
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
        item["_fixture_key"] = (home.casefold(), away.casefold())
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
        clean.sort(key=lambda x: (x["odds"], x["probability"]), reverse=True)
    else:
        clean.sort(key=lambda x: (x["_expected_factor"], x["probability"], x["odds"]), reverse=True)
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
        if ranking == "probability":
            return (state[2], state[1] * state[2], -len(state[0]))
        if ranking == "odds":
            return (state[1], state[1] * state[2], -len(state[0]))
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
            secondary = probability_now if ranking in {"probability", "odds"} else odds_now * probability_now
            return (-((odds_now / target) - 1.0), secondary, probability_now, -len(picks))
        best = max(completed, key=completion_score)
    else:
        # Return the best available partial construction, never pretend target was met.
        all_states = eligible_partials or states
        if not all_states:
            all_states = [([], 1.0, 1.0, frozenset(), 0)]
        if ranking == "probability":
            best = max(all_states, key=lambda s: (s[2], s[1], -len(s[0])))
        elif ranking == "odds":
            best = max(all_states, key=lambda s: (s[1], s[2], -len(s[0])))
        else:
            best = max(all_states, key=lambda s: (s[1] * s[2], s[2], -len(s[0])))
    picks, odds_product, probability_product, _, _ = best
    selections = []
    for item in picks:
        selections.append({k: v for k, v in item.items() if not k.startswith("_")})
    return {
        "selections": selections,
        "combined_odds": odds_product,
        "combined_probability": probability_product,
        "expected_return_factor": odds_product * probability_product,
        "expected_roi": odds_product * probability_product - 1,
        "target_reached": target <= odds_product <= target * 1.20,
        "independence_assumption": True,
        "reason": None if target <= odds_product <= target * 1.20 else "No combination found within 20% above target.",
    }
