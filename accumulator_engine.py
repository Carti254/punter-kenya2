"""SportyBet accumulator construction with a statistical-quality ranking.

Quality mode accepts only selections carrying a supported model probability,
positive estimated value and sufficient historical samples. It searches across
leg counts instead of stopping at the first two-leg combination.
"""
from __future__ import annotations
import math


def _fixture_key(row):
    return (
        str(row.get("day") or row.get("match_date") or "")[:10],
        str(row.get("home") or "").strip().casefold(),
        str(row.get("away") or "").strip().casefold(),
    )


def build_accumulator(candidates, target_odds, max_legs=20, beam_width=2500,
                      max_candidates=1000, ranking="price", min_legs=1):
    target = float(target_odds)
    if not math.isfinite(target) or not 2.0 <= target <= 5.0:
        raise ValueError("Target combined odds must be between 2.00 and 5.00.")
    if not isinstance(max_legs, int) or not 1 <= max_legs <= 20:
        raise ValueError("max_legs must be 1–20.")
    if not isinstance(min_legs, int) or not 1 <= min_legs <= max_legs:
        raise ValueError("min_legs must be 1–max_legs.")
    if ranking not in {"price", "quality"}:
        raise ValueError("ranking must be 'price' or 'quality'.")

    upper = min(target * 1.10, 5.0)
    clean, seen = [], set()
    for row in candidates or []:
        try:
            odd = float(row.get("odds"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(odd) or not 1.15 <= odd <= 3.50:
            continue
        if str(row.get("bookmaker") or "").casefold().replace(" ", "") != "sportybet":
            continue
        if not (row.get("eventId") or row.get("event_id")):
            continue
        if not (row.get("marketId") or row.get("market_id")):
            continue
        if not (row.get("outcomeId") or row.get("outcome_id")):
            continue
        if row.get("fixture_match_verified") is not True:
            continue
        market_text = " ".join(str(row.get(k) or "") for k in ("market", "selection", "specifier")).casefold()
        excluded = (
            "btts", "both teams to score", "under 2.5", "draw no bet", " dnb",
            "2up", "early payout", "handicap", "corner", "card", "booking",
            "shots", "player", "correct score", "half time", "first half", "second half",
        )
        if any(term in market_text for term in excluded):
            continue
        market_name = str(row.get("market") or "").casefold()
        selection_name = str(row.get("selection") or "").casefold()
        supported = any(term in market_name for term in (
            "1x2", "match result", "full time result", "fulltime result", "double chance"
        )) or (
            ("total goals" in market_name or "over/under" in market_name or "over under" in market_name)
            and any(term in (market_name + " " + selection_name) for term in ("over 1.5", "over 2.5"))
        )
        if not supported:
            continue
        probability = None
        if ranking == "quality":
            try:
                probability = float(row.get("model_probability"))
                edge = float(row.get("expected_value"))
                home_sample = int(row.get("model_home_sample") or 0)
                away_sample = int(row.get("model_away_sample") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(probability) or not 0.60 <= probability < 1.0:
                continue
            if not math.isfinite(edge) or edge < 0.03:
                continue
            if home_sample < 6 or away_sample < 6:
                continue
        home, away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
        if not home or not away:
            continue
        fixture = _fixture_key(row)
        if not fixture[1] or not fixture[2]:
            continue
        key = (fixture, str(row.get("marketId") or row.get("market_id")),
               str(row.get("outcomeId") or row.get("outcome_id")))
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["odds"] = odd
        item["_fixture"] = fixture
        item["_probability"] = probability
        clean.append(item)

    if ranking == "quality":
        clean.sort(key=lambda x: (-x["_probability"], -float(x.get("expected_value") or 0),
                                  x["odds"], x["_fixture"]))
    else:
        clean.sort(key=lambda x: (x["odds"], x["_fixture"],
                                  str(x.get("market") or ""), str(x.get("selection") or "")))
    clean = clean[:max(1, int(max_candidates))]
    if not clean:
        return {"selections": [], "combined_odds": 1.0, "target_reached": False,
                "reason": "No executable SportyBet prices passed the required checks."}

    # A beam state is (picks, combined odds, used fixtures, next index, joint P).
    states = [([], 1.0, frozenset(), 0, 1.0)]
    complete = []
    for _depth in range(max_legs):
        expanded = []
        for picks, product, used, start, joint_probability in states:
            for index in range(start, len(clean)):
                item = clean[index]
                if item["_fixture"] in used:
                    continue
                new_product = product * item["odds"]
                if new_product > upper:
                    continue
                new_picks = picks + [item]
                new_probability = joint_probability * (item["_probability"] or 1.0)
                state = (new_picks, new_product, used | {item["_fixture"]},
                         index + 1, new_probability)
                if len(new_picks) >= min_legs and target <= new_product <= upper:
                    complete.append(state)
                elif len(new_picks) < max_legs and new_product < target:
                    expanded.append(state)
        if not expanded:
            break
        if ranking == "quality":
            expanded.sort(key=lambda x: (
                -x[4], -(x[4] * x[1] - 1.0),
                abs(math.log(max(target, 1.000001) / x[1])), len(x[0]),
            ))
        else:
            expanded.sort(key=lambda x: (
                abs(math.log(max(target, 1.000001) / x[1])), len(x[0]), x[1],
            ))
        states = expanded[:max(1, int(beam_width))]

    if not complete:
        return {"selections": [], "combined_odds": 1.0, "target_reached": False,
                "reason": "No combination of distinct fixtures reached the target using qualified prices."}

    if ranking == "quality":
        picks, product, _, _, joint_probability = min(
            complete,
            key=lambda x: (-x[4], -(x[4] * x[1] - 1.0),
                           abs(x[1] - target), len(x[0])),
        )
    else:
        picks, product, _, _, joint_probability = min(
            complete, key=lambda x: (len(x[0]), x[1] - target, x[1])
        )

    selections = [{k: v for k, v in item.items()
                   if k not in {"_fixture", "_probability"}} for item in picks]
    return {
        "selections": selections,
        "combined_odds": product,
        "target_reached": True,
        "selection_method": (
            "Statistically qualified SportyBet selections; model probability and estimated value ranking."
            if ranking == "quality" else
            "Executable SportyBet price and market-rule accumulator; no probability prediction."
        ),
        "combined_probability": joint_probability if ranking == "quality" else None,
        "expected_return_factor": joint_probability * product if ranking == "quality" else None,
        "expected_roi": joint_probability * product - 1.0 if ranking == "quality" else None,
        "independence_assumption": True if ranking == "quality" else None,
        "reason": None,
    }
