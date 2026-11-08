"""Create genuine, non-staking SportyBet Kenya booking codes.

Uses SportyBet's public web endpoints (undocumented and subject to change).
The only write operation is /orders/share; it does not place a wager.
"""
import re
import time
import requests
from difflib import SequenceMatcher

BASE = "https://www.sportybet.com/api/ke"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Current-Country": "KE",
    "User-Agent": "Mozilla/5.0 (compatible; PunterKenya/1.0)",
}
MARKET_IDS = "1,18,10,29,11,26,36,14,60100"
SESSION = requests.Session()


class SportyBetError(RuntimeError):
    pass


def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _get_fixtures():
    fixtures = []
    for page in range(1, 11):
        params = {
            "sportId": "sr:sport:1",
            "marketId": MARKET_IDS,
            "pageSize": 100,
            "pageNum": page,
            "todayGames": "false",
            "timeline": 720,
            "_t": int(time.time() * 1000),
        }
        try:
            response = SESSION.get(
                f"{BASE}/factsCenter/pcUpcomingEvents",
                params=params, headers=HEADERS, timeout=(6, 18),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise SportyBetError(f"Could not reach SportyBet Kenya: {type(exc).__name__}.") from None
        except ValueError:
            raise SportyBetError("SportyBet returned an unreadable fixture response.") from None
        if payload.get("bizCode") not in (None, 10000):
            raise SportyBetError(str(payload.get("message") or "SportyBet rejected the fixture request.")[:250])
        tournaments = (payload.get("data") or {}).get("tournaments") or []
        page_events = [event for tournament in tournaments for event in (tournament.get("events") or [])]
        fixtures.extend(page_events)
        if len(page_events) < 100:
            break
    return fixtures


def _find_fixture(fixtures, fixture_text):
    sides = re.split(r"\s+vs\.?\s+|\s+v\.?\s+", fixture_text.strip(), maxsplit=1, flags=re.I)
    if len(sides) != 2:
        raise SportyBetError(f'Fixture "{fixture_text}" must use HOME vs AWAY.')
    home_q, away_q = map(_norm, sides)
    ranked = []
    for event in fixtures:
        home, away = _norm(event.get("homeTeamName")), _norm(event.get("awayTeamName"))
        score = (SequenceMatcher(None, home_q, home).ratio() + SequenceMatcher(None, away_q, away).ratio()) / 2
        if (home_q in home or home in home_q) and (away_q in away or away in away_q):
            score = max(score, 0.98)
        ranked.append((score, event))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < 0.78:
        raise SportyBetError(f'Could not match "{fixture_text}" to an upcoming SportyBet Kenya fixture.')
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.025:
        raise SportyBetError(f'"{fixture_text}" matched more than one fixture. Include the full team names.')
    event = ranked[0][1]
    if int(event.get("estimateStartTime") or 0) <= int(time.time() * 1000):
        raise SportyBetError(f'{event.get("homeTeamName")} vs {event.get("awayTeamName")} has already started.')
    return event


def _resolve_market(event, market_text, selection_text):
    markets = event.get("markets") or []
    m = market_text.strip()
    line = None
    total_match = re.search(r"(?:over(?:/under)?|under|total|goals?)\s*(\d+(?:\.\d+)?)", m + " " + selection_text, re.I)
    if total_match:
        line = total_match.group(1)
    wanted = _norm(m)
    if wanted in ("1x2", "matchresult", "fulltimeresult", "result"):
        candidates = [x for x in markets if _norm(x.get("desc")) in ("1x2", "matchresult", "fulltimeresult", "result")]
    elif line:
        candidates = [x for x in markets if ("over" in _norm(x.get("desc")) or "total" in _norm(x.get("desc"))) and (line in str(x.get("specifier") or ""))]
    else:
        candidates = [x for x in markets if wanted == _norm(x.get("desc")) or wanted == _norm(x.get("id"))]
    if not candidates:
        available = ", ".join(dict.fromkeys(str(x.get("desc") or x.get("id")) for x in markets[:18]))
        raise SportyBetError(f'Market "{market_text}" is not available for {event.get("homeTeamName")} vs {event.get("awayTeamName")}. Available markets: {available or "none"}.')
    market = candidates[0]
    outcomes = market.get("outcomes") or []
    pick = selection_text.strip()
    pnorm = _norm(pick)
    home, away = event.get("homeTeamName", ""), event.get("awayTeamName", "")
    if _norm(market.get("desc")) in ("1x2", "matchresult", "fulltimeresult", "result"):
        aliases = {
            _norm(home): {"home", "1"},
            _norm(away): {"away", "2"},
        }
        if pnorm in ("1", "home") or pnorm == _norm(home):
            targets = {"home", "1", _norm(home)}
        elif pnorm in ("x", "draw", "tie"):
            targets = {"draw", "x"}
        elif pnorm in ("2", "away") or pnorm == _norm(away):
            targets = {"away", "2", _norm(away)}
        else:
            targets = {pnorm}
        outcome = next((o for o in outcomes if _norm(o.get("desc")) in targets), None)
    elif line and (pnorm.startswith("over") or pnorm.startswith("o") or pnorm.startswith("under") or pnorm.startswith("u")):
        wanted_side = "over" if pnorm.startswith(("over", "o")) else "under"
        outcome = next((o for o in outcomes if _norm(o.get("desc")) in {wanted_side, wanted_side[0]}), None)
    else:
        outcome = next((o for o in outcomes if _norm(o.get("desc")) == pnorm), None)
    if not outcome or int(outcome.get("isActive", 1)) != 1:
        available = ", ".join(str(o.get("desc")) for o in outcomes)
        raise SportyBetError(f'Selection "{selection_text}" is not available for {market.get("desc")}. Available: {available or "none"}.')
    return {
        "eventId": str(event.get("eventId")),
        "marketId": str(market.get("id")),
        "specifier": market.get("specifier"),
        "outcomeId": str(outcome.get("id")),
        "fixture": f'{home} vs {away}',
        "league": "",
        "market": str(market.get("desc") or market_text),
        "selection": str(outcome.get("desc")),
        "odds": float(outcome.get("odds") or 0),
        "startTime": event.get("estimateStartTime"),
    }


def prepare_slip(lines):
    """Resolve lines formatted Fixture | Market | Selection to current live IDs."""
    if not lines or len(lines) > 20:
        raise SportyBetError("A SportyBet slip must contain between 1 and 20 selections.")
    fixtures = _get_fixtures()
    resolved = []
    for number, raw in enumerate(lines, 1):
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) != 3 or not all(parts):
            raise SportyBetError(f"Line {number}: use Fixture | Market | Selection.")
        event = _find_fixture(fixtures, parts[0])
        item = _resolve_market(event, parts[1], parts[2])
        if any(x["eventId"] == item["eventId"] and x["marketId"] == item["marketId"] and x["outcomeId"] == item["outcomeId"] for x in resolved):
            raise SportyBetError(f"Line {number}: duplicate selection.")
        resolved.append(item)
    return resolved


def create_booking(resolved):
    selections = [{
        "eventId": x["eventId"],
        "marketId": x["marketId"],
        "specifier": x.get("specifier"),
        "outcomeId": x["outcomeId"],
    } for x in resolved]
    try:
        response = SESSION.post(
            f"{BASE}/orders/share",
            json={"selections": selections},
            headers=HEADERS,
            timeout=(6, 20),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise SportyBetError(f"SportyBet booking request failed: {type(exc).__name__}.") from None
    except ValueError:
        raise SportyBetError("SportyBet returned an unreadable booking response.") from None
    if payload.get("bizCode") not in (None, 10000):
        raise SportyBetError(str(payload.get("message") or "SportyBet did not accept these selections.")[:250])
    data = payload.get("data") or {}
    code = data.get("shareCode")
    if not code:
        unavailable = data.get("unavailableOutcomes") or []
        detail = f" {len(unavailable)} selection(s) were unavailable." if unavailable else ""
        raise SportyBetError("SportyBet did not return a booking code." + detail)
    return {
        "bookingCode": str(code),
        "shareURL": str(data.get("shareURL") or ""),
        "legs": data.get("outcomes") or [],
        "unavailable": data.get("unavailableOutcomes") or [],
        "deadline": data.get("deadline"),
    }



def get_booking(code):
    """Read a SportyBet share code (read-only) and normalize its legs."""
    clean = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{4,12}", clean):
        raise SportyBetError("Enter a valid SportyBet booking code (4–12 letters/numbers).")
    try:
        response = SESSION.get(
            f"{BASE}/orders/share/{clean}",
            headers=HEADERS,
            timeout=(6, 18),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise SportyBetError(f"Could not read SportyBet booking code: {type(exc).__name__}.") from None
    except ValueError:
        raise SportyBetError("SportyBet returned an unreadable booking-code response.") from None
    if payload.get("bizCode") not in (None, 10000):
        raise SportyBetError(str(payload.get("message") or "SportyBet rejected this booking code.")[:250])
    data = payload.get("data") or {}
    if not data.get("shareCode"):
        raise SportyBetError("Booking code was not found or has expired.")
    selections = []
    for leg in data.get("outcomes") or []:
        markets = leg.get("markets") or []
        market = markets[0] if markets else {}
        outcomes = market.get("outcomes") or []
        outcome = outcomes[0] if outcomes else {}
        event_id = str(leg.get("eventId") or "")
        market_id = str(market.get("id") or "")
        outcome_id = str(outcome.get("id") or "")
        if not (event_id and market_id and outcome_id):
            raise SportyBetError("SportyBet returned a leg without reusable event/market/outcome IDs. This code cannot safely be edited.")
        start_time = leg.get("estimateStartTime")
        if start_time and int(start_time) <= int(time.time() * 1000):
            raise SportyBetError(f"{leg.get('homeTeamName', 'A fixture')} vs {leg.get('awayTeamName', '')} has already started; this selection cannot be rebuilt.")
        selections.append({
            "eventId": event_id,
            "marketId": market_id,
            "specifier": market.get("specifier"),
            "outcomeId": outcome_id,
            "fixture": f"{leg.get('homeTeamName', '')} vs {leg.get('awayTeamName', '')}".strip(" vs"),
            "league": "",
            "market": str(market.get("desc") or market.get("name") or market_id),
            "selection": str(outcome.get("desc") or outcome_id),
            "odds": float(outcome.get("odds") or 0),
            "startTime": start_time,
        })
    if not selections:
        raise SportyBetError("SportyBet returned no readable selections for this code.")
    return {
        "bookingCode": str(data.get("shareCode")),
        "shareURL": str(data.get("shareURL") or ""),
        "selections": selections,
        "deadline": data.get("deadline"),
        "unavailable": data.get("unavailableOutcomes") or [],
    }
