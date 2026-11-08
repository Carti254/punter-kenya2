"""Free external football-data adapters for PUNTER KENYA.

Providers:
- BSD (Bzzoiro Sports Data): free football tier, 7,500 requests/day.
- PitchAPI: free football REST API; check current provider terms and coverage.

These adapters enrich an already-matched SportyBet fixture. They never replace
SportyBet's executable price and never treat predicted lineups as confirmed.
External model probabilities are cross-checks; they do not overwrite PUNTER's
own probabilities except for exact supported corner totals where PUNTER has no
specialist estimate. All network calls are bounded and fail open.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache

import requests

LOG = logging.getLogger("punter_kenya.free_sources")
BSD_BASE = "https://sports.bzzoiro.com/api/v2"
PITCH_BASE = "https://api.pitchapi.dev/v1"
TIMEOUT = (4, 12)


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", value.casefold())
    ignored = {"fc", "cf", "sc", "afc", "club", "football"}
    return " ".join(x for x in words if x not in ignored)


def _same_team(a: str, b: str) -> bool:
    aa, bb = _normal(a), _normal(b)
    if not aa or not bb:
        return False
    if aa == bb or aa in bb or bb in aa:
        return True
    return min(len(aa), len(bb)) >= 6 and SequenceMatcher(None, aa, bb).ratio() >= 0.90


def _unwrap(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _rows(payload):
    data = _unwrap(payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "matches", "events", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _request(url, headers, params=None):
    response = requests.get(url, headers=headers, params=params or {}, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error)[:180])
    return payload


def _utc_day(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _event_names(row, provider):
    if provider == "bsd":
        home = row.get("home_team") or row.get("home") or {}
        away = row.get("away_team") or row.get("away") or {}
        if isinstance(home, dict):
            home = home.get("name") or home.get("team_name")
        if isinstance(away, dict):
            away = away.get("name") or away.get("team_name")
        return str(home or ""), str(away or "")
    home = row.get("home_team") or {}
    away = row.get("away_team") or {}
    if isinstance(home, dict):
        home = home.get("name")
    if isinstance(away, dict):
        away = away.get("name")
    return str(home or ""), str(away or "")


def _event_time(row, provider):
    return row.get("event_date") or row.get("kickoff_at") or row.get("time_utc") or row.get("date") or row.get("start_time")


def _match_event(rows, home, away, target_day, provider):
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        h, a = _event_names(row, provider)
        if not (_same_team(home, h) and _same_team(away, a)):
            continue
        event_day = _utc_day(_event_time(row, provider))
        # The EAT fixture date can differ from UTC by one day.
        if event_day and abs((event_day - target_day).days) > 1:
            continue
        matches.append(row)
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=32)
def _bsd_events_for_window(start_day: str, end_day: str, token: str):
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    payload = _request(
        f"{BSD_BASE}/events/",
        headers,
        {"date_from": start_day, "date_to": end_day, "status": "upcoming", "limit": 200, "offset": 0},
    )
    return tuple(_rows(payload))


@lru_cache(maxsize=48)
def _pitch_date(day_text: str, token: str):
    headers = {"X-API-KEY": token, "Accept": "application/json"}
    payload = _request(f"{PITCH_BASE}/date/{day_text}", headers, {"status": "upcoming"})
    return tuple(_rows(payload))


def _bsd_detail(event_id, suffix, token):
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    return _unwrap(_request(f"{BSD_BASE}/events/{event_id}/{suffix.strip('/')}/", headers))


def _pitch_detail(match_id, suffix, token):
    headers = {"X-API-KEY": token, "Accept": "application/json"}
    return _unwrap(_request(f"{PITCH_BASE}/matches/{match_id}/{suffix.strip('/')}", headers))


def _line_probability(prediction, market, selection):
    """Return an exact BSD forecast probability for a quoted corners line."""
    if not isinstance(prediction, dict):
        return None
    markets = prediction.get("markets") or {}
    market_text = _normal(f"{market} {selection}")
    if "corner" not in market_text:
        return None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", str(selection))
    if not match:
        return None
    line = float(match.group(1))
    suffix = str(line).replace(".", "")
    corners = markets.get("corners") or {}
    over = corners.get(f"prob_over_{suffix}")
    if over is None:
        return None
    try:
        p = float(over) / 100.0
    except (TypeError, ValueError):
        return None
    if not 0 < p < 1:
        return None
    text = _normal(selection)
    if "under" in text:
        return 1.0 - p
    if "over" in text:
        return p
    return None


def _external_probability(prediction, market, selection):
    """Map only explicitly published BSD forecast markets to exact selections."""
    if not isinstance(prediction, dict):
        return None
    markets = prediction.get("markets") or {}
    market_text = _normal(f"{market} {selection}")
    selection_text = _normal(selection)
    result = markets.get("match_result") or {}
    values = {}
    for key in ("prob_home", "prob_draw", "prob_away"):
        try:
            values[key] = float(result.get(key)) / 100.0
        except (TypeError, ValueError):
            values[key] = None
    home, draw, away = values["prob_home"], values["prob_draw"], values["prob_away"]
    is_result = any(term in market_text for term in ("1x2", "match result", "full time result", "fulltime result"))
    if is_result and all(value is not None for value in (home, draw, away)):
        if selection_text in {"1", "home", "home win"}: return home
        if selection_text in {"x", "draw", "tie"}: return draw
        if selection_text in {"2", "away", "away win"}: return away
    if "double chance" in market_text and all(value is not None for value in (home, draw, away)):
        if selection_text in {"1x", "home or draw", "home draw"}: return home + draw
        if selection_text in {"12", "home or away", "home away"}: return home + away
        if selection_text in {"x2", "draw or away", "draw away"}: return draw + away
    if ("draw no bet" in market_text or "dnb" in market_text) and home is not None and away is not None:
        denominator = home + away
        if denominator > 0:
            if selection_text in {"1", "home", "home win"}: return home / denominator
            if selection_text in {"2", "away", "away win"}: return away / denominator
    if any(term in market_text for term in ("total goals", "over under", "goals over", "goals under")):
        line_match = re.search(r"(1[.]5|2[.]5|3[.]5)", str(selection))
        if line_match:
            suffix = line_match.group(1).replace(".", "")
            totals = markets.get("over_under") or {}
            try:
                over = float(totals.get(f"prob_over_{suffix}")) / 100.0
                if 0 < over < 1:
                    if "under" in selection_text: return 1.0 - over
                    if "over" in selection_text: return over
            except (TypeError, ValueError):
                pass
    return _line_probability(prediction, market, selection)


def _extract_prediction_1x2(prediction):
    markets = (prediction or {}).get("markets") or {}
    result = markets.get("match_result") or {}
    values = {}
    for key in ("prob_home", "prob_draw", "prob_away"):
        try:
            value = float(result.get(key)) / 100.0
            if 0 < value < 1:
                values[key] = value
        except (TypeError, ValueError):
            pass
    return values or None


def enrich_candidates(rows: list[dict], max_fixtures: int = 30):
    """Enrich unique SportyBet candidates with BSD/PitchAPI evidence.

    Returns (rows, summary). Provider outages, missing keys and ambiguous event
    matches do not stop the main SportyBet scan.
    """
    output = [dict(row) for row in (rows or [])]
    bsd_key = os.getenv("BSD_API_KEY") or os.getenv("BZZOIRO_API_KEY")
    pitch_key = os.getenv("PITCHAPI_API_KEY") or os.getenv("PITCH_KEY")
    summary = {
        "bsd_enabled": bool(bsd_key),
        "pitchapi_enabled": bool(pitch_key),
        "fixtures_enriched": 0,
        "bsd_matches": 0,
        "pitchapi_matches": 0,
        "corner_probabilities_added": 0,\n        "external_probabilities_added": 0,
        "errors": [],
    }
    if not output or not (bsd_key or pitch_key):
        summary["reason"] = "No free-provider API keys configured; base PUNTER research remains active."
        return output, summary

    grouped = {}
    for row in output:
        home, away = str(row.get("home") or ""), str(row.get("away") or "")
        raw_day = str(row.get("day") or row.get("match_date") or "")[:10]
        try:
            target_day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        if home and away:
            grouped.setdefault((home, away, target_day), []).append(row)

    for index, ((home, away, target_day), fixture_rows) in enumerate(grouped.items()):
        if index >= max_fixtures:
            summary["errors"].append(f"Enrichment capped at {max_fixtures} matched fixtures to protect free API quotas.")
            break
        evidence = {}
        bsd_event = None
        pitch_event = None

        if bsd_key:
            try:
                start = (target_day - timedelta(days=1)).isoformat()
                end = (target_day + timedelta(days=1)).isoformat()
                event_rows = _bsd_events_for_window(start, end, bsd_key)
                bsd_event = _match_event(event_rows, home, away, target_day, "bsd")
                if bsd_event:
                    event_id = bsd_event.get("id") or bsd_event.get("event_id")
                    evidence["bsd"] = {"event_id": event_id, "status": bsd_event.get("status")}
                    if event_id:
                        for suffix, key in (("prediction", "prediction"), ("stats", "stats"),
                                            ("lineups", "lineups"), ("h2h", "h2h")):
                            try:
                                evidence["bsd"][key] = _bsd_detail(event_id, suffix, bsd_key)
                            except Exception as exc:
                                LOG.debug("BSD %s unavailable for %s vs %s: %s", suffix, home, away, exc)
                        summary["bsd_matches"] += 1
                else:
                    evidence["bsd"] = {"matched": False, "reason": "No unique exact fixture match."}
            except Exception as exc:
                summary["errors"].append(f"BSD {home} vs {away}: {type(exc).__name__}: {str(exc)[:120]}")

        if pitch_key:
            try:
                pitch_rows = []
                for offset in (-1, 0, 1):
                    query_day = (target_day + timedelta(days=offset)).isoformat()
                    pitch_rows.extend(_pitch_date(query_day, pitch_key))
                pitch_event = _match_event(pitch_rows, home, away, target_day, "pitch")
                if pitch_event:
                    match_id = pitch_event.get("id")
                    evidence["pitchapi"] = {"match_id": match_id, "status": pitch_event.get("status")}
                    if match_id:
                        for suffix, key in (("stats", "stats"), ("advanced", "advanced"),
                                            ("lineups", "lineups"), ("h2h", "h2h"),
                                            ("events", "events"), ("players", "players")):
                            try:
                                evidence["pitchapi"][key] = _pitch_detail(match_id, suffix, pitch_key)
                            except Exception as exc:
                                LOG.debug("PitchAPI %s unavailable for %s vs %s: %s", suffix, home, away, exc)
                        summary["pitchapi_matches"] += 1
                else:
                    evidence["pitchapi"] = {"matched": False, "reason": "No unique exact fixture match."}
            except Exception as exc:
                summary["errors"].append(f"PitchAPI {home} vs {away}: {type(exc).__name__}: {str(exc)[:120]}")

        if evidence:
            summary["fixtures_enriched"] += 1
        prediction = (evidence.get("bsd") or {}).get("prediction") or {}
        for row in fixture_rows:
            row["external_research"] = evidence
            row["external_evidence_sources"] = [name for name in ("bsd", "pitchapi") if name in evidence]
            p1x2 = _extract_prediction_1x2(prediction)
            if p1x2:
                row["external_1x2_probabilities"] = p1x2
            external_p = _external_probability(prediction, row.get("market"), row.get("selection"))
            if external_p is not None:
                row["external_market_probability"] = external_p
                own_p = row.get("probability")
                if own_p is not None:
                    row["external_model_disagreement"] = abs(float(own_p) - external_p)
                else:
                    row["probability"] = external_p
                    row["probability_source"] = "BSD free football model — exact market/selection mapping"
                    row["ev"] = external_p * float(row["odds"]) - 1.0
                    row["fair_odds"] = 1.0 / external_p
                    market_text = _normal(f"{row.get('market')} {row.get('selection')}")
                    mapped_market = (
                        "total_corners" if "corner" in market_text else
                        "total_goals" if any(x in market_text for x in ("goal", "over under")) else
                        "1x2_or_double_chance"
                    )
                    row["specialist_evidence"] = {
                        "provider": "BSD",
                        "market": mapped_market,
                        "probability": external_p,
                        "warning": "External provider estimate; validate calibration separately.",
                    }
                    summary["external_probabilities_added"] = summary.get("external_probabilities_added", 0) + 1
                    if mapped_market == "total_corners":
                        summary["corner_probabilities_added"] += 1

    return output, summary


def provider_key_status():
    return {
        "BSD_API_KEY": bool(os.getenv("BSD_API_KEY") or os.getenv("BZZOIRO_API_KEY")),
        "PITCHAPI_API_KEY": bool(os.getenv("PITCHAPI_API_KEY") or os.getenv("PITCH_KEY")),
        "API_FOOTBALL_KEY": bool(os.getenv("API_FOOTBALL_KEY")),
    }
