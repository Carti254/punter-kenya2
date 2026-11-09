"""Advanced football model primitives used only when source inputs are present.

Elo is chronological and requires dated historical results. Monte Carlo is a
scoreline simulation from supplied expected-goal rates; neither model is
advertised as calibrated without out-of-sample validation.
"""
from __future__ import annotations

import math
import random
from datetime import date


def elo_expected(rating_a: float, rating_b: float, home_advantage: float = 0.0) -> float:
    """Expected score for side A, with optional home-advantage Elo points."""
    a, b, h = float(rating_a), float(rating_b), float(home_advantage)
    if not all(math.isfinite(x) for x in (a, b, h)):
        raise ValueError("Elo inputs must be finite.")
    return 1.0 / (1.0 + 10.0 ** (-(a + h - b) / 400.0))


def build_elo_ratings(matches, initial_rating: float = 1500.0, k_factor: float = 20.0,
                      home_advantage: float = 60.0, before_date: str | None = None) -> dict:
    """Build ratings from chronological {date,home,away,home_goals,away_goals} rows."""
    ratings = {}
    cutoff = date.fromisoformat(before_date[:10]) if before_date else None
    ordered = []
    for row in matches or []:
        try:
            played = date.fromisoformat(str(row["date"])[:10])
            hg, ag = int(row["home_goals"]), int(row["away_goals"])
            home, away = str(row["home"]).strip(), str(row["away"]).strip()
            if not home or not away or min(hg, ag) < 0 or (cutoff and played >= cutoff):
                continue
            ordered.append((played, home, away, hg, ag))
        except (KeyError, TypeError, ValueError):
            continue
    ordered.sort(key=lambda x: x[0])
    for _, home, away, hg, ag in ordered:
        rh = ratings.get(home, float(initial_rating))
        ra = ratings.get(away, float(initial_rating))
        expected_h = elo_expected(rh, ra, home_advantage)
        actual_h = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        delta = float(k_factor) * (actual_h - expected_h)
        ratings[home], ratings[away] = rh + delta, ra - delta
    return ratings


def _poisson_sample(rng: random.Random, mean: float) -> int:
    if not math.isfinite(mean) or mean < 0 or mean > 20:
        raise ValueError("Poisson mean must be finite and between 0 and 20.")
    if mean == 0:
        return 0
    limit = math.exp(-mean)
    product, count = 1.0, 0
    while product > limit:
        count += 1
        product *= rng.random()
    return count - 1


def monte_carlo_scoreline(lambda_home: float, lambda_away: float, simulations: int = 20000,
                          seed: int | None = None) -> dict:
    """Simulate independent Poisson scores; return empirical outcome/goal probabilities."""
    lh, la = float(lambda_home), float(lambda_away)
    if not (math.isfinite(lh) and math.isfinite(la) and 0.05 <= lh <= 5 and 0.05 <= la <= 5):
        raise ValueError("Expected-goal inputs must be between 0.05 and 5.")
    if not isinstance(simulations, int) or simulations < 1000 or simulations > 500000:
        raise ValueError("simulations must be an integer from 1,000 to 500,000.")
    rng = random.Random(seed if seed is not None else f"{lh:.6f}:{la:.6f}")
    home = draw = away = over15 = over25 = btts = 0
    for _ in range(simulations):
        h, a = _poisson_sample(rng, lh), _poisson_sample(rng, la)
        home += h > a
        draw += h == a
        away += h < a
        over15 += h + a >= 2
        over25 += h + a >= 3
        btts += h > 0 and a > 0
    n = float(simulations)
    return {
        "simulations": simulations, "home": home / n, "draw": draw / n, "away": away / n,
        "over_1_5": over15 / n, "over_2_5": over25 / n, "btts": btts / n,
        "model": "independent-Poisson Monte Carlo",
        "warning": "Simulation inherits the supplied goal-rate assumptions; it is not an independent data source or calibrated forecast.",
    }


def pearson_correlation(xs, ys) -> float | None:
    """Estimate correlation only from paired observed samples; never infer it from labels."""
    if not isinstance(xs, (list, tuple)) or not isinstance(ys, (list, tuple)) or len(xs) != len(ys) or len(xs) < 5:
        return None
    try:
        x, y = [float(v) for v in xs], [float(v) for v in ys]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in x + y):
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 0 or vy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(vx * vy)
