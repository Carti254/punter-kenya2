"""OpenAI-powered natural-language and screenshot interface for PUNTER KENYA.

The language model interprets requests and reads slip images. It never invents
bookmaker prices or calculates betting probabilities; those remain the job of
PUNTER's deterministic data and prediction code.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

ALLOWED_ACTIONS = {"deepdive", "odds", "fixtures", "topvalue", "autobuild", "help", "explain"}


def ai_is_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _client():
    if not ai_is_configured():
        raise RuntimeError("AI is not configured. Add OPENAI_API_KEY to the hosting environment.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI SDK is not installed. Redeploy after installing requirements.txt.") from exc
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"].strip(), timeout=40.0, max_retries=2)


def _json_from_response(response) -> dict[str, Any]:
    raw = str(getattr(response, "output_text", "") or "").strip()
    if not raw:
        raise RuntimeError("The AI returned an empty response.")
    raw = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", raw, flags=re.I)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The AI response could not be read as structured data.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("The AI response had an unexpected format.")
    return value


def interpret_request(message: str) -> dict[str, Any]:
    """Translate a user's natural-language request into an existing bot workflow."""
    if not message or len(message) > 2000:
        raise ValueError("Message must contain 1–2,000 characters.")
    response = _client().responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
        instructions=(
            "You are the request router for PUNTER KENYA, a Kenyan football research "
            "Telegram bot. Return JSON only with keys action, fixture, date, market, "
            "target_odds, explanation. action must be one of deepdive, odds, fixtures, "
            "topvalue, autobuild, help, explain. Route match research to deepdive; "
            "current bookmaker prices to odds; fixture lists to fixtures; ranked "
            "research/picks to topvalue; a requested slip/accumulator preview to "
            "autobuild; help requests to help; general betting concept questions to "
            "explain. Extract a fixture only when both teams are clear, formatted "
            "'Home vs Away'. date must be today, tomorrow, or both. target_odds must "
            "be a number >1 only if the user specified a target, otherwise 5.0. "
            "market is a concise filter such as all, 1X2, goals, corners, cards, "
            "or a specific market. Do not make predictions, invent odds, promise "
            "profit, or claim to have researched a match. If intent is unclear, use help. "
            "explanation is a short answer only for explain; otherwise empty."
        ),
        input=message,
        text={"format": {"type": "json_object"}},
        max_output_tokens=250,
    )
    data = _json_from_response(response)
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
    return {
        "action": action,
        "fixture": fixture,
        "date": date,
        "market": market,
        "target_odds": target,
        "explanation": str(data.get("explanation") or "")[:1200],
    }


def read_slip_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> list[str]:
    """Read a betting-slip image and return candidate lines for live validation."""
    if not image_bytes or len(image_bytes) > 12 * 1024 * 1024:
        raise ValueError("Image must be between 1 byte and 12 MB.")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        mime_type = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    response = _client().responses.create(
        model=os.getenv("OPENAI_VISION_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini")).strip(),
        instructions=(
            "Read this sportsbook slip image. Extract only clearly legible football "
            "selections. Return JSON only: {\"selections\":[{\"fixture\":"
            "\"Home vs Away\",\"market\":\"market name and line\","
            "\"selection\":\"selected outcome\"}]}. Do not guess missing "
            "teams, markets, lines or selections. Exclude non-football selections. "
            "Do not include odds in the output because PUNTER will fetch and validate "
            "current SportyBet Kenya prices separately."
        ),
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "Extract the clearly visible football slip selections."},
            {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"},
        ]}],
        text={"format": {"type": "json_object"}},
        max_output_tokens=1200,
    )
    data = _json_from_response(response)
    rows = data.get("selections")
    if not isinstance(rows, list):
        raise RuntimeError("No readable selections were returned.")
    lines = []
    for row in rows[:20]:
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
