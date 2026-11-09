"""Read-only odds adapters for Kenyan bookmakers.

These adapters use public web endpoints observed in the open-source OddsPro
project. The endpoints are undocumented and may change or restrict access.
No credentials, bet placement, or bookmaker account access are used.
"""
from __future__ import annotations

import logging
import re
import time
import math
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
    try:
        response.raise_for_status()
    except requests.HTTPError:
        # Preserve the provider's diagnostic response; a bare status code
        # makes undocumented API changes almost impossible to troubleshoot.
        body = (response.text or "")[:700]
        safe_url = response.url.split("?", 1)[0] if response.url else url
        raise RuntimeError(
            f"SportyBet HTTP {response.status_code} at {safe_url}: {body}"
        ) from None
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
        return round(n, 3) if math.isfinite(n) and n > 1 else None
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


def _sportybet_parse_markets(markets):
    parsed = []
    for market in markets or []:
        if not isinstance(market, dict):
            continue
        if int(market.get("status", 0) or 0) != 0 or market.get("banned") is True:
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
                parsed.append(item)
    return parsed


def fetch_sportybet(day: date | None = None, max_pages=60):
    """Fetch target-date SportyBet events, then retrieve each event's full market catalogue."""
    if day is None:
        target_days = {datetime.now(EAT).date().isoformat()}
    elif isinstance(day, (list, tuple, set, frozenset)):
        target_days = {value.isoformat() if hasattr(value, "isoformat") else str(value) for value in day}
    else:
        target_days = {day.isoformat() if hasattr(day, "isoformat") else str(day)}

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Current-Country": "KE",
        "Referer": "https://www.sportybet.com/ke/sport/football",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134.0.0.0 Safari/537.36",
    }
    events_by_id = {}
    seen = set()
    index_failures = []
    pages_fetched = 0
    reported_total = 0

    # The public index can be served by replicas with different snapshots.
    # Walk it more than once and union event IDs; stop only when a complete pass
    # adds nothing. Two consecutive empty/failed pages end one pass.
    for pass_number in range(1, 4):
        before_pass = len(seen)
        empty_pages = 0
        for page in range(1, max_pages + 1):
            try:
                payload = _get(
                    SPORTYBET_BASE + "/factsCenter/pcUpcomingEvents",
                    params={
                        "sportId": "sr:sport:1",
                        "marketId": "1",
                        # marketId=1 is a lightweight event index. option=2 is
                        # the complete fixture list; option=1 is highlights-only.
                        # Full markets are fetched from factsCenter/event below.
                        "pageSize": 100,
                        "pageNum": page,
                        "option": "2",
                        "todayGames": "false",
                        "_t": int(time.time() * 1000),
                    },
                    headers=headers,
                    timeout=(8, 20),
                )
                pages_fetched += 1
                if not isinstance(payload, dict) or int(payload.get("bizCode", 0) or 0) != 10000:
                    code = payload.get("bizCode") if isinstance(payload, dict) else "invalid"
                    raise ValueError(f"SportyBet rejected the public feed (bizCode={code}).")
                data = payload.get("data") or {}
                tournaments = data.get("tournaments", []) if isinstance(data, dict) else []
                if not isinstance(tournaments, list):
                    raise ValueError("SportyBet returned an unexpected event catalogue.")
                total = int(data.get("totalNum", 0) or 0) if isinstance(data, dict) else 0
                reported_total = max(reported_total, total)
            except Exception as exc:
                index_failures.append(f"pass {pass_number}, page {page}: {type(exc).__name__}: {str(exc)[:160]}")
                LOG.warning("SportyBet event-index page failed (%s/%s): %s", pass_number, page, exc)
                empty_pages += 1
                if empty_pages >= 2:
                    break
                continue

            page_events = 0
            for tournament in tournaments:
                if not isinstance(tournament, dict):
                    continue
                for event in tournament.get("events", []) or []:
                    if not isinstance(event, dict):
                        continue
                    event_id = str(event.get("eventId") or "").strip()
                    if not event_id or event_id in seen:
                        continue
                    seen.add(event_id)
                    page_events += 1
                    kickoff = _eat_time(event.get("estimateStartTime"))
                    if not kickoff or kickoff[:10] not in target_days:
                        continue
                    try:
                        kickoff_dt = datetime.strptime(kickoff, "%Y-%m-%d %H:%M").replace(tzinfo=EAT)
                    except ValueError:
                        continue
                    if kickoff_dt <= datetime.now(EAT):
                        continue
                    events_by_id[event_id] = {
                        "event": event,
                        "event_id": event_id,
                        "home": event.get("homeTeamName"),
                        "away": event.get("awayTeamName"),
                        "competition": tournament.get("name") or tournament.get("categoryName"),
                        "kickoff_eat": kickoff,
                        "index_markets": event.get("markets") or [],
                    }

            if not tournaments or page_events == 0:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                continue
            empty_pages = 0
            time.sleep(0.15)

        if len(seen) == before_pass:
            break

    if not events_by_id and index_failures:
        raise RuntimeError("SportyBet fixture index failed: " + " | ".join(index_failures[:3]))
    LOG.info(
        "SportyBet index: %s unique events, %s target-date events, %s pages, %s passes, reported total %s, %s page failures.",
        len(seen), len(events_by_id), pages_fetched, pass_number, reported_total, len(index_failures),
    )

    def price_one(record):
        event_id = record["event_id"]
        detail_error = None
        markets = []
        market_source = "event_detail"
        try:
            detail_payload = _get(
                SPORTYBET_BASE + "/factsCenter/event",
                params={
                    "eventId": event_id,
                    "productId": "3",
                    "_t": int(time.time() * 1000),
                },
                headers=headers,
                timeout=(8, 20),
            )
            if not isinstance(detail_payload, dict) or int(detail_payload.get("bizCode", 0) or 0) != 10000:
                code = detail_payload.get("bizCode") if isinstance(detail_payload, dict) else "invalid"
                raise ValueError(f"SportyBet event detail rejected (bizCode={code}).")
            detail_data = detail_payload.get("data") or {}
            if isinstance(detail_data, list):
                detail_data = detail_data[0] if detail_data and isinstance(detail_data[0], dict) else {}
            if isinstance(detail_data, dict) and isinstance(detail_data.get("event"), dict):
                detail_data = detail_data["event"]
            detail_markets = detail_data.get("markets") if isinstance(detail_data, dict) else None
            if isinstance(detail_markets, list) and detail_markets:
                markets = _sportybet_parse_markets(detail_markets)
            else:
                raise ValueError("SportyBet event detail contained no market catalogue.")
        except Exception as exc:
            detail_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            market_source = "index_fallback"
            LOG.warning("SportyBet full market detail failed for %s: %s", event_id, detail_error)

        if not markets:
            market_source = "index_fallback"
            if detail_error is None:
                detail_error = "Event detail returned no active, priced outcomes."
            markets = _sportybet_parse_markets(record.get("index_markets"))

        if not markets:
            return None

        return {
            "bookmaker": "SportyBet",
            "event_id": event_id,
            "home": record.get("home"),
            "away": record.get("away"),
            "competition": record.get("competition"),
            "kickoff_eat": record.get("kickoff_eat"),
            "markets": markets,
            "market_source": market_source,
            "market_count": len(markets),
            "detail_error": detail_error,
            "url": "https://www.sportybet.com/ke/sport/football",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    # Fetch full markets concurrently, but keep concurrency bounded because
    # SportyBet's public web API is undocumented and has no published rate limit.
    if not events_by_id:
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        priced = list(executor.map(price_one, events_by_id.values()))
    return [game for game in priced if game]


BETIKA_BASE = "https://api.betika.com/v1/uo"


def _betika_items(payload):
    """Extract a real list page; malformed/partial envelopes are errors, not empty days."""
    if not isinstance(payload, dict):
        raise ValueError("Betika returned a non-object list response.")
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "matches", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    for key in ("matches", "items", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise ValueError("Betika list response did not contain a recognised matches array.")


def _betika_detail(match_id):
    # Match the browser headers used by Betika's own web client. The list
    # endpoint was returning data, but detail calls without Origin/Referer
    # were being served an incomplete envelope (no data.meta/data arrays).
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "DNT": "1",
        "Origin": "https://www.betika.com",
        "Pragma": "no-cache",
        "Referer": "https://www.betika.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    }
    payload = _get(
        BETIKA_BASE + "/match",
        params={"parent_match_id": match_id},
        headers=headers,
        timeout=(8, 20),
    )
    if not isinstance(payload, dict):
        raise ValueError("Betika match detail response was not an object.")
    # Betika /match returns {meta: ..., data: [...]} at the JSON root.
    # The JavaScript client destructures Axios response.data first; that
    # outer wrapper is not present in this Python response body.
    if isinstance(payload.get("meta"), dict) and isinstance(payload.get("data"), list):
        return payload["meta"], payload["data"]
    envelope = payload.get("data")
    if isinstance(envelope, dict):
        meta = envelope.get("meta")
        odds_groups = envelope.get("data")
        if isinstance(meta, dict) and isinstance(odds_groups, list):
            return meta, odds_groups
    raise ValueError(
        "Betika match detail response has unexpected shape "
        f"(top-level keys={sorted(str(k) for k in payload.keys())[:12]}, "
        f"data_type={type(envelope).__name__})."
    )


def _parse_betika_game(base, detail):
    meta, odds_groups = detail
    home = str(meta.get("home_team") or base.get("home_team") or "").strip()
    away = str(meta.get("away_team") or base.get("away_team") or "").strip()
    if not home or not away:
        raise ValueError("Betika detail omitted a home or away team.")
    kickoff = _eat_time(meta.get("start_time") or base.get("start_time"))
    if not kickoff:
        raise ValueError("Betika detail omitted a valid kick-off time.")
    markets = []
    for group in odds_groups:
        if not isinstance(group, dict):
            continue
        market_name = group.get("name") or group.get("sub_type_name") or group.get("sub_type_id") or "Unknown market"
        market_id = str(group.get("sub_type_id") or group.get("id") or "")
        outcomes = group.get("odds")
        if not isinstance(outcomes, list):
            continue
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            item = _market("Betika", market_name, outcome.get("display"), outcome.get("odd_value"),
                           handicap=(outcome.get("parsed_special_bet_value") or {}).get("total"))
            if not item:
                continue
            item["market_id"] = market_id
            item["outcome_id"] = str(outcome.get("id") or outcome.get("odd_id") or outcome.get("display") or "")
            item["specifier"] = (outcome.get("parsed_special_bet_value") or {}).get("total")
            markets.append(item)
    return {
        "bookmaker": "Betika",
        "event_id": str(base.get("parent_match_id")),
        "home": home, "away": away,
        "competition": meta.get("competition_name") or base.get("competition_name") or base.get("category"),
        "kickoff_eat": kickoff, "markets": markets,
        "url": "https://www.betika.com/en-ke/sportsbook",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_betika(day: date | None = None, max_pages=30, max_details=None):
    """Read Betika Kenya's public upcoming football list and per-match odds."""
    day = day or datetime.now(EAT).date()
    offset = (day - datetime.now(EAT).date()).days
    if offset < 0 or offset > 7:
        raise ValueError("Betika date lookup supports today through seven days ahead.")
    period_id = -1 if offset == 0 else offset
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.betika.com",
        "Referer": "https://www.betika.com/en-ke/sportsbook",
    }
    matches, seen = [], set()
    for page in range(1, max_pages + 1):
        payload = _get(
            BETIKA_BASE + "/matches",
            params={
                "page": page, "limit": 50, "tab": "upcoming",
                "sub_type_id": 1, "sport_id": "14,139",
                "sort_id": 2, "period_id": period_id, "esports": "false",
            },
            headers=headers, timeout=(8, 20),
        )
        page_items = _betika_items(payload)
        for item in page_items:
            if not isinstance(item, dict):
                continue
            match_id = item.get("parent_match_id")
            if match_id is None or str(match_id) in seen:
                continue
            competition_text = " ".join(str(item.get(key) or "") for key in ("category", "competition_name", "league_name", "name"))
            if re.search(r"-zoom\b|\bsrl\b|virtual|e[- ]?soccer", competition_text, re.I):
                continue
            seen.add(str(match_id))
            kickoff = _eat_time(item.get("start_time"))
            if not kickoff or kickoff[:10] != day.isoformat():
                continue
            matches.append(item)
        if len(page_items) < 50:
            break
        time.sleep(0.08)

    if max_details is not None:
        matches = matches[:max(0, int(max_details))]

    def load_one(item):
        detail = _betika_detail(item["parent_match_id"])
        return _parse_betika_game(item, detail)

    games = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(load_one, item) for item in matches]
        for future in as_completed(futures):
            try:
                game = future.result()
                if game["markets"]:
                    games.append(game)
            except Exception as exc:
                LOG.warning("Betika detail skipped after provider error: %s", str(exc)[:180])
    return sorted(games, key=lambda game: game.get("kickoff_eat") or "")

def smoke_test_bookmakers():
    """Small read-only live smoke check: one feed page and at most three Betika details."""
    day = datetime.now(EAT).date()
    output = {}
    for name, fetcher in (
        ("Betika", lambda: fetch_betika(day, max_pages=1, max_details=3)),
        ("Betika tomorrow", lambda: fetch_betika(day + timedelta(days=1), max_pages=1, max_details=3)),
        # SportyBet's fixture index is paginated; page 1 is not a complete
        # catalogue. Check today and tomorrow across the normal index window.
        ("SportyBet", lambda: fetch_sportybet([day, day + timedelta(days=1)], max_pages=60)),
    ):
        try:
            games = fetcher()
            output[name] = {
                "request_ok": True,
                "events": len(games),
                "priced_events": sum(bool(game.get("markets")) for game in games),
                "event_detail_events": sum(game.get("market_source") == "event_detail" for game in games),
                "index_fallback_events": sum(game.get("market_source") == "index_fallback" for game in games),
                "market_counts": [int(game.get("market_count") or 0) for game in games[:10]],
                "sample": [f'{g.get("home")} vs {g.get("away")}' for g in games[:3]],
            }
        except Exception as exc:
            output[name] = {
                "request_ok": False,
                "error": f"{type(exc).__name__}: {str(exc).split('?', 1)[0][:220]}",
            }
    return output


def fetch_all(day: date | None = None):
    """Fetch only providers with an implemented read-only adapter."""
    day = day or datetime.now(EAT).date()
    result = {"date": day.isoformat(), "bookmakers": {}, "errors": {}}
    for name, fetcher in (("Betika", fetch_betika), ("SportyBet", fetch_sportybet)):
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
    result["supported_bookmakers"] = ["Betika", "SportyBet"]
    return result


def canonical_team(value):
    value = str(value or "").casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    ignored = {"fc", "cf", "sc", "afc", "club"}
    return " ".join(part for part in value.split() if part not in ignored)


def _team_match_score(left, right):
    """Score common provider naming variations without matching on one shared token."""
    from difflib import SequenceMatcher

    a, b = canonical_team(left), canonical_team(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    at, bt = set(a.split()), set(b.split())
    if not at or not bt:
        return 0.0
    union_score = len(at & bt) / len(at | bt)
    # A provider may omit a city/club suffix or use a shortened official name.
    containment_score = len(at & bt) / min(len(at), len(bt))
    sequence_score = SequenceMatcher(None, a, b).ratio()
    score = max(union_score, containment_score * 0.94, sequence_score * 0.90)
    # Tiny one-word names are too ambiguous for fuzzy matching.
    if min(len(a), len(b)) < 5 and a != b:
        return 0.0
    return min(score, 1.0)


def find_event(games, home, away):
    """Match home/away orientation with fuzzy provider-name tolerance and ambiguity rejection."""
    ranked = []
    for game in games:
        home_score = _team_match_score(game.get("home"), home)
        away_score = _team_match_score(game.get("away"), away)
        # Require meaningful similarity on BOTH teams; never accept reversed orientation.
        if home_score >= 0.70 and away_score >= 0.70:
            combined = (home_score + away_score) / 2
            ranked.append((combined, min(home_score, away_score), game))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        return []
    # Demand a clear winner; ties remain unmatched rather than risking a wrong fixture.
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.035:
        return []
    return [ranked[0][2]]
