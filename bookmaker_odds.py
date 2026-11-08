"""Read-only odds adapters for Kenyan bookmakers.

These adapters use public web endpoints observed in the open-source OddsPro
project. The endpoints are undocumented and may change or restrict access.
No credentials, bet placement, or bookmaker account access are used.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("punter_kenya.bookmakers")
EAT = ZoneInfo("Africa/Nairobi")

SESSION = requests.Session()
RETRY = Retry(
    total=2, connect=2, read=1, status=2, backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}), respect_retry_after_header=True,
)
SESSION.mount("https://", HTTPAdapter(max_retries=RETRY))
SESSION.headers.update({
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (compatible; PunterKenya/1.0)",
})


def _get(url: str, *, params=None, headers=None, timeout=15):
    response = SESSION.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _eat_time(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value / (1000 if value > 10**12 else 1), timezone.utc)
        else:
            raw = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(EAT).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _number(value):
    try:
        n = float(value)
        return round(n, 3) if n > 1 and n < 10000 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _market(provider, market, selection, price, handicap=None):
    odd = _number(price)
    if not odd:
        return None
    return {
        "bookmaker": provider,
        "market": str(market or "Unknown").strip(),
        "selection": str(selection or "Unknown").strip(),
        "handicap": handicap,
        "odds": odd,
    }


SPORTYBET_BASE = "https://www.sportybet.com/api/ke"


def fetch_sportybet(day: date | None = None, max_pages=30):
    """Fetch all active SportyBet Kenya football markets and outcomes from its public JSON feed."""
    day = day or datetime.now(EAT).date()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Current-Country": "KE",
        "Referer": "https://www.sportybet.com/ke/sport/football",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134.0.0.0 Safari/537.36",
    }
    games, seen = [], set()
    for page in range(1, max_pages + 1):
        payload = _get(
            SPORTYBET_BASE + "/factsCenter/pcUpcomingEvents",
            params={"sportId": "sr:sport:1", "pageSize": 100,
                    "pageNum": page, "option": 2, "_t": int(time.time() * 1000)},
            headers=headers, timeout=(8, 20),
        )
        if not isinstance(payload, dict) or int(payload.get("bizCode", 0) or 0) != 10000:
            code = payload.get("bizCode") if isinstance(payload, dict) else "invalid"
            raise ValueError(f"SportyBet rejected the public feed (bizCode={code}).")
        data = payload.get("data") or {}
        tournaments = data.get("tournaments", []) if isinstance(data, dict) else []
        if not isinstance(tournaments, list):
            raise ValueError("SportyBet returned an unexpected event catalogue.")
        page_events = 0
        for tournament in tournaments:
            if not isinstance(tournament, dict):
                continue
            for event in tournament.get("events", []) or []:
                if not isinstance(event, dict):
                    continue
                event_id = event.get("eventId")
                if not event_id or str(event_id) in seen:
                    continue
                seen.add(str(event_id))
                page_events += 1
                kickoff = _eat_time(event.get("estimateStartTime"))
                if not kickoff or kickoff[:10] != day.isoformat():
                    continue
                markets = []
                for market in event.get("markets", []) or []:
                    if not isinstance(market, dict):
                        continue
                    market_name = market.get("desc") or market.get("name") or market.get("id") or "Unknown market"
                    market_id = str(market.get("id") or "")
                    specifier = market.get("specifier")
                    for outcome in market.get("outcomes", []) or []:
                        if not isinstance(outcome, dict) or int(outcome.get("isActive", 0) or 0) != 1:
                            continue
                        selection = outcome.get("desc") or outcome.get("name") or outcome.get("id")
                        item = _market("SportyBet", market_name, selection, outcome.get("odds"), handicap=specifier)
                        if item:
                            item["market_id"] = market_id
                            item["outcome_id"] = str(outcome.get("id") or "")
                            item["specifier"] = specifier
                            markets.append(item)
                if markets:
                    games.append({
                        "bookmaker": "SportyBet", "event_id": str(event_id),
                        "home": event.get("homeTeamName"), "away": event.get("awayTeamName"),
                        "competition": tournament.get("name") or tournament.get("categoryName"),
                        "kickoff_eat": kickoff, "markets": markets,
                        "url": "https://www.sportybet.com/ke/sport/football",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    })
        total = int(data.get("totalNum", 0) or 0) if isinstance(data, dict) else 0
        if not tournaments or page_events == 0 or (total and len(seen) >= total):
            break
        time.sleep(0.25)
    return games

def fetch_all(day: date | None = None):
    """Fetch only providers with an implemented read-only adapter."""
    day = day or datetime.now(EAT).date()
    result = {"date": day.isoformat(), "bookmakers": {}, "errors": {}}
    for name, fetcher in (("SportyBet", fetch_sportybet),):
        try:
            games = fetcher(day)
            result["bookmakers"][name] = {
                "events": len(games),
                "priced_events": sum(bool(g.get("markets")) for g in games),
                "games": games,
            }
        except Exception as exc:
            LOG.exception("%s odds fetch failed", name)
            result["errors"][name] = f"{type(exc).__name__}: {str(exc).split('?', 1)[0][:180]}"
    result["supported_bookmakers"] = ["SportyBet"]
    return result


def canonical_team(value):
    value = str(value or "").casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    ignored = {"fc", "cf", "sc", "afc", "club"}
    return " ".join(part for part in value.split() if part not in ignored)


def _team_match_score(left, right):
    """Return a conservative similarity score for two team names."""
    a, b = canonical_team(left), canonical_team(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    at, bt = set(a.split()), set(b.split())
    if not at or not bt:
        return 0.0
    overlap = len(at & bt) / len(at | bt)
    # Substring matches are useful for known suffixes, but never for tiny names.
    if min(len(a), len(b)) >= 8 and (a in b or b in a):
        overlap = max(overlap, 0.88)
    return overlap


def find_event(games, home, away):
    """Match only the requested home/away orientation; reject ambiguous matches."""
    ranked = []
    for game in games:
        home_score = _team_match_score(game.get("home"), home)
        away_score = _team_match_score(game.get("away"), away)
        # Both sides must be a strong match. Reversed fixtures are deliberately rejected.
        if home_score >= 0.80 and away_score >= 0.80:
            ranked.append((min(home_score, away_score) + (home_score + away_score) / 2, game))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return []
    # If the top two candidates are effectively tied, do not guess.
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.04:
        return []
    return [ranked[0][1]]
