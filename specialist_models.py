"""Evidence-led specialist football market models from API-Football recent match data.

These are transparent Poisson baselines, not calibrated forecasts. The provider's
coverage/plan limits and small recent samples are surfaced. Player props are withheld
until a stable named-player sample and confirmed line-up can be established.
"""
from __future__ import annotations

import math
import os
from datetime import date, timedelta

from football_stats_provider import _get, _team_key, FootballStatsError


STAT_ALIASES = {
    "corners": ("corner kicks", "corners"),
    "shots": ("total shots", "shots total"),
    "shots_on_target": ("shots on goal", "shots on target"),
    "yellow_cards": ("yellow cards",),
    "red_cards": ("red cards",),
}


def _stats_by_team(rows):
    result = {}
    for team_row in rows or []:
        team = (team_row.get("team") or {}).get("name")
        values = {}
        for stat in team_row.get("statistics") or []:
            key = str(stat.get("type") or "").strip().casefold()
            value = stat.get("value")
            if value is None:
                continue
            try:
                values[key] = float(str(value).replace("%", ""))
            except (TypeError, ValueError):
                continue
        if team:
            result[_team_key(team)] = values
    return result


def _poisson_over(mean, line):
    if mean < 0 or line < 0:
        raise ValueError("Mean and line must be non-negative.")
    cutoff = math.floor(float(line))
    cdf = sum(math.exp(-mean) * mean**k / math.factorial(k) for k in range(cutoff + 1))
    return max(0.0, min(1.0, 1.0 - cdf))


def _poisson_under(mean, line):
    if mean < 0 or line < 0:
        raise ValueError("Mean and line must be non-negative.")
    cutoff = math.ceil(float(line)) - 1
    if cutoff < 0:
        return 0.0
    return max(0.0, min(1.0, sum(math.exp(-mean) * mean**k / math.factorial(k) for k in range(cutoff + 1))))


def _value(values, aliases):
    for alias in aliases:
        if alias in values:
            return values[alias]
    return None


def _recent_team_observations(team_id, before_date, api_key, session, last=5):
    rows = _get("fixtures", {
        "team": int(team_id), "last": int(last), "to": (before_date - timedelta(days=1)).isoformat(),
        "timezone": "UTC",
    }, api_key, session)
    observations = []
    for fixture_row in rows:
        fixture = fixture_row.get("fixture") or {}
        fixture_id = fixture.get("id")
        if not fixture_id:
            continue
        stats_rows = _get("fixtures/statistics", {"fixture": int(fixture_id)}, api_key, session)
        by_team = _stats_by_team(stats_rows)
        teams = fixture_row.get("teams") or {}
        home = _team_key((teams.get("home") or {}).get("name"))
        away = _team_key((teams.get("away") or {}).get("name"))
        home_stats, away_stats = by_team.get(home, {}), by_team.get(away, {})
        home_ht = (fixture_row.get("score") or {}).get("halftime") or {}
        ht_total = None
        try:
            if home_ht.get("home") is not None and home_ht.get("away") is not None:
                ht_total = float(home_ht["home"]) + float(home_ht["away"])
        except (TypeError, ValueError):
            pass
        obs = {"half_time_goals": ht_total}
        for key, aliases in STAT_ALIASES.items():
            h, a = _value(home_stats, aliases), _value(away_stats, aliases)
            obs[key] = (h + a) if h is not None and a is not None else None
        observations.append(obs)
    return observations


def specialist_council(home, away, match_date, api_key=None, session=None, last=5):
    """Return independent specialist market assessments and an evidence-only council summary."""
    import requests
    session = session or requests
    key = api_key or os.getenv("API_FOOTBALL_KEY")
    if not key:
        raise FootballStatsError("Set API_FOOTBALL_KEY privately to enable specialist market research.")
    target = date.fromisoformat(str(match_date)[:10])
    fixtures = _get("fixtures", {"date": target.isoformat(), "timezone": "UTC"}, key, session)
    exact = []
    for row in fixtures:
        teams = row.get("teams") or {}
        if (_team_key((teams.get("home") or {}).get("name")) == _team_key(home)
                and _team_key((teams.get("away") or {}).get("name")) == _team_key(away)):
            exact.append(row)
    if len(exact) != 1:
        raise FootballStatsError(f"Expected one exact fixture for {home} vs {away} on {target}; found {len(exact)}.")
    teams = exact[0].get("teams") or {}
    home_team, away_team = teams.get("home") or {}, teams.get("away") or {}
    if not home_team.get("id") or not away_team.get("id"):
        raise FootballStatsError("The stats provider omitted one or both team IDs.")
    home_rows = _recent_team_observations(home_team["id"], target, key, session, last)
    away_rows = _recent_team_observations(away_team["id"], target, key, session, last)

    panels = []
    specs = [
        ("corners", "Corners analyst", "corners", 8.5),
        ("cards", "Cards analyst", "yellow_cards", 3.5),
        ("shots", "Shots analyst", "shots", 22.5),
        ("shots_on_target", "Shots-on-target analyst", "shots_on_target", 7.5),
        ("half_time_goals", "First-half analyst", "half_time_goals", 1.5),
    ]
    for key_name, role, field, default_line in specs:
        h = [r[field] for r in home_rows if r.get(field) is not None]
        a = [r[field] for r in away_rows if r.get(field) is not None]
        if len(h) < 3 or len(a) < 3:
            panels.append({"role": role, "market": key_name, "available": False,
                           "reason": f"Insufficient recent data (home {len(h)}, away {len(a)}; minimum 3 each)."})
            continue
        mean = (sum(h) / len(h) + sum(a) / len(a)) / 2
        panels.append({
            "role": role, "market": key_name, "available": True,
            "home_sample": len(h), "away_sample": len(a), "expected_total": mean,
            "default_line": default_line,
            "over_probability": _poisson_over(mean, default_line),
            "under_probability": _poisson_under(mean, default_line),
            "model": "recent-form Poisson baseline",
        })
    available = [p for p in panels if p["available"]]
    return {
        "fixture": f"{home} vs {away}", "date": target.isoformat(),
        "provider": "API-Football", "panels": panels,
        "council": {
            "available_panels": len(available),
            "unsupported_panels": [p["market"] for p in panels if not p["available"]],
            "consensus": "No consensus recommendation is issued; each market requires its own price and calibration.",
        },
        "player_props": {
            "available": False,
            "reason": "Named-player props are withheld until confirmed line-ups and a sufficiently large player-level sample are available.",
        },
        "warning": "Experimental recent-form Poisson estimates, not calibrated probabilities. Verify coverage, line-ups and bookmaker settlement rules."
    }
