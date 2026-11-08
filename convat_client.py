"""Convat booking-code integration for PUNTER KENYA.

Uses Convat's documented server-side API. Never logs API credentials or invents
booking codes. Evaluation keys use eval-api.convat.io; production keys use
api.convat.io. Configure CONVAT_API_KEY and optionally CONVAT_API_BASE_URL.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

import requests

DEFAULT_BASE_URL = "https://eval-api.convat.io/v1"
REQUEST_TIMEOUT = (8, 65)
MAX_RETRIES = 3


class ConvatError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload or {}


def _settings():
    key = (os.getenv("CONVAT_API_KEY") or "").strip()
    base = (os.getenv("CONVAT_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    if not key:
        raise ConvatError(
            "Booking-code conversion is not activated yet. Add CONVAT_API_KEY "
            "to the bot's environment after receiving an evaluation key from Convat."
        )
    if not base.startswith("https://"):
        raise ConvatError("CONVAT_API_BASE_URL must use HTTPS.")
    return key, base


def _request(path: str, body: dict[str, Any], *, idempotency_key: str | None = None):
    key, base = _settings()
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                f"{base}/{path.lstrip('/')}",
                json=body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            # A timeout can occur after a target code was created. Only a
            # conversion with the same idempotency key may safely be retried.
            if not idempotency_key or attempt == MAX_RETRIES - 1:
                raise ConvatError(
                    "Convat could not be reached. If this was a conversion, "
                    "the request may still be processing; retry the same action safely."
                ) from exc
            time.sleep(min(2 ** attempt, 8))
            continue

        try:
            payload = response.json()
        except ValueError:
            payload = {"message": "Convat returned a non-JSON response."}

        if response.ok:
            if not isinstance(payload, dict):
                raise ConvatError("Convat returned an unexpected response format.")
            return payload

        error_code = payload.get("error") if isinstance(payload, dict) else None
        message = payload.get("message") if isinstance(payload, dict) else None
        message = str(message or f"Convat request failed (HTTP {response.status_code}).")

        retryable = response.status_code in (429, 502, 503, 409)
        if retryable and idempotency_key and attempt < MAX_RETRIES - 1:
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = max(1, min(int(retry_after), 15))
            except (TypeError, ValueError):
                delay = min(2 ** attempt, 8)
            time.sleep(delay)
            continue

        raise ConvatError(
            message, status=response.status_code, code=error_code,
            payload=payload if isinstance(payload, dict) else {},
        )

    raise ConvatError("Convat request failed after safe retries.") from last_error


def preview_betika_code(code: str) -> dict:
    clean = "".join(str(code or "").split())
    if not 3 <= len(clean) <= 40 or not clean.isalnum():
        raise ConvatError("That does not look like a valid booking code. Check it and try again.")
    return _request("preview", {"source": "betika", "code": clean})


def convert_betika_to_sportybet(code: str, *, exact_lines: bool = True,
                                idempotency_key: str | None = None) -> dict:
    clean = "".join(str(code or "").split())
    if not 3 <= len(clean) <= 40 or not clean.isalnum():
        raise ConvatError("That does not look like a valid booking code. Check it and try again.")
    return _request(
        "convert",
        {
            "source": "betika",
            "target": "sportybet_ke",
            "code": clean,
            "exactLines": bool(exact_lines),
        },
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )
