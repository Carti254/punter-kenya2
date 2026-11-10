"""Single, transparent pre-match probability and value engine.

Uses only completed Open-Matchday scorelines dated before the fixture. It does
not claim calibration, xG, line-up or injury information. Unsupported markets
and teams without sufficient history are rejected rather than guessed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import math
import re
import unicodedata

MIN_TEAM_MATCHES = 6
MIN_VALUE_EDGE = 0.03
MIN_SELECTION_PROBABILITY = 0.60
MAX_GOALS = 8


def _key(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", value.casefold())
    aliases = {"man utd": "manchester united", "manchester utd": "manchester united",
               "psg": "paris saint germain"}
    phrase = " ".join(words)
    phrase = aliases.get(phrase, phrase)
    return " ".join(x for x in phrase.split() if x not in {"fc", "cf", "sc", "afc", "football", "club"})


def _date(value):
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _weighted_mean(rows, field):
    weights = [0.88 ** i for i in range(len(rows))]
    total = sum(weights)
    return sum(float(row[field]) * weight for row, weight in zip(rows, weights)) / total


def _team_rows(history, team, cutoff):
    wanted = _key(team)
    found = []
    seen = set()
    for raw in history or []:
        if not isinstance(raw, dict):
            continue
        match_day = _date(raw.get("date"))
        if not match_day or match_day >= cutoff:
            continue
        home, away = _key(raw.get("home")), _key(raw.get("away"))
        if wanted not in (home, away):
            continue
        try:
            hg, ag = int(raw.get("ft_home")), int(raw.get("ft_away"))
            if hg < 0 or ag < 0:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        key = (match_day, home, away, hg, ag)
        if key in seen:
            continue
        seen.add(key)
        is_home = home == wanted
        gf, ga = (hg, ag) if is_home else (ag, hg)
        found.append({"date": match_day, "venue": "H" if is_home else "A",
                      "gf": gf, "ga": ga})
    found.sort(key=lambda x: x["date"], reverse=True)
    return found[:12]


def _prepare_history(history):
    """Index team results and calculate the shared scoring baseline once per scan."""
    index = defaultdict(list)
    league_home, league_away = [], []
    for raw in history or []:
        if not isinstance(raw, dict):
            continue
        match_day = _date(raw.get("date"))
        home, away = _key(raw.get("home")), _key(raw.get("away"))
        if not match_day or not home or not away:
            continue
        try:
            hg, ag = int(raw.get("ft_home")), int(raw.get("ft_away"))
            if hg < 0 or ag < 0:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        league_home.append(hg)
        league_away.append(ag)
        index[home].append({"date": match_day, "venue": "H", "gf": hg, "ga": ag})
        index[away].append({"date": match_day, "venue": "A", "gf": ag, "ga": hg})
    for rows in index.values():
        rows.sort(key=lambda item: item["date"], reverse=True)
    baseline = (
        len(league_home),
        sum(league_home) / len(league_home) if league_home else 0.0,
        sum(league_away) / len(league_away) if league_away else 0.0,
    )
    return index, baseline


def _indexed_team_rows(history_index, team, cutoff):
    wanted = _key(team)
    if not wanted:
        return []
    return [row for row in history_index.get(wanted, []) if row["date"] < cutoff][:12]


def _blend(team_rows, venue, field, fallback):
    selected = [row for row in team_rows if row["venue"] == venue][:6]
    if len(selected) >= 3:
        venue_mean = _weighted_mean(selected, field)
        overall_mean = _weighted_mean(team_rows, field)
        # Venue-specific evidence is useful but deliberately shrunk toward
        # the team's broader recent record to reduce small-sample volatility.
        return 0.65 * venue_mean + 0.35 * overall_mean
    return fallback


def _poisson(rate):
    values = [math.exp(-rate) * (rate ** goals) / math.factorial(goals)
              for goals in range(MAX_GOALS + 1)]
    total = sum(values)
    return [value / total for value in values]


def _prediction(home_rows, away_rows, history, baseline=None):
    if len(home_rows) < MIN_TEAM_MATCHES or len(away_rows) < MIN_TEAM_MATCHES:
        return None
    if baseline is None:
        history_index, baseline = _prepare_history(history)
    baseline_count, home_avg, away_avg = baseline
    if baseline_count < 100:
        return None
    if home_avg <= 0 or away_avg <= 0:
        return None

    home_attack = _blend(home_rows, "H", "gf", _weighted_mean(home_rows, "gf"))
    home_defence = _blend(home_rows, "H", "ga", _weighted_mean(home_rows, "ga"))
    away_attack = _blend(away_rows, "A", "gf", _weighted_mean(away_rows, "gf"))
    away_defence = _blend(away_rows, "A", "ga", _weighted_mean(away_rows, "ga"))

    home_rate = home_avg * (home_attack / home_avg) * (away_defence / home_avg)
    away_rate = away_avg * (away_attack / away_avg) * (home_defence / away_avg)
    # Keep extreme tiny-sample rates from dominating the score matrix.
    home_rate = min(3.5, max(0.15, home_rate))
    away_rate = min(3.5, max(0.15, away_rate))

    hp, ap = _poisson(home_rate), _poisson(away_rate)
    matrix = [[h * a for a in ap] for h in hp]
    total = sum(sum(row) for row in matrix)
    matrix = [[value / total for value in row] for row in matrix]
    p_home = sum(matrix[h][a] for h in range(len(matrix)) for a in range(len(matrix[h])) if h > a)
    p_draw = sum(matrix[h][a] for h in range(len(matrix)) for a in range(len(matrix[h])) if h == a)
    p_away = max(0.0, 1.0 - p_home - p_draw)
    p_over15 = sum(matrix[h][a] for h in range(len(matrix)) for a in range(len(matrix[h])) if h + a >= 2)
    p_over25 = sum(matrix[h][a] for h in range(len(matrix)) for a in range(len(matrix[h])) if h + a >= 3)
    return {"home": p_home, "draw": p_draw, "away": p_away,
            "over15": p_over15, "over25": p_over25,
            "home_rate": home_rate, "away_rate": away_rate,
            "home_sample": len(home_rows), "away_sample": len(away_rows)}


def _market_probability(row, prediction):
    market = re.sub(r"[^a-z0-9.+]", " ", str(row.get("market") or "").casefold())
    selection = re.sub(r"[^a-z0-9.+]", " ", str(row.get("selection") or "").casefold())
    combined = market + " " + selection
    if (any(term in combined for term in ("btts", "both teams", "draw no bet",
                                           "handicap", "corner", "card", "player",
                                           "correct score", "half time", "first half",
                                           "second half", "early payout", "2up"))
            or "under" in selection
            or ("under" in market and "over" not in selection)):
        return None, "unsupported or excluded market"
    if "double chance" in market:
        if selection in ("home or draw", "1x", "home draw"):
            return prediction["home"] + prediction["draw"], "double chance 1X"
        if selection in ("home or away", "12", "home away"):
            return prediction["home"] + prediction["away"], "double chance 12"
        if selection in ("draw or away", "x2", "draw away"):
            return prediction["draw"] + prediction["away"], "double chance X2"
        return None, "unrecognised double-chance selection"
    if any(term in market for term in ("1x2", "match result", "full time result", "fulltime result")):
        mapping = {"home": prediction["home"], "home win": prediction["home"], "1": prediction["home"],
                   "draw": prediction["draw"], "x": prediction["draw"],
                   "away": prediction["away"], "away win": prediction["away"], "2": prediction["away"]}
        home_name, away_name = _key(row.get("home")), _key(row.get("away"))
        selected_name = _key(row.get("selection"))
        if selected_name and selected_name == home_name:
            return prediction["home"], "1X2 home"
        if selected_name and selected_name == away_name:
            return prediction["away"], "1X2 away"
        return (mapping[selection], "1X2") if selection in mapping else (None, "unrecognised 1X2 selection")
    if "over" in selection or "over" in market:
        if "1.5" in selection or "1.5" in market:
            return prediction["over15"], "Over 1.5"
        if "2.5" in selection or "2.5" in market:
            return prediction["over25"], "Over 2.5"
    return None, "market not covered by the model"


def assess_candidate(row, history, history_index=None, baseline=None):
    """Attach a conservative model estimate and reject insufficient/negative value."""
    try:
        kickoff_day = _date(str(row.get("kickoff") or row.get("day") or "")[:10])
        odds = float(row.get("odds"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not kickoff_day or not math.isfinite(odds) or odds <= 1:
        return None
    if history_index is None or baseline is None:
        history_index, baseline = _prepare_history(history)
    home_rows = _indexed_team_rows(history_index, row.get("home"), kickoff_day)
    away_rows = _indexed_team_rows(history_index, row.get("away"), kickoff_day)
    prediction = _prediction(home_rows, away_rows, history, baseline)
    if prediction is None:
        return None
    probability, market_label = _market_probability(row, prediction)
    if probability is None or not math.isfinite(probability) or not 0 < probability <= 1:
        return None
    implied = 1.0 / odds
    expected_value = probability * odds - 1.0
    if probability < MIN_SELECTION_PROBABILITY or expected_value < MIN_VALUE_EDGE:
        return None
    return {
        **row,
        "model_probability": probability,
        "implied_probability": implied,
        "fair_odds": 1.0 / probability,
        "expected_value": expected_value,
        "model_market": market_label,
        "model_home_rate": prediction["home_rate"],
        "model_away_rate": prediction["away_rate"],
        "model_home_sample": prediction["home_sample"],
        "model_away_sample": prediction["away_sample"],
        "selection_method": "Recent-results Poisson estimate; uncalibrated; value threshold +3%",
    }



def assess_candidates(rows, history):
    """Assess a complete SportyBet candidate batch using one indexed history pass."""
    history_index, baseline = _prepare_history(history)
    qualified = []
    for row in rows or []:
        item = assess_candidate(row, history, history_index, baseline)
        if item is not None:
            qualified.append(item)
    return qualified
