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
    "xg": ("expected goals", "expected_goals", "xg"),
    "possession": ("ball possession", "possession"),
    "passes": ("total passes", "passes"),
    "tackles": ("total tackles", "tackles"),
    "fouls": ("fouls",),
    "offsides": ("offsides",),
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
        "timezone": "Africa/Nairobi",
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
        home_id = (teams.get("home") or {}).get("id")
        own_stats = home_stats if str(home_id) == str(team_id) else away_stats
        for key, aliases in STAT_ALIASES.items():
            h, a = _value(home_stats, aliases), _value(away_stats, aliases)
            obs[key] = (h + a) if h is not None and a is not None else None
            obs["team_" + key] = _value(own_stats, aliases)
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
    fixtures = _get("fixtures", {"date": target.isoformat(), "timezone": "Africa/Nairobi"}, key, session)
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
    fixture_id = int(((exact[0].get("fixture") or {}).get("id") or 0))
    lineup_evidence = None
    team_news_evidence = None
    provider_warnings = []

    # API-Football exposes official lineups and fixture-specific injuries on
    # the free plan, but coverage varies. Never infer a clean bill of health
    # from an empty or unavailable response.
    if fixture_id:
        try:
            lineup_rows = _get("fixtures/lineups", {"fixture": fixture_id}, key, session)
            confirmed_sides = []
            for lineup in lineup_rows or []:
                team = (lineup.get("team") or {}).get("name") or "Team"
                starters = lineup.get("startXI") or []
                if starters:
                    confirmed_sides.append({
                        "team": team,
                        "formation": lineup.get("formation"),
                        "coach": (lineup.get("coach") or {}).get("name"),
                        "starters": [
                            (item.get("player") or {}).get("name")
                            for item in starters
                            if (item.get("player") or {}).get("name")
                        ],
                        "substitutes": [
                            (item.get("player") or {}).get("name")
                            for item in (lineup.get("substitutes") or [])
                            if (item.get("player") or {}).get("name")
                        ],
                    })
            if confirmed_sides:
                lineup_evidence = {
                    "provider": "API-Football",
                    "status": "provider lineup published",
                    "confirmed": True,
                    "sides": confirmed_sides,
                }
        except Exception as exc:
            provider_warnings.append(f"Lineups unavailable: {type(exc).__name__}")

        try:
            injury_rows = _get("injuries", {"fixture": fixture_id}, key, session)
            absences = []
            for injury in injury_rows or []:
                player = injury.get("player") or {}
                team = injury.get("team") or {}
                absences.append({
                    "player": player.get("name"),
                    "team": team.get("name"),
                    "type": injury.get("type"),
                    "reason": injury.get("reason"),
                })
            if absences:
                team_news_evidence = {
                    "provider": "API-Football",
                    "fixture_id": fixture_id,
                    "reported_absences": absences,
                }
        except Exception as exc:
            provider_warnings.append(f"Injury feed unavailable: {type(exc).__name__}")
    if not home_team.get("id") or not away_team.get("id"):
        raise FootballStatsError("The stats provider omitted one or both team IDs.")
    home_rows = _recent_team_observations(home_team["id"], target, key, session, last)
    away_rows = _recent_team_observations(away_team["id"], target, key, session, last)

    def tactical_profile(team_rows):
        fields = ("team_possession", "team_shots", "team_shots_on_target",
                  "team_corners", "team_passes", "team_tackles", "team_fouls", "team_offsides")
        profile = {}
        for field in fields:
            values = [float(r[field]) for r in team_rows if r.get(field) is not None]
            if len(values) >= 3:
                profile[field.removeprefix("team_") + "_average"] = round(sum(values) / len(values), 2)
        sample_counts = [len([r for r in team_rows if r.get(field) is not None]) for field in fields]
        profile["matches_sampled"] = max(sample_counts, default=0)
        if "possession_average" in profile and profile["possession_average"] >= 55:
            profile["descriptive_style"] = "higher-possession profile in this sample"
        elif "possession_average" in profile and profile["possession_average"] <= 45:
            profile["descriptive_style"] = "lower-possession profile in this sample"
        else:
            profile["descriptive_style"] = "no clear possession-style signal in this sample"
        profile["provider"] = "API-Football fixture statistics"
        profile["warning"] = "Descriptive recent-stat profile only; not video scouting or proof of tactical intent."
        return profile if profile["matches_sampled"] >= 3 else None

    tactical_evidence = {
        "home": tactical_profile(home_rows),
        "away": tactical_profile(away_rows),
    }

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
    home_xg = [r["xg"] for r in home_rows if r.get("xg") is not None]
    away_xg = [r["xg"] for r in away_rows if r.get("xg") is not None]
    xg_evidence = None
    if len(home_xg) >= 3 and len(away_xg) >= 3:
        xg_evidence = {
            "provider": "API-Football", "home_sample": len(home_xg), "away_sample": len(away_xg),
            "home_xg_average": sum(home_xg) / len(home_xg),
            "away_xg_average": sum(away_xg) / len(away_xg),
            "source_statistic": "expected goals where returned by fixture statistics",
        }
    return {
        "fixture": f"{home} vs {away}", "date": target.isoformat(),
        "fixture_id": fixture_id,
        "provider": "API-Football", "panels": panels,
        "lineup_evidence": lineup_evidence,
        "team_news_evidence": team_news_evidence,
        "xg_evidence": xg_evidence,
        "tactical_evidence": tactical_evidence,
        "provider_warnings": provider_warnings,
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


def specialist_price_assessments(report, bookmaker_games):
    """Match specialist estimates to exact fixture prices and calculate transparent edge."""
    import re
    aliases = {
        "corners": ("corner",),
        "cards": ("yellow card",),
        "shots": ("total shots", "shots"),
        "shots_on_target": ("shots on target", "shots on goal"),
        "half_time_goals": ("1st half", "first half", "half time", "half-time"),
    }
    panels = {p["market"]: p for p in report.get("panels", []) if p.get("available")}
    output = []
    for game in bookmaker_games or []:
        if (_team_key(game.get("home")) != _team_key(report["fixture"].split(" vs ", 1)[0])
                or _team_key(game.get("away")) != _team_key(report["fixture"].split(" vs ", 1)[-1])):
            continue
        for row in game.get("markets") or []:
            name = str(row.get("market") or "").casefold()
            selection = str(row.get("selection") or "").strip()
            match = re.search(r"\b(over|under)\s+(\d+(?:\.\d+)?)", selection, re.I)
            if not match:
                continue
            direction, line = match.group(1).casefold(), float(match.group(2))
            for market_key, panel in panels.items():
                if not any(alias in name for alias in aliases.get(market_key, ())):
                    continue
                if market_key == "shots" and any(x in name for x in ("shots on target", "shots on goal")):
                    continue
                if market_key == "half_time_goals" and not any(x in name for x in ("1st half", "first half", "half time", "half-time")):
                    continue
                probability = (_poisson_over if direction == "over" else _poisson_under)(
                    float(panel["expected_total"]), line
                )
                try:
                    odds = float(row.get("odds"))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(odds) or odds <= 1:
                    continue
                output.append({
                    "bookmaker": game.get("bookmaker") or "Unknown",
                    "market": market_key, "market_name": row.get("market"),
                    "selection": selection, "line": line, "odds": odds,
                    "probability": probability, "implied_probability": 1 / odds,
                    "edge": probability - (1 / odds),
                    "expected_roi": probability * odds - 1,
                })
                break
    return sorted(output, key=lambda x: (x["expected_roi"], x["edge"]), reverse=True)


PLAYER_METRICS = {
    "shots": ("shots", "total"),
    "shots_on_target": ("shots", "on"),
    "goals": ("goals", "total"),
    "assists": ("goals", "assists"),
    "yellow_cards": ("cards", "yellow"),
    "red_cards": ("cards", "red"),
}


def player_prop_forecast(home, away, match_date, player_name, metric, line,
                         api_key=None, session=None, last=5):
    """Estimate a named player's prop from their last appearances; requires an exact player match."""
    import requests
    session = session or requests
    key = api_key or os.getenv("API_FOOTBALL_KEY")
    if not key:
        raise FootballStatsError("Set API_FOOTBALL_KEY privately to enable player props.")
    metric_key = str(metric).strip().casefold().replace(" ", "_")
    if metric_key not in PLAYER_METRICS:
        raise ValueError("Metric must be shots, shots_on_target, goals, assists, yellow_cards or red_cards.")
    target = date.fromisoformat(str(match_date)[:10])
    fixtures = _get("fixtures", {"date": target.isoformat(), "timezone": "Africa/Nairobi"}, key, session)
    exact = []
    for row in fixtures:
        teams = row.get("teams") or {}
        if (_team_key((teams.get("home") or {}).get("name")) == _team_key(home)
                and _team_key((teams.get("away") or {}).get("name")) == _team_key(away)):
            exact.append(row)
    if len(exact) != 1:
        raise FootballStatsError(f"Expected one exact fixture for {home} vs {away} on {target}; found {len(exact)}.")
    fixture = exact[0]
    season = (fixture.get("league") or {}).get("season")
    if not season:
        raise FootballStatsError("The provider omitted the competition season.")
    team_rows = fixture.get("teams") or {}
    player_matches = []
    for side in ("home", "away"):
        team_id = (team_rows.get(side) or {}).get("id")
        if not team_id:
            continue
        roster = _get("players", {
            "team": int(team_id), "season": int(season), "search": str(player_name).strip(),
        }, key, session)
        for row in roster:
            player = row.get("player") or {}
            if _team_key(player.get("name")) == _team_key(player_name):
                player_matches.append((int(player["id"]), int(team_id), player.get("name")))
    unique = {(x[0], x[1], x[2]) for x in player_matches}
    if len(unique) != 1:
        raise FootballStatsError(f"Expected one exact squad match for player '{player_name}'; found {len(unique)}.")
    player_id, team_id, resolved_name = next(iter(unique))
    recent = _get("fixtures", {
        "team": team_id, "last": int(last),
        "to": (target - timedelta(days=1)).isoformat(), "timezone": "Africa/Nairobi",
    }, key, session)
    category, field = PLAYER_METRICS[metric_key]
    observations = []
    for match in recent:
        fixture_id = (match.get("fixture") or {}).get("id")
        if not fixture_id:
            continue
        player_payload = _get("players", {"fixture": int(fixture_id)}, key, session)
        for team_data in player_payload:
            for player_data in team_data.get("players") or []:
                player = player_data.get("player") or {}
                if int(player.get("id") or 0) != player_id:
                    continue
                stats = (player_data.get("statistics") or [{}])[0]
                games = stats.get("games") or {}
                minutes = float(games.get("minutes") or 0)
                if minutes <= 0:
                    continue
                value = ((stats.get(category) or {}).get(field))
                if value is not None:
                    observations.append(float(value))
    if len(observations) < 4:
        raise FootballStatsError(f"Only {len(observations)} recent appearances found for {resolved_name}; at least four are required.")
    mean = sum(observations) / len(observations)
    line_value = float(line)
    return {
        "player": resolved_name, "player_id": player_id, "fixture_id": int((fixture.get("fixture") or {}).get("id")), "metric": metric_key, "line": line_value,
        "appearances": len(observations), "recent_values": observations,
        "expected_per_appearance": mean,
        "over_probability": _poisson_over(mean, line_value),
        "under_probability": _poisson_under(mean, line_value),
        "model": "recent-appearance Poisson baseline",
        "warning": "Experimental, uncalibrated player-prop estimate. Verify the player is expected to start and check provider coverage and bookmaker settlement rules.",
    }
