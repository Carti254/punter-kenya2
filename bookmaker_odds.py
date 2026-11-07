"""Read-only odds adapters for Kenyan bookmakers.

These adapters use public web endpoints observed in the open-source OddsPro
project. The endpoints are undocumented and may change or restrict access.
No credentials, bet placement, or bookmaker account access are used.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
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

BETIKA_BASE = "https://api.betika.com/v1/uo"
BETPAWA_BASE = "https://www.betpawa.co.ke/api/sportsbook/v4"


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


def fetch_betika(day: date | None = None, max_pages=12):
    """Fetch Betika football pre-match markets for one EAT calendar day."""
    day = day or datetime.now(EAT).date()
    today = datetime.now(EAT).date()
    day_offset = (day - today).days
    if day_offset < 0 or day_offset > 7:
        return []
    # Betika uses period_id=-1 for today, then 1..7 for future days.
    period = -1 if day_offset == 0 else day_offset
    headers = {
        "origin": "https://www.betika.com",
        "referer": "https://www.betika.com/",
        "accept-language": "en-GB,en;q=0.9",
    }
    games, page = [], 1
    while page <= max_pages:
        payload = _get(
            BETIKA_BASE + "/matches",
            params={
                "page": page, "limit": 50, "tab": "upcoming",
                "sub_type_id": 1, "sport_id": "14,139", "sort_id": 2,
                "period_id": period, "esports": "false",
            },
            headers=headers,
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, dict):
            rows = rows.get("data")
        if not isinstance(rows, list):
            raise ValueError("Betika returned an unexpected matches response.")
        if not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = _eat_time(row.get("start_time"))
            if start and start[:10] != day.isoformat():
                continue
            match_id = row.get("parent_match_id")
            if not match_id:
                continue
            try:
                detail = _get(
                    BETIKA_BASE + "/match",
                    params={"parent_match_id": match_id},
                    headers=headers,
                )
                meta = detail.get("meta") if isinstance(detail, dict) else {}
                meta = meta if isinstance(meta, dict) else {}
                odds_rows = detail.get("data") if isinstance(detail, dict) else []
                if not isinstance(odds_rows, list):
                    odds_rows = []
                markets = []
                for market in odds_rows:
                    if not isinstance(market, dict):
                        continue
                    for selection in market.get("odds", []) or []:
                        if not isinstance(selection, dict):
                            continue
                        special = (selection.get("parsed_special_bet_value") or {}).get("total")
                        item = _market("Betika", market.get("name"), selection.get("display"),
                                       selection.get("odd_value"), special)
                        if item:
                            markets.append(item)
                games.append({
                    "bookmaker": "Betika", "event_id": str(match_id),
                    "home": meta.get("home_team") or row.get("home_team"),
                    "away": meta.get("away_team") or row.get("away_team"),
                    "competition": meta.get("competition_name") or row.get("competition_name"),
                    "kickoff_eat": start, "markets": markets,
                    "url": f"https://www.betika.com/en-ke/m/{match_id}",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                LOG.warning("Betika event %s detail failed: %s", match_id, exc)
        if len(rows) < 50:
            break
        page += 1
        time.sleep(0.08)
    return games


def fetch_betpawa(day: date | None = None):
    """Fetch BetPawa football pre-match markets for one EAT calendar day."""
    day = day or datetime.now(EAT).date()
    start = datetime.combine(day, dt_time.min, tzinfo=EAT).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), dt_time.min, tzinfo=EAT).astimezone(timezone.utc)
    query = {
        "queries": [{
            "query": {
                "eventType": "UPCOMING", "categories": ["2"], "zones": {},
                "hasOdds": True,
                "startTime": {"gte": start.isoformat(), "lt": end.isoformat()},
            },
            "view": {"marketTypes": ["3743", "28000810", "28000850", "4693", "1096755", "3795"]},
            "skip": 0, "take": 100,
        }]
    }
    headers = {
        "devicetype": "web", "referer": "https://www.betpawa.co.ke/events",
        "x-pawa-brand": "betpawa-kenya", "x-pawa-language": "en",
    }
    payload = _get(
        BETPAWA_BASE + "/events/lists/by-queries",
        params={"q": __import__("json").dumps(query, separators=(",", ":"))},
        headers=headers,
    )
    responses = payload.get("responses") if isinstance(payload, dict) else None
    if not isinstance(responses, list):
        # Some responses wrap the event list in a data object.
        data = payload.get("data") if isinstance(payload, dict) else None
        responses = data.get("responses") if isinstance(data, dict) else None
    if not isinstance(responses, list):
        raise ValueError("BetPawa returned an unexpected event-list response.")
    games = []
    for envelope in responses:
        events = envelope.get("events", []) if isinstance(envelope, dict) else []
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict) or not event.get("id"):
                continue
            try:
                detail = _get(
                    BETPAWA_BASE + "/events/" + str(event["id"]),
                    headers=headers,
                )
                participants = detail.get("participants") or []
                if len(participants) != 2:
                    continue
                markets = []
                for market in detail.get("markets", []) or []:
                    if not isinstance(market, dict):
                        continue
                    market_name = (market.get("marketType") or {}).get("displayName")
                    for row in market.get("row", []) or []:
                        for selection in row.get("prices", []) or []:
                            item = _market("BetPawa", market_name, selection.get("name"),
                                           selection.get("odds"),
                                           selection.get("handicap") or row.get("specifier"))
                            if item:
                                markets.append(item)
                games.append({
                    "bookmaker": "BetPawa", "event_id": str(detail["id"]),
                    "home": participants[0].get("name"),
                    "away": participants[1].get("name"),
                    "competition": (detail.get("competition") or {}).get("name"),
                    "kickoff_eat": _eat_time(detail.get("startTime")),
                    "markets": markets,
                    "url": f"https://www.betpawa.co.ke/event/{detail['id']}?filter=all",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                LOG.warning("BetPawa event %s detail failed: %s", event.get("id"), exc)
    return games


def fetch_all(day: date | None = None):
    """Return per-bookmaker results and isolated errors; never fake empty success."""
    day = day or datetime.now(EAT).date()
    result = {"date": day.isoformat(), "bookmakers": {}, "errors": {}}
    for name, fetcher in (("Betika", fetch_betika), ("BetPawa", fetch_betpawa)):
        try:
            games = fetcher(day)
            result["bookmakers"][name] = {
                "events": len(games),
                "priced_events": sum(bool(g.get("markets")) for g in games),
                "games": games,
            }
        except Exception as exc:
            LOG.exception("%s odds fetch failed", name)
            result["errors"][name] = f"{type(exc).__name__}: {str(exc)[:220]}"
    return result


def canonical_team(value):
    value = str(value or "").casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    ignored = {"fc", "cf", "sc", "afc", "club"}
    return " ".join(part for part in value.split() if part not in ignored)


def find_event(games, home, away):
    """Conservative exact/containment matcher; ambiguous matches are not guessed."""
    h, a = canonical_team(home), canonical_team(away)
    matches = []
    for game in games:
        gh, ga = canonical_team(game.get("home")), canonical_team(game.get("away"))
        direct = (gh == h and ga == a) or (gh in h or h in gh) and (ga in a or a in ga)
        reverse = (gh == a and ga == h) or (gh in a or a in gh) and (ga in h or h in ga)
        if direct or reverse:
            matches.append(game)
    return matches
