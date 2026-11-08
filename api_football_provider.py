"""Optional API-Football evidence adapter for market-specific bet settlement.

Requires API_FOOTBALL_KEY. It never treats missing coverage as zero and only
returns evidence after an exact date + home/away team match.
"""
import os
import re
from datetime import date, datetime
from functools import lru_cache

import requests


BASE_URL = "https://v3.football.api-sports.io"
TIMEOUT = (5, 20)


class ProviderError(RuntimeError):
    pass


def _team_key(value):
    value = str(value or "").casefold()
    value = re.sub(r"\\b(fc|cf|sc|afc|club)\\b", " ", value)
    return re.sub(r"[^a-z0-9]", "", value)


class ApiFootballClient:
    def __init__(self, api_key=None, session=None):
        self.api_key = api_key or os.getenv("API_FOOTBALL_KEY")
        if not self.api_key:
            raise ProviderError("API_FOOTBALL_KEY is not configured.")
        self.session = session or requests.Session()
        self.session.headers.update({"x-apisports-key": self.api_key})

    def _get(self, endpoint, params):
        response = self.session.get(
            f"{BASE_URL}/{endpoint.lstrip('/')}",
            params=params,
            timeout=TIMEOUT,
        )
        if response.status_code == 204:
            return []
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors")
        if errors:
            raise ProviderError(f"API-Football rejected {endpoint}: {str(errors)[:250]}")
        return payload.get("response") or []

    def find_fixture(self, match_date, home_team, away_team):
        """Resolve one fixture by UTC date and exact normalized home/away names."""
        if isinstance(match_date, (date, datetime)):
            date_text = match_date.strftime("%Y-%m-%d")
        else:
            date_text = datetime.strptime(str(match_date)[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        rows = self._get("fixtures", {"date": date_text, "timezone": "UTC"})
        home_key, away_key = _team_key(home_team), _team_key(away_team)
        matches = [
            row for row in rows
            if _team_key((row.get("teams") or {}).get("home", {}).get("name")) == home_key
            and _team_key((row.get("teams") or {}).get("away", {}).get("name")) == away_key
        ]
        if len(matches) != 1:
            raise ProviderError(
                f"Expected one exact API-Football fixture for {home_team} vs {away_team} "
                f"on {date_text}; found {len(matches)}. No settlement evidence returned."
            )
        return matches[0]

    def match_evidence(self, match_date, home_team, away_team):
        """Fetch FT/HT, team stats, events and player stats for one resolved fixture.

        Coverage is explicit. Missing/null statistics remain None and must not
        be interpreted as zero. Half-time team statistics are supported only
        where the provider has data (documented from 2024 season).
        """
        fixture = self.find_fixture(match_date, home_team, away_team)
        fixture_id = (fixture.get("fixture") or {}).get("id")
        if not fixture_id:
            raise ProviderError("Resolved fixture did not include a provider fixture ID.")

        goals = fixture.get("goals") or {}
        score = fixture.get("score") or {}
        halftime = score.get("halftime") or {}
        status = (fixture.get("fixture") or {}).get("status") or {}
        stats = self._get("fixtures/statistics", {"fixture": fixture_id})
        halftime_stats = self._get(
            "fixtures/statistics", {"fixture": fixture_id, "half": "true"}
        )
        events = self._get("fixtures/events", {"fixture": fixture_id})
        players = self._get("players", {"fixture": fixture_id})

        return {
            "provider": "api-football",
            "provider_fixture_id": fixture_id,
            "status": status.get("short"),
            "finished": status.get("short") in {"FT", "AET", "PEN"},
            "full_time": {"home": goals.get("home"), "away": goals.get("away")},
            "half_time": {"home": halftime.get("home"), "away": halftime.get("away")},
            "team_statistics": stats,
            "half_time_statistics": halftime_stats,
            "events": events,
            "player_statistics": players,
            "coverage": {
                "team_statistics": bool(stats),
                "half_time_statistics": bool(halftime_stats),
                "events": bool(events),
                "player_statistics": bool(players),
            },
        }
