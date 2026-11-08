"""Create genuine, non-staking SportyBet Kenya booking codes.

Uses SportyBet's public web endpoints (undocumented and subject to change).
The only write operation is /orders/share; it does not place a wager.
"""
import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from difflib import SequenceMatcher

BASE = "https://www.sportybet.com/api/ke"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Current-Country": "KE",
    "User-Agent": "Mozilla/5.0 (compatible; PunterKenya/1.0)",
}
MARKET_IDS = "1,18,10,29,11,26,36,14,60100"
# Current PUNTER booking/share integration validation limit; do not silently exceed it.
MAX_SELECTIONS = 20
SESSION = requests.Session()
# Retry idempotent fixture reads only; a booking POST must never be retried.
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=2, connect=2, read=1, status=2, backoff_factor=0.4,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}), respect_retry_after_header=True,
)))


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
    if len(sides) != 2 or not all(sides):
        raise SportyBetError(f'Fixture "{fixture_text}" must use HOME vs AWAY.')
    home_q, away_q = map(_norm, sides)
    ranked = []
    for event in fixtures:
        home, away = _norm(event.get("homeTeamName")), _norm(event.get("awayTeamName"))
        if not home or not away:
            continue
        home_score = SequenceMatcher(None, home_q, home).ratio()
        away_score = SequenceMatcher(None, away_q, away).ratio()
        # Only tolerate abbreviated suffix variants when both sides independently match.
        if min(len(home_q), len(home)) >= 6 and (home_q in home or home in home_q):
            home_score = max(home_score, 0.90)
        if min(len(away_q), len(away)) >= 6 and (away_q in away or away in away_q):
            away_score = max(away_score, 0.90)
        if home_score >= 0.82 and away_score >= 0.82:
            ranked.append((min(home_score, away_score), (home_score + away_score) / 2, event))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        raise SportyBetError(f'Could not confidently match both teams in "{fixture_text}" to a SportyBet Kenya fixture.')
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.025 and ranked[0][1] - ranked[1][1] < 0.025:
        raise SportyBetError(f'"{fixture_text}" matched more than one fixture. Include the full team names.')
    event = ranked[0][2]
    try:
        start_ms = int(event.get("estimateStartTime") or 0)
    except (TypeError, ValueError):
        start_ms = 0
    if start_ms <= int(time.time() * 1000):
        raise SportyBetError(f'{event.get("homeTeamName")} vs {event.get("awayTeamName")} has already started or has no valid kick-off time.')
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
    if len(candidates) > 1:
        # Prefer exact specifier equality, never the first market returned by the feed.
        wanted_spec = line
        exact = [x for x in candidates if wanted_spec and re.search(r"(?:^|[=,:])" + re.escape(wanted_spec) + r"(?:$|[,:])", str(x.get("specifier") or ""))]
        if len(exact) == 1:
            candidates = exact
        else:
            raise SportyBetError(f'Market "{market_text}" is ambiguous for {event.get("homeTeamName")} vs {event.get("awayTeamName")}; specify its exact line.')
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
        # SportyBet commonly labels outcomes "Over 6.5" / "Under 6.5".
        # Match the full side + line first; matching only "over"/"under"
        # incorrectly rejects valid outcomes carrying the handicap in desc.
        wanted_side = "over" if pnorm.startswith(("over", "o")) else "under"
        wanted_line = re.search(r"\d+(?:\.\d+)?", pick)
        wanted_line = wanted_line.group(0) if wanted_line else line

        def outcome_matches_total(outcome):
            desc = str(outcome.get("desc") or "").strip()
            normalized = _norm(desc)
            if normalized == pnorm:
                return True
            desc_match = re.match(r"^\s*(over|under|o|u)\s*(\d+(?:\.\d+)?)?\s*$", desc, re.I)
            if not desc_match:
                return False
            side = desc_match.group(1).lower()
            side = "over" if side in ("over", "o") else "under"
            desc_line = desc_match.group(2)
            if side != wanted_side:
                return False
            # Some feeds put the line in the market specifier, not outcome desc.
            return desc_line is None or str(float(desc_line)) == str(float(wanted_line))

        outcome = next((o for o in outcomes if outcome_matches_total(o)), None)
    else:
        outcome = next((o for o in outcomes if _norm(o.get("desc")) == pnorm), None)
    if not outcome or int(outcome.get("isActive", 0) or 0) != 1:
        available = ", ".join(str(o.get("desc")) for o in outcomes)
        raise SportyBetError(f'Selection "{selection_text}" is suspended or unavailable for {market.get("desc")}. Available: {available or "none"}.')
    try:
        current_odds = float(outcome.get("odds"))
    except (TypeError, ValueError, OverflowError):
        current_odds = 0.0
    if not (current_odds > 1.0 and current_odds < 10000):
        raise SportyBetError(f'Selection "{selection_text}" has no valid current decimal price.')
    return {
        "eventId": str(event.get("eventId")),
        "marketId": str(market.get("id")),
        "specifier": market.get("specifier"),
        "outcomeId": str(outcome.get("id")),
        "fixture": f'{home} vs {away}',
        "league": "",
        "market": str(market.get("desc") or market_text),
        "selection": str(outcome.get("desc")),
        "odds": current_odds,
        "startTime": event.get("estimateStartTime"),
    }


def prepare_slip(lines):
    """Resolve lines formatted Fixture | Market | Selection to current live IDs."""
    if not lines or len(lines) > MAX_SELECTIONS:
        raise SportyBetError(f"A SportyBet slip must contain between 1 and {MAX_SELECTIONS} selections.")
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


def prepare_slip_from_candidates(candidates):
    """Resolve scanner candidates by their exact bookmaker-native IDs, never fuzzy names."""
    if not candidates or len(candidates) > MAX_SELECTIONS:
        raise SportyBetError("A SportyBet slip must contain between 1 and 20 selections.")
    fixtures = _get_fixtures()
    by_id = {str(event.get("eventId")): event for event in fixtures if event.get("eventId") is not None}
    resolved, seen = [], set()
    now_ms = int(time.time() * 1000)
    for number, candidate in enumerate(candidates, 1):
        event_id = str(candidate.get("event_id") or "")
        market_id = str(candidate.get("market_id") or "")
        outcome_id = str(candidate.get("outcome_id") or "")
        if not event_id or not market_id or not outcome_id:
            raise SportyBetError(f"Leg {number}: scanner did not preserve all bookmaker-native IDs.")
        event = by_id.get(event_id)
        if not event:
            raise SportyBetError(f"Leg {number}: fixture disappeared from the current SportyBet feed.")
        try:
            start_ms = int(event.get("estimateStartTime") or 0)
        except (TypeError, ValueError):
            start_ms = 0
        if start_ms <= now_ms:
            raise SportyBetError(f"Leg {number}: fixture has started or has no valid kick-off time.")
        markets = [
            market for market in (event.get("markets") or [])
            if str(market.get("id")) == market_id
            and (market.get("specifier") or None) == (candidate.get("specifier") or None)
        ]
        if len(markets) != 1:
            raise SportyBetError(f"Leg {number}: exact market ID/specifier is missing or ambiguous.")
        outcomes = [
            outcome for outcome in (markets[0].get("outcomes") or [])
            if str(outcome.get("id")) == outcome_id
        ]
        if len(outcomes) != 1 or int(outcomes[0].get("isActive", 0) or 0) != 1:
            raise SportyBetError(f"Leg {number}: exact outcome is suspended or unavailable.")
        try:
            live_odds = float(outcomes[0].get("odds"))
            preview_odds = float(candidate.get("odds"))
        except (TypeError, ValueError, OverflowError):
            raise SportyBetError(f"Leg {number}: invalid preview or live odds.") from None
        if not (1.0 < live_odds < 10000 and 1.0 < preview_odds < 10000):
            raise SportyBetError(f"Leg {number}: invalid decimal odds.")
        if abs(live_odds - preview_odds) > 0.001:
            raise SportyBetError(
                f"Leg {number}: odds changed from {preview_odds:.3f} to {live_odds:.3f}; "
                "the slip preview must be rebuilt."
            )
        key = (event_id, market_id, outcome_id)
        if key in seen:
            raise SportyBetError(f"Leg {number}: duplicate native selection.")
        seen.add(key)
        resolved.append({
            "eventId": event_id, "marketId": market_id,
            "specifier": markets[0].get("specifier"),
            "outcomeId": outcome_id,
            "fixture": f'{event.get("homeTeamName")} vs {event.get("awayTeamName")}',
            "league": "", "market": str(markets[0].get("desc") or candidate.get("market") or ""),
            "selection": str(outcomes[0].get("desc") or candidate.get("selection") or ""),
            "odds": live_odds, "startTime": start_ms,
        })
    return resolved


def _revalidate_selections(resolved):
    """Re-read the live Kenyan feed and verify every native ID before sharing."""
    if not resolved:
        raise SportyBetError("Cannot create an empty booking code.")
    fixtures = _get_fixtures()
    by_id = {str(event.get("eventId")): event for event in fixtures if event.get("eventId") is not None}
    refreshed = []
    for index, item in enumerate(resolved, 1):
        event = by_id.get(str(item.get("eventId")))
        if not event:
            raise SportyBetError(f"Leg {index}: fixture is no longer available on SportyBet.")
        try:
            start_ms = int(event.get("estimateStartTime") or 0)
        except (TypeError, ValueError):
            start_ms = 0
        if start_ms <= int(time.time() * 1000):
            raise SportyBetError(f"Leg {index}: fixture has started or its kick-off time is invalid.")
        markets = [
            m for m in (event.get("markets") or [])
            if str(m.get("id")) == str(item.get("marketId"))
            and (m.get("specifier") or None) == (item.get("specifier") or None)
        ]
        if len(markets) != 1:
            raise SportyBetError(f"Leg {index}: original market is missing or ambiguous; rebuild the slip.")
        outcomes = [
            o for o in (markets[0].get("outcomes") or [])
            if str(o.get("id")) == str(item.get("outcomeId"))
        ]
        if len(outcomes) != 1 or int(outcomes[0].get("isActive", 0) or 0) != 1:
            raise SportyBetError(f"Leg {index}: selection is suspended or no longer available; rebuild the slip.")
        try:
            price = float(outcomes[0].get("odds"))
        except (TypeError, ValueError, OverflowError):
            price = 0.0
        if not (1.0 < price < 10000):
            raise SportyBetError(f"Leg {index}: bookmaker returned an invalid price.")
        try:
            preview_price = float(item.get("odds"))
        except (TypeError, ValueError, OverflowError):
            preview_price = 0.0
        if not (preview_price > 1.0 and preview_price < 10000):
            raise SportyBetError(f"Leg {index}: preview price is missing or invalid; rebuild the slip.")
        if abs(price - preview_price) > 0.001:
            raise SportyBetError(
                f"Leg {index}: odds changed from {preview_price:.3f} to {price:.3f}. "
                "No booking code was requested; rebuild and review the updated slip."
            )
        refreshed.append({**item, "odds": price, "startTime": start_ms})
    return refreshed


def create_booking(resolved):
    """Revalidate every native selection, then request a share code only."""
    current = _revalidate_selections(resolved)
    selections = [{
        "eventId": x["eventId"],
        "marketId": x["marketId"],
        "specifier": x.get("specifier"),
        "outcomeId": x["outcomeId"],
    } for x in current]
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
    if not isinstance(payload, dict) or payload.get("bizCode") != 10000:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise SportyBetError(str(message or "SportyBet did not confirm the booking request.")[:250])
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise SportyBetError("SportyBet returned an unexpected booking response.")
    code = data.get("shareCode")
    unavailable = data.get("unavailableOutcomes") or []
    accepted = data.get("outcomes") or []
    if unavailable:
        raise SportyBetError(f"SportyBet rejected {len(unavailable)} selection(s) during booking validation. Nothing has been accepted as a complete slip; rebuild and review.")
    if not code or len(accepted) != len(current):
        raise SportyBetError("SportyBet did not confirm every requested selection; no complete booking code will be shown.")
    return {
        "bookingCode": str(code),
        "shareURL": str(data.get("shareURL") or ""),
        "legs": accepted,
        "unavailable": unavailable,
        "deadline": data.get("deadline"),
        "revalidatedSelections": current,
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
