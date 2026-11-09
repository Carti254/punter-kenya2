"""Gemini-powered natural-language and screenshot interface for PUNTER KENYA.

Gemini interprets requests and reads slip images. It never invents bookmaker
prices or calculates betting probabilities; those remain the job of PUNTER's
deterministic data and prediction code.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import requests

ALLOWED_ACTIONS = {"deepdive", "odds", "fixtures", "topvalue", "autobuild", "help", "explain"}
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def ai_is_configured() -> bool:
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def _generate(parts: list[dict[str, Any]], max_tokens: int = 1200) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("AI is not configured. Add GEMINI_API_KEY to the hosting environment.")
    # Gemini 2.5 Flash is unavailable to some new API projects. Keep old
    # Railway environment values from silently pinning the bot to that model.
    configured_model = os.getenv("GEMINI_MODEL", "").strip()
    retired_model_aliases = {
        "gemini-2.5-flash": "gemini-3.8-flash",
        "models/gemini-2.5-flash": "gemini-3.8-flash",
    }
    model = retired_model_aliases.get(configured_model, configured_model) or "gemini-3.8-flash"
    url = GEMINI_API_URL.format(model=model.removeprefix("models/"))
    payload = {
        "system_instruction": {"parts": [{"text":
            "You are the request router and slip-image reader for PUNTER KENYA, a Kenyan football research Telegram bot. "
            "Never invent fixtures, bookmaker prices, statistics or probabilities. Numeric probabilities and odds must "
            "come from PUNTER's own data and deterministic models. Be conservative and return only the requested JSON."
        }]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(
            url, headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload, timeout=45,
        )
        if response.status_code == 429:
            raise RuntimeError("Gemini's free request quota is temporarily exhausted. Please try again later.")
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"Gemini API request failed: {detail or str(exc)[:240]}") from exc
    candidates = body.get("candidates") or []
    try:
        return str(candidates[0]["content"]["parts"][0]["text"]).strip()
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Gemini returned no readable response. Check model availability and API quota.") from exc


def _json_from_response(raw: str) -> dict[str, Any]:
    raw = re.sub(r"^\x60\x60\x60(?:json)?\s*|\s*\x60\x60\x60$", "", raw.strip(), flags=re.I)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini's response could not be read as structured data.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Gemini returned an unexpected response format.")
    return value


def _plain_fixture_request(message: str) -> dict[str, Any] | None:
    """Route only a bare 'Home vs Away' message without depending on Gemini."""
    # Requests with an instruction prefix (e.g. "Analyse Arsenal vs Chelsea")
    # need intent extraction; do not mistake the verb for part of the home team.
    if re.match(
        r"^(?:analyse|analyze|show|check|predict|research|deep\s+dive|"
        r"tell\s+me\s+about|odds\s+for|find|compare)\b",
        message.strip(),
        flags=re.IGNORECASE,
    ):
        return None
    match = re.fullmatch(r"\s*(.+?)\s+(?:vs?\.?|v)\s+(.+?)\s*", message, flags=re.IGNORECASE)
    if not match:
        return None
    home = re.sub(r"\s+", " ", match.group(1)).strip(" .,-")
    away = re.sub(r"\s+", " ", match.group(2)).strip(" .,-")
    if not home or not away or len(home) > 90 or len(away) > 90:
        return None
    return {
        "action": "deepdive",
        "fixture": f"{home} vs {away}",
        "date": "today",
        "market": "all",
        "target_odds": 5.0,
        "explanation": "",
    }


def interpret_request(message: str) -> dict[str, Any]:
    """Translate a user's request; route clear fixture queries locally first."""
    if not message or len(message) > 2000:
        raise ValueError("Message must contain 1–2,000 characters.")
    direct_fixture = _plain_fixture_request(message)
    if direct_fixture is not None:
        return direct_fixture
    prompt = (
        "Translate this user request into JSON with keys action, fixture, date, market, target_odds, explanation. "
        "action must be one of deepdive, odds, fixtures, topvalue, autobuild, help, explain. "
        "Route match research to deepdive; current bookmaker prices to odds; fixture lists to fixtures; "
        "ranked research/picks to topvalue; requested slip/accumulator preview to autobuild; help to help; "
        "general betting concept questions to explain. Extract fixture only when both teams are clear, formatted "
        "'Home vs Away'. date must be today, tomorrow, or both. target_odds must be a number >1 only if specified, "
        "otherwise 5.0. market is a concise filter such as all, 1X2, goals, corners, cards, or a specific market. "
        "Do not make predictions, invent odds, promise profit, or claim research was done. If unclear, use help. "
        "explanation is a short answer only for explain; otherwise empty.\n\nUSER REQUEST:\n" + message
    )
    data = _json_from_response(_generate([{"text": prompt}], max_tokens=350))
    action = str(data.get("action", "help")).strip().lower()
    if action not in ALLOWED_ACTIONS:
        action = "help"
    fixture = str(data.get("fixture") or "").strip()[:180]
    if " vs " not in fixture.casefold():
        fixture = ""
    date = str(data.get("date") or "both").strip().lower()
    if date not in {"today", "tomorrow", "both"}:
        date = "both"
    market = str(data.get("market") or "all").strip()[:80]
    try:
        target = float(data.get("target_odds", 5.0))
        if not 1.01 <= target <= 1000:
            target = 5.0
    except (TypeError, ValueError, OverflowError):
        target = 5.0
    return {"action": action, "fixture": fixture, "date": date, "market": market,
            "target_odds": target, "explanation": str(data.get("explanation") or "")[:1200]}


def read_slip_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> list[str]:
    """Read a betting-slip image and return candidate lines for live validation."""
    if not image_bytes or len(image_bytes) > 12 * 1024 * 1024:
        raise ValueError("Image must be between 1 byte and 12 MB.")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        mime_type = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "Read this sportsbook slip image. Extract only clearly legible football selections. "
        "Return JSON: {\"selections\":[{\"fixture\":\"Home vs Away\","
        "\"market\":\"market name and line\",\"selection\":\"selected outcome\"}]}. "
        "Do not guess missing teams, markets, lines or selections. Exclude non-football selections. "
        "Do not include odds; PUNTER will fetch and validate current SportyBet Kenya prices separately."
    )
    raw = _generate([
        {"text": prompt},
        {"inline_data": {"mime_type": mime_type, "data": encoded}},
    ], max_tokens=1600)
    data = _json_from_response(raw)
    rows = data.get("selections")
    if not isinstance(rows, list):
        raise RuntimeError("Gemini did not return a readable selections list.")
    lines = []
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        fixture = re.sub(r"[|\n\r]", " ", str(row.get("fixture") or "")).strip()
        market = re.sub(r"[|\n\r]", " ", str(row.get("market") or "")).strip()
        selection = re.sub(r"[|\n\r]", " ", str(row.get("selection") or "")).strip()
        if fixture and " vs " in fixture.casefold() and market and selection:
            lines.append(f"{fixture} | {market} | {selection}")
    if not lines:
        raise RuntimeError("No complete, clearly readable football selections were found.")
    return lines


def review_fixture_evidence(fixtures: list[dict], max_fixtures: int = 12) -> dict:
    """Gemini qualitative review of supplied, source-labelled evidence only.

    This is an interpretation layer, not a live-data source. It cannot add injuries,
    lineups, xG, odds movement or tactical facts that are absent from the payload.
    """
    if not ai_is_configured():
        return {"enabled": False, "reason": "GEMINI_API_KEY is not configured."}
    safe = []
    for row in (fixtures or [])[:max(1, min(int(max_fixtures), 12))]:
        evidence = row.get("research_evidence") or {}
        safe.append({
            "fixture": str(row.get("fixture") or f"{row.get('home','')} vs {row.get('away','')}")[:180],
            "date": str(row.get("day") or row.get("match_date") or "")[:10],
            "market": str(row.get("market") or "")[:100],
            "selection": str(row.get("selection") or "")[:100],
            "offered_odds": row.get("odds"),
            "model_probability": row.get("probability"),
            "implied_probability": row.get("implied_probability"),
            "expected_value": row.get("ev"),
            "probability_source": row.get("probability_source"),
            "model_confidence": row.get("model_confidence"),
            "recent_form": {
                "home": evidence.get("home_form"), "away": evidence.get("away_form"),
                "home_sample": evidence.get("home_sample"), "away_sample": evidence.get("away_sample"),
                "home_scored_avg": evidence.get("home_scored_avg"),
                "home_conceded_avg": evidence.get("home_conceded_avg"),
                "away_scored_avg": evidence.get("away_scored_avg"),
                "away_conceded_avg": evidence.get("away_conceded_avg"),
            },
            "venue_samples": [evidence.get("home_home_sample"), evidence.get("away_away_sample")],
            "h2h_count": evidence.get("h2h_count"),
            "specialist_evidence": evidence.get("specialist_evidence"),
            "lineup_evidence": row.get("lineup_evidence"),
            "team_news_evidence": row.get("team_news_evidence"),
            "xg_evidence": row.get("xg_evidence"),
            "odds_history_evidence": row.get("odds_history_evidence"),
            "other_bookmaker_prices": row.get("other_bookmaker_prices"),
            "tactical_evidence": row.get("tactical_evidence"),
        })
    if not safe:
        return {"enabled": True, "reviewed": 0, "reviews": []}
    prompt = (
        "You are PUNTER KENYA's evidence auditor. Review only the JSON evidence supplied. "
        "Do not browse, infer current team news, invent statistics, invent tactical facts, or alter numeric probabilities. "
        "For each fixture return JSON object {reviews:[{fixture, assessment, risk_flags, evidence_used, evidence_missing, "
        "confidence:'LOW'|'MEDIUM'|'HIGH'}]}. evidence_used must name only fields with actual non-null values. "
        "evidence_missing must name absent material data (lineups, injuries, xG, odds history, tactical evidence, "
        "H2H, venue splits, cross-bookmaker prices as applicable). Be conservative. An empty injury feed is NOT proof "
        "of no injuries. A public social post is NOT confirmed team news. Statistical estimates are not guarantees. "
        "Do not recommend increasing stake or claim a bet is safe. Input JSON:\\n"
        + json.dumps(safe, ensure_ascii=False, default=str)
    )
    try:
        data = _json_from_response(_generate([{"text": prompt}], max_tokens=2200))
        reviews = data.get("reviews")
        if not isinstance(reviews, list):
            raise RuntimeError("Gemini evidence review omitted the reviews array.")
        by_fixture = {}
        allowed = {x["fixture"] for x in safe}
        for item in reviews:
            if not isinstance(item, dict) or str(item.get("fixture") or "") not in allowed:
                continue
            by_fixture[str(item["fixture"])] = {
                "assessment": str(item.get("assessment") or "")[:500],
                "risk_flags": [str(x)[:160] for x in (item.get("risk_flags") or [])[:5]],
                "evidence_used": [str(x)[:120] for x in (item.get("evidence_used") or [])[:8]],
                "evidence_missing": [str(x)[:120] for x in (item.get("evidence_missing") or [])[:8]],
                "confidence": str(item.get("confidence") or "LOW").upper()
                    if str(item.get("confidence") or "LOW").upper() in {"LOW", "MEDIUM", "HIGH"} else "LOW",
            }
        return {"enabled": True, "reviewed": len(by_fixture), "reviews": by_fixture}
    except Exception as exc:
        return {"enabled": True, "reviewed": 0, "reviews": {},
                "reason": f"Gemini evidence review failed: {type(exc).__name__}"}
