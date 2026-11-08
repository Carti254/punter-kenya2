"""Constrained accumulator construction from priced, model-supported candidates.

Searches for a target-odds combination while allowing only one leg per fixture.
The objective maximises the product of model probability and offered odds (expected
payout factor) under an independence assumption; that assumption is surfaced explicitly.
"""
from __future__ import annotations

import math


def build_accumulator(candidates, target_odds, max_legs=8, beam_width=1800, max_candidates=60):
    target = float(target_odds)
    if not math.isfinite(target) or target <= 1:
        raise ValueError("Target odds must be finite and greater than 1.")
    if not isinstance(max_legs, int) or not 1 <= max_legs <= 20:
        raise ValueError("max_legs must be an integer from 1 to 20.")
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
        clean.append(item)
    clean.sort(key=lambda x: (x["_expected_factor"], x["probability"], x["odds"]), reverse=True)
    clean = clean[:max_candidates]
    if not clean:
        return {"selections": [], "combined_odds": 1.0, "combined_probability": 0.0,
                "expected_return_factor": 0.0, "target_reached": False, "reason": "No valid candidates."}

    # A beam keeps this practical for Telegram requests with large fixture slates.
    states = [([], 1.0, 1.0, frozenset())]
    completed = []
    for _depth in range(max_legs):
        expanded = []
        for picks, odds_product, probability_product, used in states:
            for item in clean:
                fixture_key = item["_fixture_key"]
                if fixture_key in used:
                    continue
                new_odds = odds_product * item["odds"]
                new_probability = probability_product * item["probability"]
                new_picks = picks + [item]
                factor = new_odds * new_probability
                state = (new_picks, new_odds, new_probability, used | {fixture_key})
                if new_odds >= target:
                    completed.append(state)
                else:
                    expanded.append(state)
        if not expanded:
            break
        # Keep states near target and with stronger expected payout factors.
        expanded.sort(
            key=lambda s: (
                math.log(max(1e-15, s[1] * s[2]))
                - 0.20 * abs(math.log(target / s[1])),
                s[2], -len(s[0]),
            ),
            reverse=True,
        )
        states = expanded[:beam_width]

    if completed:
        # Highest expected payout factor; then probability; then fewer legs.
        best = max(completed, key=lambda s: (s[1] * s[2], s[2], -len(s[0])))
    else:
        # Return the best available partial construction, never pretend target was met.
        all_states = states
        if not all_states:
            all_states = [([], 1.0, 1.0, frozenset())]
        best = max(all_states, key=lambda s: (s[1], s[2], -len(s[0])))
    picks, odds_product, probability_product, _ = best
    selections = []
    for item in picks:
        selections.append({k: v for k, v in item.items() if not k.startswith("_")})
    return {
        "selections": selections,
        "combined_odds": odds_product,
        "combined_probability": probability_product,
        "expected_return_factor": odds_product * probability_product,
        "expected_roi": odds_product * probability_product - 1,
        "target_reached": odds_product >= target,
        "independence_assumption": True,
        "reason": None if odds_product >= target else "Best available combination did not reach the target.",
    }
