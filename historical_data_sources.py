"""Competition-scoped supplementary historical results from Football-Data.co.uk.

This source is public CSV data. It is used only when a fixture's primary
Open-Matchday competition history cannot fit the model. Results are never
combined across competitions. Coverage is limited to the divisions listed
below; unsupported competitions remain unmodelled.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime
from functools import lru_cache

import requests

LOG = logging.getLogger("punter_kenya.history_sources")
BASE = "https://www.football-data.co.uk/mmz4281"

# Explicit division mapping. A match must identify one of these competitions;
# no nearest-league or cross-country fallback is permitted.
COMPETITIONS = {
    "england": {
        "premier league": "E0", "english premier league": "E0",
        "championship": "E1", "efl championship": "E1",
        "league one": "E2", "efl league one": "E2",
        "league two": "E3", "efl league two": "E3",
        "national league": "EC", "conference": "EC",
    },
    "scotland": {
        "premiership": "SC0", "scottish premiership": "SC0",
        "championship": "SC1", "league one": "SC2", "league two": "SC3",
    },
    "germany": {"bundesliga": "D1", "2 bundesliga": "D2", "2. bundesliga": "D2"},
    "italy": {"serie a": "I1", "serie b": "I2"},
    "spain": {"la liga": "SP1", "primera division": "SP1", "segunda division": "SP2", "la liga 2": "SP2"},
    "france": {"ligue 1": "F1", "ligue 2": "F2"},
    "netherlands": {"eredivisie": "N1"},
    "belgium": {"first division a": "B1", "jupiler pro league": "B1", "jupiler league": "B1"},
    "portugal": {"liga portugal": "P1", "primeira liga": "P1", "liga portugal 1": "P1"},
    "turkey": {"super lig": "T1", "super league": "T1"},
    "greece": {"super league": "G1", "super league greece": "G1"},
}

def _key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

def _country_key(value):
    raw = _key(value)
    aliases = {
        "eng": "england", "uk": "england", "united kingdom": "england",
        "sco": "scotland", "deu": "germany", "ger": "germany",
        "ita": "italy", "esp": "spain", "fra": "france",
        "nld": "netherlands", "ned": "netherlands", "bel": "belgium",
        "prt": "portugal", "por": "portugal", "tur": "turkey",
        "grc": "greece",
    }
    return aliases.get(raw, raw)

def _split_competition_country(competition, country):
    country_name = _country_key(country)
    league_name = _key(competition)
    # SportyBet frequently embeds the country in the competition label rather
    # than supplying a separate country field, e.g. "Spain - La Liga".
    if not country_name:
        prefixes = {
            "england": ("england", "english"),
            "scotland": ("scotland", "scottish"),
            "germany": ("germany", "german", "deutschland"),
            "italy": ("italy", "italian"),
            "spain": ("spain", "spanish", "espana"),
            "france": ("france", "french"),
            "netherlands": ("netherlands", "dutch", "holland"),
            "belgium": ("belgium", "belgian"),
            "portugal": ("portugal", "portuguese"),
            "turkey": ("turkey", "turkish"),
            "greece": ("greece", "greek"),
        }
        for country_key, names in prefixes.items():
            for name in names:
                match = re.match(r"^" + re.escape(name) + r"\s*(?:[-:|>]\s*)", league_name)
                if match:
                    country_name = country_key
                    league_name = league_name[match.end():].strip()
                    break
            if country_name:
                break
    return country_name, league_name

def competition_code(competition, country):
    country_name, league_name = _split_competition_country(competition, country)
    divisions = COMPETITIONS.get(country_name, {})
    # Exact alias matching only: avoid mapping a cup/youth/reserve event to a
    # similarly named senior league.
    return divisions.get(league_name)

def _season_codes(as_of_date, count=6):
    if isinstance(as_of_date, datetime):
        day = as_of_date.date()
    elif isinstance(as_of_date, date):
        day = as_of_date
    else:
        try:
            day = date.fromisoformat(str(as_of_date)[:10])
        except (TypeError, ValueError):
            day = date.today()
    start_year = day.year if day.month >= 7 else day.year - 1
    return [f"{(start_year-i)%100:02d}{(start_year-i+1)%100:02d}" for i in range(count)]

def _parse_day(value):
    raw = str(value or "").strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            if parsed.year < 1970:
                parsed = parsed.replace(year=parsed.year + 100)
            return parsed
        except ValueError:
            pass
    return None

@lru_cache(maxsize=160)
def _download_season(season_code, division_code):
    url = f"{BASE}/{season_code}/{division_code}.csv"
    response = requests.get(url, headers={"User-Agent": "PunterKenya/1.0"},
                            timeout=(5, 18))
    if response.status_code == 404:
        return ()
    response.raise_for_status()
    text = response.content.decode("cp1252", errors="replace")
    output = []
    for row in csv.DictReader(io.StringIO(text)):
        day = _parse_day(row.get("Date") or row.get("date"))
        home = str(row.get("HomeTeam") or row.get("hometeam") or "").strip()
        away = str(row.get("AwayTeam") or row.get("awayteam") or "").strip()
        try:
            hg = int(float(row.get("FTHG") or row.get("fthg")))
            ag = int(float(row.get("FTAG") or row.get("ftag")))
        except (TypeError, ValueError, OverflowError):
            continue
        if day and home and away and 0 <= hg <= 15 and 0 <= ag <= 15:
            output.append({"date": day.isoformat(), "home": home, "away": away,
                           "ft_home": hg, "ft_away": ag,
                           "_history_source": "football-data.co.uk",
                           "_history_competition_code": division_code})
    return tuple(output)

def fetch_competition_history(competition, country, as_of_date=None, seasons=6):
    """Fetch historical results for one explicitly mapped competition only."""
    code = competition_code(competition, country)
    if not code:
        # Include the exact feed labels in scan diagnostics. This makes missing
        # coverage actionable instead of collapsing every unknown league into
        # the same generic message.
        return [], {"source": "football-data.co.uk", "mapped": False,
                    "reason": (
                        "Unmapped competition/country: "
                        + str(competition or "<blank>")[:90]
                        + " / " + str(country or "<blank>")[:60]
                    )}
    rows, errors = [], []
    cutoff = str(as_of_date or date.today())[:10]
    for season in _season_codes(as_of_date, max(1, min(int(seasons), 8))):
        try:
            rows.extend(_download_season(season, code))
        except requests.RequestException as exc:
            errors.append(f"{season}: {type(exc).__name__}")
    deduped = {}
    for row in rows:
        if row["date"] >= cutoff:
            continue
        key = (row["date"], _key(row["home"]), _key(row["away"]))
        deduped[key] = row
    result = sorted(deduped.values(), key=lambda item: item["date"])
    return result, {"source": "football-data.co.uk", "mapped": True,
                    "competition_code": code, "seasons_checked": _season_codes(as_of_date, max(1, min(int(seasons), 8))),
                    "valid_results": len(result), "errors": errors}
