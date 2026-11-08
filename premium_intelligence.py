"""Premium research primitives: transparent Poisson markets, price value and bankroll controls.

No network calls and no fabricated inputs. Callers must supply evidence-backed goal
rates and observed bookmaker prices. These utilities deliberately expose assumptions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _number(value, label, minimum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be numeric.") from None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{label} must be finite and at least {minimum}.")
    return result


def poisson_pmf(rate: float, goals: int) -> float:
    rate = _number(rate, "Goal rate", 0)
    if not isinstance(goals, int) or goals < 0:
        raise ValueError("Goals must be a non-negative integer.")
    return math.exp(-rate) * rate ** goals / math.factorial(goals)


def score_matrix(home_rate: float, away_rate: float, max_goals: int = 12):
    """Independent-Poisson score matrix; tail mass is reported, not hidden."""
    home_rate = _number(home_rate, "Home rate", 0)
    away_rate = _number(away_rate, "Away rate", 0)
    if not isinstance(max_goals, int) or not 4 <= max_goals <= 30:
        raise ValueError("max_goals must be an integer from 4 to 30.")
    hp = [poisson_pmf(home_rate, i) for i in range(max_goals + 1)]
    ap = [poisson_pmf(away_rate, i) for i in range(max_goals + 1)]
    matrix = [[h * a for a in ap] for h in hp]
    return matrix, max(0.0, 1.0 - sum(map(sum, matrix)))


def market_probabilities(home_rate: float, away_rate: float, max_goals: int = 12):
    matrix, tail = score_matrix(home_rate, away_rate, max_goals)
    home = draw = away = over15 = over25 = over35 = btts = 0.0
    for h, row in enumerate(matrix):
        for a, p in enumerate(row):
            if h > a: home += p
            elif h == a: draw += p
            else: away += p
            if h + a >= 2: over15 += p
            if h + a >= 3: over25 += p
            if h + a >= 4: over35 += p
            if h > 0 and a > 0: btts += p
    # Tail mass is not silently assigned. These probabilities are truncated estimates.
    return {
        "1": home, "X": draw, "2": away,
        "1X": home + draw, "X2": draw + away, "12": home + away,
        "Over 1.5": over15, "Under 1.5": 1 - over15,
        "Over 2.5": over25, "Under 2.5": 1 - over25,
        "Over 3.5": over35, "Under 3.5": 1 - over35,
        "BTTS Yes": btts, "BTTS No": 1 - btts,
        "_tail_mass": tail,
        "_model": "independent_poisson_truncated",
    }


def remove_margin(decimal_odds):
    """Proportional de-vig for a complete mutually-exclusive market."""
    odds = [_number(x, "Odds", 1.0000001) for x in decimal_odds]
    if len(odds) < 2:
        raise ValueError("At least two prices are needed to remove market margin.")
    raw = [1 / x for x in odds]
    total = sum(raw)
    return {"probabilities": [x / total for x in raw], "overround": total - 1}


def assess_price(probability, decimal_odds, minimum_edge=0.0):
    p = _number(probability, "Probability", 0)
    odds = _number(decimal_odds, "Decimal odds", 1.0000001)
    edge = p - 1 / odds
    ev = p * (odds - 1) - (1 - p)
    return {
        "probability": p, "implied_probability": 1 / odds,
        "fair_odds": (1 / p if p > 0 else None),
        "edge": edge, "expected_roi": ev,
        "qualifies": edge >= _number(minimum_edge, "Minimum edge"),
    }


def kelly_fraction(probability, decimal_odds, fraction=0.25, cap=0.02):
    """Fractional Kelly stake fraction, capped; zero for non-positive edge."""
    p = _number(probability, "Probability", 0)
    if p > 1:
        raise ValueError("Probability cannot exceed 1.")
    odds = _number(decimal_odds, "Decimal odds", 1.0000001)
    frac = _number(fraction, "Kelly fraction", 0)
    limit = _number(cap, "Stake cap", 0)
    b = odds - 1
    full = max(0.0, (b * p - (1 - p)) / b)
    return min(limit, full * frac)


def stake_advice(bankroll, probability, decimal_odds, fraction=0.25, cap=0.02):
    bank = _number(bankroll, "Bankroll", 0)
    fraction_of_bank = kelly_fraction(probability, decimal_odds, fraction, cap)
    return {"bankroll": bank, "stake_fraction": fraction_of_bank,
            "stake": round(bank * fraction_of_bank, 2),
            "warning": "Model estimates are uncertain. Flat small stakes or no bet may be safer; never chase losses."}


def performance_summary(rows):
    """Summarize settled records supplied by caller; excludes pending and voids from ROI."""
    settled = [r for r in rows if str(r.get("status", "")).upper() in {"WON", "LOST"}]
    wins = sum(str(r.get("status", "")).upper() == "WON" for r in settled)
    turnover = sum(float(r.get("stake") or 0) for r in settled)
    pnl = sum(float(r.get("profit_loss") or 0) for r in settled)
    return {"settled": len(settled), "wins": wins, "losses": len(settled) - wins,
            "turnover": turnover, "profit_loss": pnl,
            "roi": pnl / turnover if turnover else None}


def rollover_projection(bankroll, days, daily_target):
    bank = _number(bankroll, "Starting bankroll", 0)
    target = _number(daily_target, "Daily target odds", 1.0000001)
    if not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("Days must be an integer from 1 to 365.")
    # Mathematical compounding illustration only; not a betting recommendation.
    return {"starting_bankroll": bank, "days": days, "daily_target_odds": target,
            "illustrative_gross_if_every_day_won": bank * target ** days,
            "warning": "This is a mathematical illustration, not a forecast. A losing day can break the sequence; do not stake essential money."}
