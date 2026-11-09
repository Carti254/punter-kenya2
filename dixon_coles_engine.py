"""Single fitted, time-decayed Dixon-Coles football model.

Pure Python implementation. Fits competition-level scoring, team attack/defence,
home advantage and low-score dependence from completed results before kickoff.
No Elo, Monte Carlo, external probability blending or hard-coded goal-rate inputs.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime


def _team_key(value):
    raw = unicodedata.normalize("NFKD", str(value or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", raw)


def _match_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _valid_results(rows, before_date=None):
    cutoff = _match_date(before_date) if before_date is not None else None
    clean = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = _match_date(row.get("date") or row.get("match_date"))
        home, away = _team_key(row.get("home")), _team_key(row.get("away"))
        try:
            hg = int(row.get("ft_home", row.get("home_goals")))
            ag = int(row.get("ft_away", row.get("away_goals")))
        except (TypeError, ValueError, OverflowError):
            continue
        if not day or (cutoff and day >= cutoff) or not home or not away:
            continue
        if home == away or not (0 <= hg <= 15 and 0 <= ag <= 15):
            continue
        clean.append((day, home, away, hg, ag))
    clean.sort(key=lambda x: x[0])
    return clean


def poisson_pmf(lam, goals):
    if lam <= 0:
        return 1.0 if goals == 0 else 0.0
    return math.exp(-lam + goals * math.log(lam) - math.lgamma(goals + 1))


def _tau(hg, ag, lh, la, rho):
    if hg == 0 and ag == 0:
        return 1.0 - lh * la * rho
    if hg == 0 and ag == 1:
        return 1.0 + lh * rho
    if hg == 1 and ag == 0:
        return 1.0 + la * rho
    if hg == 1 and ag == 1:
        return 1.0 - rho
    return 1.0


def safe_rho(lambda_home, lambda_away, rho):
    """Keep Dixon-Coles low-score correction positive for this fixture."""
    lh, la, requested = float(lambda_home), float(lambda_away), float(rho)
    if not all(math.isfinite(x) for x in (lh, la, requested)):
        raise ValueError("Non-finite Dixon-Coles parameter.")
    if not (0.05 <= lh <= 6 and 0.05 <= la <= 6 and -0.20 <= requested <= 0.20):
        raise ValueError("Invalid expected-goal rates or Dixon-Coles rho.")
    lower = max(-0.20, -0.99 / lh, -0.99 / la)
    upper = min(0.20, 0.99 / (lh * la))
    return min(upper, max(lower, requested))


def score_matrix(lambda_home, lambda_away, max_goals=10, rho=0.0):
    lh, la = float(lambda_home), float(lambda_away)
    if not isinstance(max_goals, int) or not 4 <= max_goals <= 20:
        raise ValueError("max_goals must be an integer from 4 to 20.")
    rho_used = safe_rho(lh, la, rho)
    home = [poisson_pmf(lh, i) for i in range(max_goals + 1)]
    away = [poisson_pmf(la, j) for j in range(max_goals + 1)]
    matrix = []
    for i in range(max_goals + 1):
        line = []
        for j in range(max_goals + 1):
            value = home[i] * away[j] * _tau(i, j, lh, la, rho_used)
            if value < 0 or not math.isfinite(value):
                raise ArithmeticError("Dixon-Coles generated an invalid score probability.")
            line.append(value)
        matrix.append(line)
    total = sum(map(sum, matrix))
    if total <= 0 or not math.isfinite(total):
        raise ArithmeticError("Could not normalize score probabilities.")
    return [[v / total for v in line] for line in matrix]


def matrix_markets(matrix):
    n = len(matrix)
    home = sum(matrix[i][j] for i in range(n) for j in range(n) if i > j)
    draw = sum(matrix[i][i] for i in range(n))
    away = sum(matrix[i][j] for i in range(n) for j in range(n) if i < j)
    total_over = lambda line: sum(matrix[i][j] for i in range(n) for j in range(n) if i + j > line)
    total_under = lambda line: sum(matrix[i][j] for i in range(n) for j in range(n) if i + j < line)
    return {
        "1": home, "X": draw, "2": away,
        "1X": home + draw, "12": home + away, "X2": draw + away,
        "over_15": total_over(1.5), "over_25": total_over(2.5),
        "under_15": total_under(1.5), "under_25": total_under(2.5),
    }


def fit_dixon_coles(rows, before_date=None, half_life_days=180, min_matches=60,
                    min_team_matches=4, max_iterations=900, ridge=0.035):
    """Fit weighted Poisson attack/defence parameters, then fit rho by likelihood."""
    matches = _valid_results(rows, before_date)
    if len(matches) < min_matches:
        return {"available": False, "reason": f"Only {len(matches)} valid prior results; {min_matches} required.", "sample_size": len(matches)}
    teams = sorted({m[1] for m in matches} | {m[2] for m in matches})
    counts = defaultdict(int)
    for _, h, a, _, _ in matches:
        counts[h] += 1
        counts[a] += 1
    teams = [t for t in teams if counts[t] >= min_team_matches]
    eligible_teams = set(teams)
    matches = [m for m in matches if m[1] in eligible_teams and m[2] in eligible_teams]
    if len(matches) < min_matches or len(teams) < 4:
        return {"available": False, "reason": "Insufficient connected team history to fit this competition.", "sample_size": len(matches)}
    index = {team: i for i, team in enumerate(teams)}
    n = len(teams)
    latest = max(m[0] for m in matches)
    weights = [math.exp(-math.log(2) * max(0, (latest - m[0]).days) / max(1, half_life_days)) for m in matches]
    weighted_total = sum(weights)
    mean_h = sum(w * m[3] for w, m in zip(weights, matches)) / weighted_total
    mean_a = sum(w * m[4] for w, m in zip(weights, matches)) / weighted_total
    mu_h = math.log(max(0.15, mean_h))
    mu_a = math.log(max(0.15, mean_a))
    params = [mu_h, mu_a, 0.12] + [0.0] * (2 * n)
    # Adam minimizes weighted negative log likelihood plus shrinkage.
    m1 = [0.0] * len(params)
    m2 = [0.0] * len(params)
    beta1, beta2, eps, lr = 0.9, 0.999, 1e-8, 0.025

    def objective_gradient(p):
        grad = [0.0] * len(p)
        loss = 0.0
        for (day, h, a, hg, ag), weight in zip(matches, weights):
            hi, ai = index[h], index[a]
            lh = math.exp(max(-2.2, min(2.0, p[0] + p[2] + p[3 + hi] - p[3 + n + ai])))
            la = math.exp(max(-2.2, min(2.0, p[1] + p[3 + ai] - p[3 + n + hi])))
            # rho is fitted after the attack/defence fit; this stage uses independent Poisson.
            loss += weight * (lh - hg * math.log(lh) + math.lgamma(hg + 1) +
                              la - ag * math.log(la) + math.lgamma(ag + 1))
            gh, ga = weight * (lh - hg), weight * (la - ag)
            grad[0] += gh
            grad[1] += ga
            grad[2] += gh
            grad[3 + hi] += gh
            grad[3 + n + ai] -= gh
            grad[3 + ai] += ga
            grad[3 + n + hi] -= ga
        # Shrink team parameters to league average; remove unidentifiable drift.
        for k in range(3, len(p)):
            loss += ridge * p[k] * p[k]
            grad[k] += 2 * ridge * p[k]
        # Strongly centre attack and defence parameters at zero.
        for start in (3, 3 + n):
            avg = sum(p[start:start+n]) / n
            loss += 0.5 * n * avg * avg
            for k in range(start, start+n):
                grad[k] += avg
        return loss / weighted_total, [g / weighted_total for g in grad]

    previous = float("inf")
    for step in range(1, max_iterations + 1):
        loss, grad = objective_gradient(params)
        for k, g in enumerate(grad):
            m1[k] = beta1 * m1[k] + (1 - beta1) * g
            m2[k] = beta2 * m2[k] + (1 - beta2) * g * g
            mh = m1[k] / (1 - beta1 ** step)
            vh = m2[k] / (1 - beta2 ** step)
            params[k] -= lr * mh / (math.sqrt(vh) + eps)
        if abs(previous - loss) < 1e-9:
            break
        previous = loss

    # Fit low-score dependence by a bounded grid search on the same weighted likelihood.
    rho_candidates = [i / 1000 for i in range(-150, 151, 5)]
    best_rho, best_ll = 0.0, float("-inf")
    for rho in rho_candidates:
        ll = 0.0
        valid = True
        for (day, h, a, hg, ag), weight in zip(matches, weights):
            hi, ai = index[h], index[a]
            lh = math.exp(max(-2.2, min(2.0, params[0] + params[2] + params[3+hi] - params[3+n+ai])))
            la = math.exp(max(-2.2, min(2.0, params[1] + params[3+ai] - params[3+n+hi])))
            tau = _tau(hg, ag, lh, la, rho)
            if tau <= 0:
                valid = False
                break
            ll += weight * (math.log(tau) - lh + hg*math.log(lh) - math.lgamma(hg+1)
                            - la + ag*math.log(la) - math.lgamma(ag+1))
        if valid and ll > best_ll:
            best_ll, best_rho = ll, rho

    return {
        "available": True, "teams": teams, "team_index": index,
        "parameters": params, "team_count": n, "rho": best_rho,
        "sample_size": len(matches), "weighted_sample_size": weighted_total,
        "half_life_days": half_life_days, "latest_result_date": latest.isoformat(),
        "fit_loss": previous, "iterations": step,
        "method": "time-decayed Dixon-Coles maximum-likelihood fit",
    }


def predict_fixture(fit, home, away):
    if not fit or not fit.get("available"):
        return {"available": False, "reason": (fit or {}).get("reason", "Model fit unavailable.")}
    index, p, n = fit["team_index"], fit["parameters"], fit["team_count"]
    h, a = index.get(_team_key(home)), index.get(_team_key(away))
    if h is None or a is None:
        return {"available": False, "reason": "One or both teams are absent from the fitted competition history."}
    lh = math.exp(max(-2.2, min(2.0, p[0] + p[2] + p[3+h] - p[3+n+a])))
    la = math.exp(max(-2.2, min(2.0, p[1] + p[3+a] - p[3+n+h])))
    matrix = score_matrix(lh, la, 10, fit["rho"])
    markets = matrix_markets(matrix)
    return {
        "available": True, "lambda_home": lh, "lambda_away": la,
        "rho": safe_rho(lh, la, fit["rho"]), "fitted_rho": fit["rho"], "score_matrix": matrix,
        "home_win": markets["1"], "draw": markets["X"], "away_win": markets["2"],
        "over_15": markets["over_15"], "over_25": markets["over_25"],
        "score": max(((matrix[i][j], i, j) for i in range(len(matrix)) for j in range(len(matrix))), key=lambda x:x[0])[1:],
        "model_version": "dixon-coles-fitted-v1",
        "probability_source": "Fitted time-decayed Dixon-Coles",
        "training_sample": fit["sample_size"], "training_teams": fit["team_count"],
        "half_life_days": fit["half_life_days"], "latest_result_date": fit["latest_result_date"],
    }


def selected_outcome_probability(lambda_home, lambda_away, selection, home, away,
                                 max_goals=10, rho=0.0):
    key = re.sub(r"[^a-z0-9]+", "", str(selection or "").casefold())
    h, a = _team_key(home), _team_key(away)
    side = "1" if key in {"1", "home", "hometeam", h} else "X" if key in {"x", "draw", "tie"} else "2" if key in {"2", "away", "awayteam", a} else None
    if side is None:
        return None
    return matrix_markets(score_matrix(lambda_home, lambda_away, max_goals, rho))[side]
