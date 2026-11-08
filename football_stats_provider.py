"""Optional API-Sports / API-Football match-statistics adapter.

Requires API_FOOTBALL_KEY. It never guesses fixture identity: the date and both
normalised team names must identify exactly one provider fixture.
"""
import os
import re
import unicodedata

import requests

BASE_URL = "https://v3.football.api-sports.io"


class FootballStatsError(RuntimeError):
    pass


def _team_key(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", value.casefold())
    aliases = {"man utd": "manchester united", "manchester utd": "manchester united",
               "psg": "paris saint germain"}
    phrase = " ".join(words)
    phrase = aliases.get(phrase, phrase)
    return " ".join(x for x in phrase.split() if x not in {"fc", "cf", "sc", "afc", "football", "club"})


def _get(endpoint, params, api_key, session=requests):
    response = session.get(
        f"{BASE_URL}/{endpoint.lstrip('/')}",
        params=params,
        headers={"x-apisports-key": api_key, "Accept": "application/json"},
        timeout=(5, 25),
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        raise FootballStatsError(f"API-Football {endpoint} returned invalid JSON.") from None
    if not isinstance(payload, dict):
        raise FootballStatsError(f"API-Football {endpoint} returned an unexpected response.")
    errors = payload.get("errors")
    if errors:
        raise FootballStatsError(f"API-Football {endpoint} error: {str(errors)[:350]}")
    rows = payload.get("response")
    if not isinstance(rows, list):
        raise FootballStatsError(f"API-Football {endpoint} response array is missing.")
    return rows


def fetch_match_market_data(home, away, match_date, api_key=None, session=requests):
    """Fetch fixture, full/half statistics and player figures for one exact match."""
    key = api_key or os.getenv("API_FOOTBALL_KEY")
    if not key:
        raise FootballStatsError("Set API_FOOTBALL_KEY in the deployment environment.")
    date_text = str(match_date)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        raise ValueError("match_date must be YYYY-MM-DD.")
    fixtures = _get("fixtures", {"date": date_text, "timezone": "UTC"}, key, session)
    home_key, away_key = _team_key(home), _team_key(away)
    matches = []
    for row in fixtures:
        teams = row.get("teams") or {}
        h = _team_key((teams.get("home") or {}).get("name"))
        a = _team_key((teams.get("away") or {}).get("name"))
        fixture = row.get("fixture") or {}
        if h == home_key and a == away_key and fixture.get("id") is not None:
            matches.append(row)
    if len(matches) != 1:
        raise FootballStatsError(
            f"Expected one exact API-Football fixture for {home} vs {away} on {date_text}; found {len(matches)}."
        )
    fixture_id = int(matches[0]["fixture"]["id"])
    full_stats = _get("fixtures/statistics", {"fixture": fixture_id}, key, session)
    half_stats = _get("fixtures/statistics", {"fixture": fixture_id, "half": "true"}, key, session)
    players = _get("players", {"fixture": fixture_id}, key, session)
    events = _get("fixtures/events", {"fixture": fixture_id}, key, session)
    return {
        "provider": "api-football",
        "fixture_id": fixture_id,
        "fixture": matches[0],
        "statistics_full_time": full_stats,
        "statistics_half_time": half_stats,
        "players": players,
        "events": events,
        "fetched_match": {"home": home, "away": away, "date": date_text},
    }
