"""Auditable pre-match 1X2 ensemble: Dixon-Coles, Elo and Monte Carlo.

This is a research baseline, not a calibrated betting model. It requires historical
results strictly before kick-off. The Monte Carlo component is a simulation of an
independent Poisson score model; probabilities must be calibrated out of sample
before being described as reliable.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict


def _poisson_pmf(k: int, mean: float) -> float:
    return math.exp(-mean) * mean ** k / math.factorial(k)


def _normalise(values: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(v)) for v in values.values())
    if total <= 0:
        raise ValueError("Probability mass is zero.")
    return {k: max(0.0, float(v)) / total for k, v in values.items()}


def dixon_coles_1x2(lambda_home: float, lambda_away: float, rho: float = -0.08,
                    max_goals: int = 10) -> dict[str, float]:
    """Dixon-Coles low-score adjusted score grid and 1X2 probabilities."""
    lh, la = float(lambda_home), float(lambda_away)
    if not (0 < lh <= 8 and 0 < la <= 8 and -0.2 <= rho <= 0.2):
        raise ValueError("Invalid expected goals or Dixon-Coles rho.")
    outcomes = {"1": 0.0, "X": 0.0, "2": 0.0}
    for hg in range(max_goals + 1):
        ph = _poisson_pmf(hg, lh)
        for ag in range(max_goals + 1):
            pa = _poisson_pmf(ag, la)
            tau = 1.0
            if hg == 0 and ag == 0:
                tau = 1.0 - (lh * la * rho)
            elif hg == 0 and ag == 1:
                tau = 1.0 + (lh * rho)
            elif hg == 1 and ag == 0:
                tau = 1.0 + (la * rho)
            elif hg == 1 and ag == 1:
                tau = 1.0 - rho
            mass = max(0.0, ph * pa * tau)
            outcomes["1" if hg > ag else "2" if hg < ag else "X"] += mass
    return _normalise(outcomes)


def elo_1x2(home: str, away: str, history: list[dict], home_advantage: float = 55.0,
            k_factor: float = 20.0) -> dict[str, float]:
    """Replay historical scores chronologically and return Elo-derived 1X2 probabilities."""
    ratings = defaultdict(lambda: 1500.0)
    parsed = []
    for row in history or []:
        try:
            date = str(row.get("date") or "")
            hg, ag = int(row.get("ft_home")), int(row.get("ft_away"))
            h, a = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
            if h and a and hg >= 0 and ag >= 0:
                parsed.append((date, h, a, hg, ag))
        except (TypeError, ValueError):
            continue
    parsed.sort(key=lambda item: item[0])
    draw_count = 0
    match_count = 0
    for _, h, a, hg, ag in parsed:
        rh, ra = ratings[h.casefold()], ratings[a.casefold()]
        expected_h = 1.0 / (1.0 + 10.0 ** ((ra - rh - home_advantage) / 400.0))
        actual_h = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        margin = max(1.0, math.log1p(abs(hg - ag)))
        change = k_factor * margin * (actual_h - expected_h)
        ratings[h.casefold()] += change
        ratings[a.casefold()] -= change
        draw_count += int(hg == ag)
        match_count += 1
    diff = ratings[str(home).casefold()] - ratings[str(away).casefold()] + home_advantage
    expected_home_score = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
    draw_rate = min(0.38, max(0.18, draw_count / match_count if match_count >= 20 else 0.26))
    # Elo expected score equals P(home win) + 0.5 * P(draw).
    # Given the empirical draw-rate prior, solve for the win probabilities.
    home_win = min(1.0 - draw_rate, max(0.0, expected_home_score - draw_rate / 2.0))
    away_win = max(0.0, 1.0 - draw_rate - home_win)
    return _normalise({"1": home_win, "X": draw_rate, "2": away_win})


def monte_carlo_1x2(lambda_home: float, lambda_away: float, simulations: int = 20000,
                    seed: int = 254) -> dict[str, float]:
    """Simulate independent Poisson scores with a reproducible random seed."""
    lh, la = float(lambda_home), float(lambda_away)
    if not (0 < lh <= 8 and 0 < la <= 8):
        raise ValueError("Invalid expected goals.")
    if not isinstance(simulations, int) or simulations < 1000:
        raise ValueError("At least 1,000 Monte Carlo simulations are required.")
    rng = random.Random(seed)

    def draw_poisson(mean: float) -> int:
        limit = math.exp(-mean)
        product, count = 1.0, 0
        while product > limit:
            count += 1
            product *= rng.random()
        return count - 1

    counts = {"1": 0, "X": 0, "2": 0}
    for _ in range(simulations):
        hg, ag = draw_poisson(lh), draw_poisson(la)
        counts["1" if hg > ag else "2" if hg < ag else "X"] += 1
    return {key: value / simulations for key, value in counts.items()}


def build_ensemble(home: str, away: str, lambda_home: float, lambda_away: float,
                   history: list[dict], simulations: int = 20000) -> dict:
    """Blend independent model families and expose disagreement and upset risk."""
    dc = dixon_coles_1x2(lambda_home, lambda_away)
    elo = elo_1x2(home, away, history)
    mc = monte_carlo_1x2(lambda_home, lambda_away, simulations=simulations)
    blended = _normalise({
        key: 0.50 * dc[key] + 0.30 * elo[key] + 0.20 * mc[key]
        for key in ("1", "X", "2")
    })
    disagreement = max(
        max(abs(dc[k] - elo[k]), abs(dc[k] - mc[k]), abs(elo[k] - mc[k]))
        for k in ("1", "X", "2")
    )
    return {
        "probabilities": blended,
        "components": {"dixon_coles": dc, "elo": elo, "monte_carlo": mc},
        "model_disagreement": disagreement,
        "upset_risk": {
            "home_favourite_away_win_probability": blended["2"] if blended["1"] >= blended["2"] else None,
            "away_favourite_home_win_probability": blended["1"] if blended["2"] > blended["1"] else None,
            "draw_probability": blended["X"],
            "level": "HIGH" if disagreement >= 0.25 else "MEDIUM" if disagreement >= 0.15 else "LOW",
        },
        "method": "50% Dixon-Coles + 30% Elo + 20% Monte Carlo; experimental, uncalibrated",
        "simulations": simulations,
        "history_matches": len(history or []),
    }
