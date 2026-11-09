"""Dixon-Coles score model ported from oKarun-b/sureodds.

Pure calculation module: no network, database, or bookmaker dependencies.
It is used only when PUNTER has valid expected-goal rates for an exact fixture.
"""
from __future__ import annotations

import math
import re


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and 0.05 <= result <= 5.0 else None


def poisson_pmf(lam: float, goals: int) -> float:
    if lam <= 0:
        return 1.0 if goals == 0 else 0.0
    return math.exp(-lam) * lam ** goals / math.factorial(goals)


def score_matrix(lambda_home: float, lambda_away: float, max_goals: int = 8,
                 rho: float = -0.05) -> list[list[float]]:
    """Return a normalized Dixon-Coles-adjusted score probability matrix."""
    lh, la = _number(lambda_home), _number(lambda_away)
    if lh is None or la is None:
        raise ValueError("Expected-goal rates must be finite values from 0.05 to 5.0.")
    if not isinstance(max_goals, int) or not 4 <= max_goals <= 12:
        raise ValueError("max_goals must be an integer between 4 and 12.")
    if not math.isfinite(float(rho)) or not -0.20 <= float(rho) <= 0.20:
        raise ValueError("rho must be finite and between -0.20 and 0.20.")

    home = [poisson_pmf(lh, i) for i in range(max_goals + 1)]
    away = [poisson_pmf(la, i) for i in range(max_goals + 1)]
    matrix = [[home[i] * away[j] for j in range(max_goals + 1)]
              for i in range(max_goals + 1)]

    # Dixon-Coles low-score dependence correction (0-0, 0-1, 1-0, 1-1).
    matrix[0][0] *= 1.0 - lh * la * rho
    matrix[0][1] *= 1.0 + lh * rho
    matrix[1][0] *= 1.0 + la * rho
    matrix[1][1] *= 1.0 - rho

    total = sum(sum(row) for row in matrix)
    if total <= 0 or not math.isfinite(total):
        raise ArithmeticError("Dixon-Coles score matrix could not be normalized.")
    return [[value / total for value in row] for row in matrix]


def outcome_probabilities(lambda_home: float, lambda_away: float,
                          max_goals: int = 8, rho: float = -0.05) -> dict[str, float]:
    matrix = score_matrix(lambda_home, lambda_away, max_goals, rho)
    n = len(matrix)
    return {
        "HOME": sum(matrix[i][j] for i in range(n) for j in range(n) if i > j),
        "DRAW": sum(matrix[i][i] for i in range(n)),
        "AWAY": sum(matrix[i][j] for i in range(n) for j in range(n) if i < j),
    }


def selected_outcome_probability(lambda_home: float, lambda_away: float,
                                 selection: str, home: str, away: str,
                                 max_goals: int = 8, rho: float = -0.05) -> float | None:
    """Resolve an exact 1X2 side and return its DC probability; otherwise None."""
    def norm(value):
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    picked = norm(selection)
    home_key, away_key = norm(home), norm(away)
    if picked in {"1", "home", "hometeam"} or (home_key and picked == home_key):
        side = "HOME"
    elif picked in {"x", "draw", "tie"}:
        side = "DRAW"
    elif picked in {"2", "away", "awayteam"} or (away_key and picked == away_key):
        side = "AWAY"
    else:
        return None
    return outcome_probabilities(lambda_home, lambda_away, max_goals, rho)[side]
