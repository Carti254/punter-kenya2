
import os
import logging
import asyncio
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
import unicodedata
import re
import math
from difflib import SequenceMatcher
import threading
import json
import time
import copy
from collections import Counter
import uuid
from sportybet_client import SportyBetError, prepare_slip as prepare_sportybet_slip, prepare_slip_from_candidates as prepare_sportybet_candidates, create_booking as create_sportybet_booking, get_booking as get_sportybet_booking
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

from database import get_connection, test_connection
from bookmaker_odds import fetch_all as fetch_bookmaker_odds, fetch_sportybet as fetch_sportybet_odds, find_event as find_bookmaker_event
from slip_manager import create_slip, get_slip, list_slips, update_slip
from schema_migrations import run_migrations
from results_tracker import record_bet, settle_bet, bet_statistics, list_pending_bets, utc_match_date
from prediction_validation import brier_score, log_loss, fit_temperature, apply_temperature
from football_stats_provider import fetch_match_market_data, FootballStatsError
from market_settlement import settle_bet_with_market_evidence
from ai_assistant import ai_is_configured, interpret_request, read_slip_image, review_fixture_evidence, _plain_fixture_request
from premium_intelligence import market_probabilities, assess_price, rollover_projection
from user_portfolio import save_bankroll, get_settings, save_rollover, save_preferences, calculate_user_stake
from premium_tracking import save_price_observations, capture_current_prices, closing_line_value, rollover_status, record_rollover_day, enrich_candidates_with_price_history
from accumulator_engine import build_accumulator
from advanced_models import build_elo_ratings, monte_carlo_scoreline
from autobuild_research import prepare_candidate_pool, candidate_research_summary, apply_walk_forward_calibration, enrich_with_specialist_models as enrich_specialist_candidates
from free_football_sources import enrich_candidates as enrich_free_football_sources, fetch_provider_fixtures as fetch_free_provider_fixtures, provider_key_status
from forecast_tracker import record_forecast, settle_forecast, forecast_performance, refresh_specialist_forecasts

# ============================================================
# PUNTER KENYA — TELEGRAM DATA BOT
# Stable foundation: API diagnostics, league discovery,
# fixture retrieval, database storage and utility commands.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
# GOAL API is an optional supplementary results/statistics source.
# Keep credentials and the provider base URL in environment variables.
GOAL_API_KEY = os.getenv("GOAL_API_KEY")
GOAL_API_BASE = (os.getenv("GOAL_API_BASE") or "").rstrip("/")
OPENMATCHDAY_RAW_BASE = "https://raw.githubusercontent.com/oladeeayo/open-matchday/master"
OPENMATCHDAY_INDEX_URL = f"{OPENMATCHDAY_RAW_BASE}/LEAGUE_INDEX.md"

PROVIDER_NAME = "open-matchday"
EAT = ZoneInfo("Africa/Nairobi")
FIXTURE_RESPONSE_DIAGNOSTICS = []

# Cross-competition team history is fetched lazily only when a fixture's own
# competition does not contain enough recent results. Cache it for this process
# so multiple cup/newly-promoted fixtures do not trigger repeated downloads.
OPENMATCHDAY_TEAM_HISTORY_CACHE = {"saved_at": 0.0, "rows": []}
OPENMATCHDAY_TEAM_HISTORY_LOCK = threading.RLock()

# Stop every bot command from repeatedly hitting GOAL API after a 429.
GOAL_API_COOLDOWN_UNTIL = None
GOAL_API_COOLDOWN_LOCK = threading.Lock()
GOAL_API_COOLDOWN_SECONDS = 60
GOAL_API_RATE_LIMIT_FAILURES = 0
GOAL_API_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 6 * 60 * 60

# Shared in-process response cache: Telegram commands read cached data first.
# Successful responses are cached by endpoint + parameters. Expired entries
# remain available as stale fallback if GOAL API returns 429 or is unreachable.
GOAL_RESPONSE_CACHE = {}
GOAL_RESPONSE_CACHE_LOCK = threading.RLock()
GOAL_CACHE_STALE_SECONDS = 6 * 60 * 60

def goal_cache_ttl(endpoint):
    if endpoint == "fixtures" or endpoint.startswith("fixtures/date/"):
        return 15 * 60
    if endpoint == "fixtures/live":
        return 30
    if endpoint.startswith("fixtures/"):
        return 10 * 60
    if endpoint == "leagues":
        return 24 * 60 * 60
    return 5 * 60

def goal_cache_key(endpoint, params):
    return (str(endpoint), json.dumps(params or {}, sort_keys=True, default=str))

def goal_cached_response(key, allow_stale=False):
    with GOAL_RESPONSE_CACHE_LOCK:
        entry = GOAL_RESPONSE_CACHE.get(key)
        if not entry:
            return None
        age = time.time() - entry["saved_at"]
        if age <= entry["ttl"] or (allow_stale and age <= GOAL_CACHE_STALE_SECONDS):
            return copy.deepcopy(entry["payload"])
    return None

def goal_store_response(key, endpoint, payload):
    with GOAL_RESPONSE_CACHE_LOCK:
        GOAL_RESPONSE_CACHE[key] = {
            "payload": copy.deepcopy(payload),
            "saved_at": time.time(),
            "ttl": goal_cache_ttl(endpoint),
        }
        # Keep memory bounded; remove entries older than the stale window.
        cutoff = time.time() - GOAL_CACHE_STALE_SECONDS
        for old_key, entry in list(GOAL_RESPONSE_CACHE.items()):
            if entry["saved_at"] < cutoff:
                GOAL_RESPONSE_CACHE.pop(old_key, None)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("punter_kenya")

# Telegram Bot API embeds the bot token in request URLs. Keep HTTP client
# access logs quiet so credentials are never written to Railway logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

POLLING_LOCK_NAME = "punter_kenya_telegram_polling"
_polling_lock_connection = None
_polling_lock_keepalive_stop = threading.Event()
_polling_lock_keepalive_thread = None


def acquire_polling_lock():
    """Use a database advisory lock when configured; keep local mode database-optional."""
    global _polling_lock_connection, _polling_lock_keepalive_thread

    required = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        logger.warning(
            "Database is not configured (%s). Starting in local/basic mode; "
            "database-backed history and saved-slip features will be unavailable.",
            ", ".join(missing),
        )
        return False

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s) AS acquired", (POLLING_LOCK_NAME, 10))
            row = cursor.fetchone()
        acquired = row.get("acquired") if isinstance(row, dict) else None
        if acquired != 1:
            raise RuntimeError(
                "Another PUNTER KENYA process already owns the Telegram polling lock. "
                "Stop the duplicate bot process before starting another."
            )
        _polling_lock_connection = connection
    except Exception:
        connection.close()
        raise

    def keep_lock_alive():
        while not _polling_lock_keepalive_stop.wait(45):
            try:
                _polling_lock_connection.ping(reconnect=False)
            except Exception:
                logger.critical(
                    "Telegram polling lock database connection was lost; "
                    "stopping this process to avoid competing pollers.",
                    exc_info=True,
                )
                os._exit(1)

    _polling_lock_keepalive_thread = threading.Thread(
        target=keep_lock_alive,
        name="telegram-polling-lock-keepalive",
        daemon=True,
    )
    _polling_lock_keepalive_thread.start()
    logger.info("Telegram polling singleton lock acquired.")
    return True

def release_polling_lock():
    global _polling_lock_connection
    _polling_lock_keepalive_stop.set()
    connection = _polling_lock_connection
    _polling_lock_connection = None
    if connection is None:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (POLLING_LOCK_NAME,))
    except Exception:
        logger.warning("Could not explicitly release Telegram polling lock.", exc_info=True)
    finally:
        connection.close()


# Reuse HTTP connections and retry temporary upstream failures.
HTTP = requests.Session()

_RETRY = Retry(
    total=1,
    connect=1,
    read=1,
    status=1,
    backoff_factor=0.25,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    respect_retry_after_header=True,
)

HTTP.mount("https://", HTTPAdapter(max_retries=_RETRY))
HTTP.mount("http://", HTTPAdapter(max_retries=_RETRY))


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc_naive():
    """UTC datetime suitable for MySQL DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def safe_int(value):
    if value is None or value == "":
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def first_value(data, *keys):
    if not isinstance(data, dict):
        return None

    for key in keys:
        value = data.get(key)

        if value is not None and value != "":
            return value

    return None


def nested_name(value):
    if isinstance(value, dict):
        return first_value(
            value,
            "name",
            "teamName",
            "title",
            "displayName",
        )

    if isinstance(value, str):
        return value

    return None


def parse_datetime(value):
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()

        try:
            parsed = datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )
        except ValueError:
            return None

    if parsed.tzinfo is None:
        # API timestamps without an offset are treated as UTC.
        return parsed

    return parsed.astimezone(
        timezone.utc
    ).replace(tzinfo=None)


def extract_list(payload, preferred_keys=()):
    """Extract a list from GOAL API envelopes, including nested data objects."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    preferred = list(preferred_keys) + [
        "items", "results", "fixtures", "matches",
        "leagues", "competitions", "data",
    ]
    visited = set()

    def walk(value, depth=0):
        if isinstance(value, list):
            return value
        if not isinstance(value, dict) or depth > 5 or id(value) in visited:
            return []
        visited.add(id(value))

        # Prefer known collection keys before inspecting other nested objects.
        for key in preferred:
            if key in value:
                found = walk(value[key], depth + 1)
                if found:
                    return found
        for child in value.values():
            if isinstance(child, (dict, list)):
                found = walk(child, depth + 1)
                if found:
                    return found
        return []

    return walk(payload)


def extract_fixtures(payload):
    return extract_list(
        payload,
        ("fixtures", "matches"),
    )


def extract_leagues(payload):
    return extract_list(
        payload,
        ("leagues", "competitions"),
    )


def response_metadata(payload):
    if not isinstance(payload, dict):
        return {}

    metadata = (
        payload.get("pagination")
        or payload.get("meta")
        or {}
    )

    return metadata if isinstance(metadata, dict) else {}


# ============================================================
# GOAL API CONNECTION
# ============================================================

def goal_request(endpoint, params=None):
    global GOAL_API_COOLDOWN_UNTIL, GOAL_API_RATE_LIMIT_FAILURES

    cache_key = goal_cache_key(endpoint, params)
    cached = goal_cached_response(cache_key)
    if cached is not None:
        logger.info("GOAL API cache hit: %s", endpoint)
        return cached

    stale_cached = goal_cached_response(cache_key, allow_stale=True)

    with GOAL_API_COOLDOWN_LOCK:
        cooldown_until = GOAL_API_COOLDOWN_UNTIL
        if cooldown_until and datetime.now(timezone.utc) < cooldown_until:
            remaining = max(
                1,
                int((cooldown_until - datetime.now(timezone.utc)).total_seconds())
            )
            if stale_cached is not None:
                logger.warning("Serving stale GOAL API cache during cooldown: %s", endpoint)
                return stale_cached
            raise RuntimeError(
                f"GOAL API is temporarily paused after a rate-limit response. "
                f"Try again in about {remaining} seconds."
            )
        if cooldown_until and datetime.now(timezone.utc) >= cooldown_until:
            GOAL_API_COOLDOWN_UNTIL = None

    if not GOAL_API_KEY or not GOAL_API_BASE:
        missing = [name for name, value in (("GOAL_API_KEY", GOAL_API_KEY), ("GOAL_API_BASE", GOAL_API_BASE)) if not value]
        raise RuntimeError(
            "GOAL API is not configured; missing " + ", ".join(missing)
            + ". Set these only if using the supplementary GOAL API source."
        )

    url = f"{GOAL_API_BASE}/{endpoint.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {GOAL_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "PunterKenya/1.0",
    }

    try:
        response = HTTP.get(
            url,
            headers=headers,
            params=params or {},
            timeout=(5, 12),
        )

        response.raise_for_status()
        with GOAL_API_COOLDOWN_LOCK:
            GOAL_API_RATE_LIMIT_FAILURES = 0

    except requests.HTTPError as exc:

        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )

        body = ""

        if exc.response is not None:
            body = (exc.response.text or "")[:500]

        rate_limit_details = ""
        if status == 429 and exc.response is not None:
            response_headers = exc.response.headers
            retry_after = response_headers.get("Retry-After")
            reset_value = response_headers.get("X-RateLimit-Reset")
            cooldown_seconds = GOAL_API_COOLDOWN_SECONDS

            # Prefer provider timing. If the gateway omits all timing headers,
            # back off progressively so repeated Telegram commands do not
            # hammer an already rate-limited account.
            try:
                if retry_after:
                    cooldown_seconds = max(1, int(float(retry_after)))
                    GOAL_API_RATE_LIMIT_FAILURES = 0
                elif reset_value:
                    reset_timestamp = float(reset_value)
                    cooldown_seconds = max(
                        1,
                        int(reset_timestamp - datetime.now(timezone.utc).timestamp())
                    )
                    GOAL_API_RATE_LIMIT_FAILURES = 0
                else:
                    GOAL_API_RATE_LIMIT_FAILURES += 1
                    cooldown_seconds = min(
                        GOAL_API_COOLDOWN_SECONDS
                        * (2 ** min(GOAL_API_RATE_LIMIT_FAILURES + 3, 9)),
                        GOAL_API_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
                    )
            except (TypeError, ValueError, OverflowError):
                GOAL_API_RATE_LIMIT_FAILURES += 1
                cooldown_seconds = min(
                    GOAL_API_COOLDOWN_SECONDS
                    * (2 ** min(GOAL_API_RATE_LIMIT_FAILURES + 3, 9)),
                    GOAL_API_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
                )

            with GOAL_API_COOLDOWN_LOCK:
                GOAL_API_COOLDOWN_UNTIL = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=cooldown_seconds)
                )

            header_values = {
                "type": response_headers.get("X-RateLimit-Type"),
                "remaining": response_headers.get("X-RateLimit-Remaining"),
                "limit": response_headers.get("X-RateLimit-Limit"),
                "reset": reset_value,
                "retry_after": retry_after,
            }
            available = [
                f"{key}={value}"
                for key, value in header_values.items()
                if value not in (None, "")
            ]
            rate_limit_details = (
                " | Rate-limit headers: " + ", ".join(available)
                if available else
                " | Provider returned no rate-limit headers; "
                f"using {cooldown_seconds}s fallback cooldown"
            )

        if stale_cached is not None:
            logger.warning("GOAL API HTTP %s; serving stale cache for %s", status, endpoint)
            return stale_cached

        raise RuntimeError(
            f"GOAL API HTTP {status}: {body}{rate_limit_details}"
        ) from None

    except requests.RequestException as exc:

        if stale_cached is not None:
            logger.warning("GOAL API connection failed; serving stale cache for %s", endpoint)
            return stale_cached

        raise RuntimeError(
            f"GOAL API connection error: "
            f"{exc.__class__.__name__}: {exc}"
        ) from None

    if response.status_code == 204:
        return {"success": True, "data": [], "pagination": {"hasMore": False}}

    try:
        payload = response.json()

    except ValueError:

        raise RuntimeError(
            "GOAL API returned a response that was not valid JSON."
        ) from None

    if (
        isinstance(payload, dict)
        and payload.get("success") is False
    ):
        raise RuntimeError(
            str(
                payload.get("message")
                or "GOAL API reported an unsuccessful request."
            )[:500]
        )

    if endpoint == "fixtures" or endpoint.startswith("fixtures/date/"):
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            data_type = "list"
            data_count = len(data)
            sample = repr(data[0])[:650] if data else "empty list"
            data_keys = ""
        elif isinstance(data, dict):
            data_type = "dict"
            data_count = "n/a"
            data_keys = ",".join(str(k) for k in list(data.keys())[:25])
            sample = repr(data)[:650]
        else:
            data_type = type(data).__name__
            data_count = "n/a"
            data_keys = ""
            sample = repr(payload)[:650]
        diagnostic = (
            f"endpoint={endpoint}; root_keys="
            f"{','.join(str(k) for k in list(payload.keys())[:25]) if isinstance(payload, dict) else type(payload).__name__}; "
            f"success={payload.get('success') if isinstance(payload, dict) else 'n/a'}; "
            f"data_type={data_type}; data_count={data_count}; "
            f"data_keys={data_keys}; sample={sample}"
        )
        FIXTURE_RESPONSE_DIAGNOSTICS.append(diagnostic)
        del FIXTURE_RESPONSE_DIAGNOSTICS[4:]

    goal_store_response(cache_key, endpoint, payload)
    return payload


# ============================================================
# FIXTURE NORMALISATION
# ============================================================

def normalise_fixture(raw):

    if not isinstance(raw, dict):
        return None

    fixture_id = first_value(
        raw,
        "id",
        "fixtureId",
        "matchId",
        "matchID",
        "match_id",
    )

    home = first_value(
        raw,
        "homeTeamName",
        "homeTeam",
        "home_team",
        "home",
        "team1",
    )

    away = first_value(
        raw,
        "awayTeamName",
        "awayTeam",
        "away_team",
        "away",
        "team2",
    )

    home_name = nested_name(home)
    away_name = nested_name(away)

    if (
        fixture_id is None
        or not home_name
        or not away_name
    ):
        return None

    competition = first_value(
        raw,
        "leagueName",
        "competitionName",
        "competition",
        "league",
        "leagueName",
    )

    country = first_value(
        raw,
        "countryName",
        "country",
    )

    if isinstance(competition, dict):

        if country is None:
            country = nested_name(
                competition.get("country")
            )

        competition = nested_name(competition)

    if isinstance(country, dict):
        country = nested_name(country)

    kickoff = first_value(
        raw,
        "kickoffUtc",
        "kickoff_utc",
        "dateTime",
        "matchDateTimeUTC",
        "matchDateTime",
        "startTime",
    )
    if kickoff is None:
        match_date = first_value(raw, "matchDate", "date")
        match_time = first_value(raw, "matchTime", "time")
        if match_date and match_time:
            time_text = str(match_time).strip()
            if len(time_text) == 5:
                time_text += ":00"
            kickoff = f"{str(match_date)[:10]}T{time_text}"
        else:
            kickoff = match_date

    status = first_value(
        raw,
        "matchStatus",
        "status",
        "fixtureStatus",
        "state",
    )
    if status is None and "matchIsFinished" in raw:
        status = "Finished" if raw.get("matchIsFinished") else "Scheduled"

    home_score = first_value(raw, "homeTeamScore", "homeScore", "home_score")
    away_score = first_value(raw, "awayTeamScore", "awayScore", "away_score")
    if home_score is None or away_score is None:
        results = raw.get("matchResults") or []
        if isinstance(results, list):
            full_time = next(
                (r for r in results if isinstance(r, dict) and safe_int(r.get("resultTypeID")) == 2),
                None,
            )
            if full_time:
                home_score = full_time.get("pointsTeam1", home_score)
                away_score = full_time.get("pointsTeam2", away_score)

    return {
        "provider": PROVIDER_NAME,
        "provider_match_id": str(fixture_id),
        "competition": (
            str(competition)
            if competition is not None
            else None
        ),
        "country": (
            str(country)
            if country is not None
            else None
        ),
        "home_team": str(home_name),
        "away_team": str(away_name),
        "kickoff_at": parse_datetime(kickoff),
        "status": (
            str(status)
            if status is not None
            else None
        ),
        "home_score": safe_int(home_score),
        "away_score": safe_int(away_score),
    }


def normalise_league(raw):

    if not isinstance(raw, dict):
        return None

    league_id = first_value(
        raw,
        "id",
        "leagueId",
        "competitionId",
        "league_id",
    )

    name = first_value(
        raw,
        "name",
        "leagueName",
        "competitionName",
        "title",
    )

    country = first_value(
        raw,
        "countryName",
        "country",
    )

    if isinstance(country, dict):
        country = nested_name(country)

    if league_id is None and name is None:
        return None

    shortcut = first_value(raw, "leagueShortcut", "shortcut")
    season = first_value(raw, "leagueSeason", "season")
    provider_id = f"{shortcut}|{season}" if shortcut and season is not None else league_id
    return {
        "id": str(provider_id) if provider_id is not None else None,
        "name": str(name) if name is not None else "Name unavailable",
        "country": str(country) if country is not None else "open-matchday",
        "raw": raw,
    }


def canonical_team_key(value):
    """Normalise common club-name variants before comparing fixtures."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    phrase = " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
    aliases = {
        "los angeles football club": "lafc",
        "la football club": "lafc",
        "los angeles fc": "lafc",
        "manchester utd": "manchester united",
        "man utd": "manchester united",
        "psg": "paris saint germain",
    }
    phrase = aliases.get(phrase, phrase)
    removable = {"fc", "cf", "sc", "afc", "football", "club"}
    return " ".join(word for word in phrase.split() if word not in removable)


def canonical_competition_key(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def upcoming_fixtures(raw_fixtures, limit=None, offset=0, include_past=False):
    """Return fixtures sorted by kickoff, removing provider aliases."""
    now = now_utc_naive()
    valid = []
    seen_ids = set()
    seen_matches = set()

    for raw in raw_fixtures:
        item = normalise_fixture(raw)
        if not item or not item["kickoff_at"] or (not include_past and item["kickoff_at"] < now):
            continue

        fixture_id = str(item["provider_match_id"])
        match_key = (
            canonical_competition_key(item.get("competition")),
            item["kickoff_at"].replace(second=0, microsecond=0),
            canonical_team_key(item["home_team"]),
            canonical_team_key(item["away_team"]),
        )
        if fixture_id in seen_ids or match_key in seen_matches:
            continue

        seen_ids.add(fixture_id)
        seen_matches.add(match_key)
        valid.append((item["kickoff_at"], raw, item))

    valid.sort(key=lambda row: (row[0], canonical_competition_key(row[2].get("competition"))))
    start = max(0, int(offset or 0))
    end = start + max(0, int(limit)) if limit is not None else None
    return [(raw, item) for _, raw, item in valid[start:end]]




def openmatchday_season_code():
    """Return the current season code used by open-matchday."""
    now = datetime.now(EAT)
    start_year = now.year if now.month >= 7 else now.year - 1
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def openmatchday_previous_season_code():
    """Return the preceding season code for deeper team-form samples."""
    now = datetime.now(EAT)
    start_year = (now.year if now.month >= 7 else now.year - 1) - 1
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def openmatchday_index():
    """Load the published 75-competition index without authentication."""
    cache_key = goal_cache_key("open-matchday:index", {})
    cached = goal_cached_response(cache_key)
    if cached is not None:
        return cached

    response = HTTP.get(
        OPENMATCHDAY_INDEX_URL,
        headers={"Accept": "text/plain", "User-Agent": "PunterKenya/1.0"},
        timeout=(5, 20),
    )
    response.raise_for_status()
    rows = []
    pattern = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|")
    for line in response.text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        folder, name, country = [part.strip() for part in match.groups()]
        if folder and name and country and folder.lower() != "folder":
            rows.append({
                "id": f"{folder}|{openmatchday_season_code()}",
                "leagueShortcut": folder,
                "leagueSeason": openmatchday_season_code(),
                "leagueName": name,
                "countryName": country,
            })

    if not rows:
        raise RuntimeError("Open-matchday returned an empty or unrecognised league index.")
    goal_store_response(cache_key, "leagues", rows)
    return rows


def fetch_all_leagues():
    """Return the complete football-only competition index from open-matchday."""
    return openmatchday_index()


def fetch_openmatchday_csv(folder, kind="fixtures", season_code=None):
    """Fetch one public CSV file; cache successful responses to reduce traffic."""
    season = str(season_code or openmatchday_season_code())
    path = f"data/{folder}/{kind}_{season}.csv"
    cache_key = goal_cache_key("open-matchday:" + path, {})
    cached = goal_cached_response(cache_key)
    if cached is not None:
        return cached

    url = f"{OPENMATCHDAY_RAW_BASE}/{path}"
    response = HTTP.get(
        url,
        headers={"Accept": "text/csv", "User-Agent": "PunterKenya/1.0"},
        timeout=(5, 20),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()

    rows = list(csv.DictReader(io.StringIO(response.text)))
    goal_store_response(cache_key, path, rows)
    return rows


def fetch_openmatchday_results_history(folder):
    """Combine current and previous-season results for reliable recent team form."""
    combined = []
    seen = set()
    for season in (openmatchday_season_code(), openmatchday_previous_season_code()):
        try:
            season_rows = fetch_openmatchday_csv(folder, "results", season_code=season)
        except Exception as exc:
            logger.info("Results history unavailable for %s season %s: %s", folder, season, exc)
            continue
        for row in season_rows:
            key = (
                str(row.get("date") or ""),
                str(row.get("home") or "").strip().casefold(),
                str(row.get("away") or "").strip().casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            combined.append(row)
    combined.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    return combined


def fetch_openmatchday_team_history_rows(max_age_seconds=1800):
    """Load a cached cross-competition results pool for team-form fallback."""
    with OPENMATCHDAY_TEAM_HISTORY_LOCK:
        cached_at = float(OPENMATCHDAY_TEAM_HISTORY_CACHE.get("saved_at") or 0)
        cached_rows = OPENMATCHDAY_TEAM_HISTORY_CACHE.get("rows") or []
        if cached_at and time.time() - cached_at < max_age_seconds:
            return list(cached_rows)

    leagues = fetch_all_leagues()
    collected = []

    def fetch_one(league):
        folder = str(league.get("leagueShortcut") or "").strip()
        if not folder:
            return []
        try:
            return fetch_openmatchday_results_history(folder)
        except Exception as exc:
            logger.info("Cross-competition history unavailable for %s: %s", folder, exc)
            return []

    with ThreadPoolExecutor(max_workers=min(24, max(1, len(leagues)))) as pool:
        futures = [pool.submit(fetch_one, league) for league in leagues]
        for future in as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception:
                continue

    with OPENMATCHDAY_TEAM_HISTORY_LOCK:
        OPENMATCHDAY_TEAM_HISTORY_CACHE["saved_at"] = time.time()
        OPENMATCHDAY_TEAM_HISTORY_CACHE["rows"] = list(collected)
    return collected


def fetch_openmatchday_fixture_rows(days=14, league_id=None, day_filter=None):
    """Fetch fixtures for a selected EAT calendar day, or a rolling upcoming window."""
    FIXTURE_RESPONSE_DIAGNOSTICS.clear()
    leagues = fetch_all_leagues()
    if league_id:
        parts = str(league_id).split("|", 1)
        if len(parts) != 2:
            raise RuntimeError("Invalid open-matchday competition reference.")
        leagues = [
            row for row in leagues
            if str(row.get("leagueShortcut")) == parts[0]
            and str(row.get("leagueSeason")) == parts[1]
        ]

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_eat = datetime.now(EAT).date()
    target_date = today_eat + timedelta(days=1) if day_filter == "tomorrow" else today_eat
    cutoff = now + timedelta(days=max(1, int(days or 14)))
    collected = []
    errors = []

    def fetch_one(league):
        folder = league["leagueShortcut"]
        rows = fetch_openmatchday_csv(folder, "fixtures")
        output = []
        for row in rows:
            match_date = str(row.get("date") or "").strip()
            match_time = str(row.get("time") or "").strip()
            home = str(row.get("home") or "").strip()
            away = str(row.get("away") or "").strip()
            if not match_date or not match_time or not home or not away:
                continue
            try:
                # Dataset documents its kickoff clock as fixed UTC+1 (CET-1 convention).
                kickoff = datetime.fromisoformat(f"{match_date}T{match_time}:00+01:00")
                kickoff_utc = kickoff.astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
            kickoff_naive = kickoff.astimezone(timezone.utc).replace(tzinfo=None)
            kickoff_eat_date = kickoff.astimezone(EAT).date()
            # Always apply a live UTC cutoff, including exact-day searches.
            # A calendar-day match is not necessarily still upcoming.
            if kickoff_naive <= now:
                continue
            if day_filter:
                if kickoff_eat_date != target_date:
                    continue
            elif kickoff_naive > cutoff:
                continue
            stable_id = f"{folder}:{match_date}:{home}:{away}"
            output.append({
                "id": stable_id,
                "home": home,
                "away": away,
                "kickoffUtc": kickoff_utc,
                "matchStatus": "Scheduled",
                "leagueName": league["leagueName"],
                "countryName": league["countryName"],
                "leagueShortcut": folder,
                "leagueSeason": league["leagueSeason"],
            })
        return output

    # The index contains 75 competitions. Consume whichever request completes
    # first so one slow league cannot block already completed fixture feeds.
    # Fetch league fixture files concurrently: the index covers many competitions,
    # and a small worker pool can make a cold scan exceed the Telegram timeout.
    with ThreadPoolExecutor(max_workers=min(24, max(1, len(leagues)))) as pool:
        futures = [pool.submit(fetch_one, league) for league in leagues]
        for future in as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:140]}")
                logger.warning("Open-matchday competition request failed: %s", exc)

    if errors:
        FIXTURE_RESPONSE_DIAGNOSTICS.append(
            f"Open-matchday CSV requests failed: {len(errors)} of {len(leagues)} competitions"
        )
    return collected


def fetch_matchintel_fixture_rows(days=14):
    return fetch_openmatchday_fixture_rows(days)


def fetch_upcoming_fixture_rows(days=14, league_id=None):
    return fetch_openmatchday_fixture_rows(days, league_id)


# ============================================================
# DATABASE STORAGE
# ============================================================

def get_table_columns(cursor, table_name):

    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = %s
        """,
        (table_name,),
    )

    return {
        row["COLUMN_NAME"]
        for row in cursor.fetchall()
    }


def save_leagues_to_database(raw_leagues):
    """Upsert the complete provider league catalogue into the leagues table."""
    connection = get_connection()
    saved = 0
    skipped = 0

    try:
        with connection.cursor() as cursor:
            sql = """
                INSERT INTO leagues
                    (provider, provider_league_id, name, region)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    region = VALUES(region)
            """
            for raw in raw_leagues:
                item = normalise_league(raw)
                if not item or not item["id"]:
                    skipped += 1
                    continue
                cursor.execute(
                    sql,
                    (PROVIDER_NAME, item["id"], item["name"], item["country"]),
                )
                saved += 1

        return {"received": len(raw_leagues), "saved": saved, "skipped": skipped}
    finally:
        connection.close()


def save_fixtures_to_database(raw_fixtures):
    """
    Store fixtures using columns that actually exist
    in the matches table.

    This avoids crashing merely because an optional
    tracking column such as last_synced_at is missing.
    """

    connection = get_connection()

    saved = 0
    skipped = 0

    try:

        with connection.cursor() as cursor:

            columns = get_table_columns(
                cursor,
                "matches",
            )

            required = {
                "provider",
                "provider_match_id",
                "home_team",
                "away_team",
            }

            missing = required - columns

            if missing:
                raise RuntimeError(
                    "The matches table is missing required columns: "
                    + ", ".join(sorted(missing))
                )

            optional_fields = [
                "competition",
                "country",
                "kickoff_at",
                "status",
                "home_score",
                "away_score",
                "last_synced_at",
            ]

            fields = [
                "provider",
                "provider_match_id",
                "home_team",
                "away_team",
            ]

            fields += [
                field
                for field in optional_fields
                if field in columns
            ]

            update_fields = [
                field
                for field in fields
                if field not in {
                    "provider",
                    "provider_match_id",
                }
            ]

            assignments = [
                f"`{field}` = VALUES(`{field}`)"
                for field in update_fields
            ]

            sql_fields = ", ".join(
                f"`{field}`"
                for field in fields
            )

            placeholders = ", ".join(
                ["%s"] * len(fields)
            )

            sql = (
                f"INSERT INTO `matches` ({sql_fields}) "
                f"VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE "
                f"{', '.join(assignments)}"
            )

            for raw in raw_fixtures:

                item = normalise_fixture(raw)

                if item is None:
                    skipped += 1
                    continue

                values = []

                for field in fields:

                    if field == "last_synced_at":
                        values.append(now_utc_naive())

                    else:
                        values.append(item.get(field))

                cursor.execute(
                    sql,
                    tuple(values),
                )

                saved += 1

        return {
            "received": len(raw_fixtures),
            "saved": saved,
            "skipped": skipped,
        }

    finally:
        connection.close()


# ============================================================
# TELEGRAM HELPERS
# ============================================================

async def reply(update: Update, message: str):

    if update.effective_message:
        await update.effective_message.reply_text(
            message[:4000]
        )


# ============================================================
# START AND HELP
# ============================================================

def main_menu_keyboard():
    """Persistent Telegram menu inspired by the reference layout."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📅 Today's Matches"), KeyboardButton("🏆 Top Leagues")],
            [KeyboardButton("🔎 Analyze Match"), KeyboardButton("ℹ️ iHelp")],
            [KeyboardButton("🤖 Build a slip"), KeyboardButton("📝 Manual slip")],
            [KeyboardButton("✏️ Edit slip"), KeyboardButton("🔗 Merge slips")],
            [KeyboardButton("✂️ Split slip"), KeyboardButton("🎯 Trim to target odds")],
            [KeyboardButton("📋 Bookies & regions"), KeyboardButton("🎟️ My slips")],
            [KeyboardButton("⚽ Match intelligence"), KeyboardButton("❓ Contact support")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Pick an option…",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "⚽ PUNTER KENYA\n"
        "FOOTBALL INTELLIGENCE & BETTING CENTRE\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Research matches, compare available bookmaker prices and review potential value.\n\n"
        "🎟️ SLIP MANAGEMENT\n"
        "🎟️ Generate genuine SportyBet Kenya booking codes from live fixtures and markets.\n"
        "No bookmaker account, API key or stake is used to prepare a code.\n\n"
        "🧠 INTELLIGENCE\n"
        "/fullintel — all research and analysis commands\n"
        "/deepdive HOME vs AWAY — full match report\n"
        "/topvalue — bookmaker value scan\n"
        "/accumulator [TARGET] — qualifying-leg accumulator scan\n"
        "/bookies — bookmaker feed status\n"
        "/odds HOME vs AWAY — compare retrieved prices\n"
        "/fixtures — upcoming fixtures\n\n"
        "🧠 AI ASSISTANT\n"
        "Ask match questions in ordinary language, or send a betting-slip screenshot for reading and live selection validation.\n\n"
        "⚠️ Estimates are not guarantees. Prices must be checked with the bookmaker. "
        "PUNTER KENYA does not place bets."
    )
    await update.effective_message.reply_text(
        message,
        reply_markup=main_menu_keyboard(),
    )


def parse_slip_lines(raw_text):
    """Parse one selection per line: Fixture | Market | Pick | Decimal odds [| Bookmaker]."""
    selections = []
    bookmaker = "Manual"
    for line_number, raw in enumerate(raw_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (4, 5):
            raise ValueError(
                f"Line {line_number}: use Fixture | Market | Selection | Odds [| Bookmaker]."
            )
        fixture, market, selection, odds_text = parts[:4]
        try:
            odds = float(odds_text)
        except ValueError:
            raise ValueError(f"Line {line_number}: odds must be a decimal number.")
        if not fixture or not market or not selection or not math.isfinite(odds) or odds <= 1.0 or odds > 1000:
            raise ValueError(f"Line {line_number}: fixture, market, selection and odds above 1.00 are required.")
        if len(parts) == 5 and parts[4]:
            bookmaker = parts[4][:80]
        selections.append({
            "fixture": fixture[:180],
            "market": market[:100],
            "selection": selection[:120],
            "odds": round(odds, 4),
        })
    if not selections:
        raise ValueError("No selections found. Send at least one non-empty selection line.")
    if len(selections) > 50:
        raise ValueError("A slip can contain no more than 50 selections.")
    return selections, bookmaker


def format_slip(slip_id, selections, bookmaker="Manual"):
    combined = math.prod(float(item["odds"]) for item in selections)
    lines = [
        f"🎟️ PUNTER KENYA — SLIP #{slip_id}",
        f"Bookmaker label: {bookmaker}",
        f"Selections: {len(selections)}",
        f"Combined decimal odds: {combined:.2f}",
        "",
    ]
    for index, item in enumerate(selections, 1):
        lines.append(
            f"{index}. {item['fixture']}\n"
            f"   {item['market']}: {item['selection']} @ {float(item['odds']):.2f}"
        )
    lines.extend(["", "Internal PUNTER slip record only — not a bookmaker booking code and no bet has been placed."])
    return "\n".join(lines)


async def process_slip_flow(update, context):
    state = context.user_data.get("slip_flow")
    if not state:
        return False
    message = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id
    if message.casefold() in ("/cancel", "cancel", "❌ cancel"):
        context.user_data.pop("slip_flow", None)
        await reply(update, "Cancelled. Your existing slips have not been changed.")
        return True

    try:
        if state == "autobuild_target":
            try:
                target = float(message.lower().replace("odds", "").strip())
            except ValueError:
                await reply(update, "Enter your target combined odds, e.g. 2, 5, 10, 20 or 50.")
                return True
            if not math.isfinite(target) or target <= 1:
                await reply(update, "Target odds must be greater than 1.00.")
                return True
            # Target-only flow: leg count is selected automatically by the
            # accumulator engine. Clear legacy state so old sessions cannot ask
            # the user for an exact number of legs.
            context.user_data.pop("autobuild_target", None)
            context.user_data.pop("autobuild_config", None)
            context.user_data.pop("slip_flow", None)
            context.args = [f"{target:.12g}"]
            await autobuild_sportybet_command(update, context)
            return True

        if state == "autobuild_legs":
            # Compatibility for users who were left on the old exact-leg prompt:
            # ignore the submitted leg count and continue using their saved target.
            target = float(context.user_data.get("autobuild_target") or 0)
            context.user_data.pop("autobuild_target", None)
            context.user_data.pop("autobuild_config", None)
            context.user_data.pop("slip_flow", None)
            if not math.isfinite(target) or target <= 1:
                await reply(update, "That old slip request expired. Press Build a slip and enter only your target combined odds.")
                return True
            context.args = [f"{target:.12g}"]
            await autobuild_sportybet_command(update, context)
            return True

        if state == "sportybet_build":
            if message.casefold() in ("validate", "confirm selections"):
                raw_lines = context.user_data.pop("ai_ocr_lines", None) or []
                if not raw_lines:
                    await reply(update, "The extracted image selections have expired. Send the screenshot again.")
                    return True
            else:
                raw_lines = [line.strip() for line in message.splitlines() if line.strip()]
            resolved = await asyncio.to_thread(prepare_sportybet_slip, raw_lines)
            context.user_data.pop("slip_flow", None)
            context.user_data["pending_sportybet_booking"] = resolved
            combined = math.prod(float(item["odds"]) for item in resolved)
            preview_lines = [
                "🎟️ SPORTYBET KENYA — SLIP PREVIEW",
                "━━━━━━━━━━━━━━━━━━━━",
                f"Selections: {len(resolved)}",
                f"Current combined odds: {combined:.2f}",
                "",
            ]
            for index, item in enumerate(resolved, 1):
                preview_lines.append(
                    f"{index}. {item['fixture']}\n"
                    f"   {item['market']}: {item['selection']} @ {item['odds']:.2f}"
                )
            preview_lines.extend([
                "",
                "Confirm to ask SportyBet Kenya to generate a genuine booking code.",
                "This only prepares a shareable slip. It does NOT place a bet.",
            ])
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Generate SportyBet code", callback_data="sb:confirm"),
                InlineKeyboardButton("✖️ Cancel", callback_data="sb:cancel"),
            ]])
            await update.effective_message.reply_text("\n".join(preview_lines), reply_markup=keyboard)
            return True

        if state == "sportybet_edit_code":
            code = "".join(message.split()).upper()
            booking = await asyncio.to_thread(get_sportybet_booking, code)
            context.user_data["sportybet_manage_source"] = booking
            context.user_data["slip_flow"] = "sportybet_edit_remove"
            lines = [f"✏️ SPORTYBET SLIP EDITOR — {booking['bookingCode']}", f"Selections: {len(booking['selections'])}", ""]
            for i, leg in enumerate(booking["selections"], 1):
                lines.append(f"{i}. {leg['fixture']} | {leg['market']}: {leg['selection']} @ {leg['odds']:.2f}")
            lines.extend(["", "Send the leg numbers to REMOVE, separated by commas (e.g. 2,5,7).", "Send KEEP ALL to recreate an unchanged copy, or /cancel."])
            await reply(update, "\n".join(lines))
            return True

        if state == "sportybet_edit_remove":
            booking = context.user_data.get("sportybet_manage_source") or {}
            selections = booking.get("selections") or []
            if message.casefold() == "keep all":
                kept = list(selections)
            else:
                try:
                    remove = {int(x.strip()) for x in message.split(",") if x.strip()}
                except ValueError:
                    await reply(update, "Send leg numbers separated by commas, e.g. 2,5,7, or KEEP ALL.")
                    return True
                if not remove or min(remove) < 1 or max(remove) > len(selections):
                    await reply(update, f"Choose leg numbers from 1 to {len(selections)}.")
                    return True
                kept = [x for i, x in enumerate(selections, 1) if i not in remove]
            if not kept:
                await reply(update, "You cannot generate an empty slip. Keep at least one selection.")
                return True
            context.user_data["pending_sportybet_groups"] = [kept]
            context.user_data.pop("slip_flow", None)
            combined = math.prod(float(x["odds"]) for x in kept)
            removed_count = len(selections) - len(kept)
            preview = [f"✏️ EDIT PREVIEW — {booking.get('bookingCode')}", f"Kept: {len(kept)} | Removed: {removed_count}", f"New combined odds: {combined:.2f}", ""]
            preview.extend(f"{i}. {x['fixture']} | {x['market']}: {x['selection']} @ {x['odds']:.2f}" for i, x in enumerate(kept, 1))
            preview.extend(["", "Confirm to create a NEW SportyBet code. The original code remains unchanged."])
            await update.effective_message.reply_text("\n".join(preview), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Generate edited code", callback_data="sb:manage_confirm"), InlineKeyboardButton("✖️ Cancel", callback_data="sb:manage_cancel")]]))
            return True

        if state == "sportybet_merge_codes":
            codes = [x.strip().upper() for x in re.split(r"[\s,]+", message) if x.strip()]
            if len(codes) < 2 or len(codes) > 10 or len(set(codes)) != len(codes):
                await reply(update, "Send 2–10 different SportyBet booking codes, separated by spaces or new lines.")
                return True
            bookings = await asyncio.gather(*[asyncio.to_thread(get_sportybet_booking, code) for code in codes])
            selections = []
            seen = set()
            for booking in bookings:
                for leg in booking["selections"]:
                    key = (leg["eventId"], leg["marketId"], leg["outcomeId"])
                    if key not in seen:
                        seen.add(key)
                        selections.append(leg)
            if not selections:
                raise SportyBetError("No selections could be merged.")
            if len(selections) > 50:
                raise SportyBetError(f"Merged slip has {len(selections)} unique selections; SportyBet Kenya's published betslip limit is 50.")
            context.user_data["pending_sportybet_groups"] = [selections]
            context.user_data.pop("slip_flow", None)
            odds = math.prod(float(x["odds"]) for x in selections)
            await update.effective_message.reply_text(f"🔗 SPORTYBET MERGE PREVIEW\nSource codes: {len(codes)}\nUnique selections: {len(selections)}\nCombined odds: {odds:.2f}\nDuplicate identical selections were removed.\n\nConfirm to create one new SportyBet code; source codes remain unchanged.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Generate merged code", callback_data="sb:manage_confirm"), InlineKeyboardButton("✖️ Cancel", callback_data="sb:manage_cancel")]]))
            return True

        if state == "sportybet_split_code":
            code = "".join(message.split()).upper()
            booking = await asyncio.to_thread(get_sportybet_booking, code)
            context.user_data["sportybet_manage_source"] = booking
            context.user_data["slip_flow"] = "sportybet_split_size"
            await reply(update, f"✂️ SPORTYBET SPLITTER\nLoaded {len(booking['selections'])} selections from {booking['bookingCode']}.\nSend selections per new slip (e.g. 5).")
            return True

        if state == "sportybet_split_size":
            booking = context.user_data.get("sportybet_manage_source") or {}
            try:
                chunk_size = int(message)
            except ValueError:
                await reply(update, "Send a whole number, e.g. 5.")
                return True
            selections = booking.get("selections") or []
            if chunk_size < 1 or chunk_size >= len(selections):
                await reply(update, f"Choose between 1 and {len(selections)-1}.")
                return True
            groups = [selections[i:i+chunk_size] for i in range(0, len(selections), chunk_size)]
            context.user_data["pending_sportybet_groups"] = groups
            context.user_data.pop("slip_flow", None)
            lines = [f"✂️ SPORTYBET SPLIT PREVIEW — {len(groups)} new slips"]
            for i, group in enumerate(groups, 1):
                lines.append(f"Slip {i}: {len(group)} selections | odds {math.prod(float(x['odds']) for x in group):.2f}")
            lines.append("Confirm to generate separate SportyBet codes. Original code remains unchanged.")
            await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Generate split codes", callback_data="sb:manage_confirm"), InlineKeyboardButton("✖️ Cancel", callback_data="sb:manage_cancel")]]))
            return True

        if state == "sportybet_trim_code":
            code = "".join(message.split()).upper()
            booking = await asyncio.to_thread(get_sportybet_booking, code)
            context.user_data["sportybet_manage_source"] = booking
            context.user_data["slip_flow"] = "sportybet_trim_target"
            odds = math.prod(float(x["odds"]) for x in booking["selections"])
            await reply(update, f"🎯 TARGET-ODDS TRIM\nLoaded {len(booking['selections'])} selections. Current combined odds: {odds:.2f}.\nSend your maximum target odds (e.g. 10).")
            return True

        if state == "sportybet_trim_target":
            booking = context.user_data.get("sportybet_manage_source") or {}
            try:
                target = float(message)
            except ValueError:
                await reply(update, "Enter a valid target odds number, e.g. 10.")
                return True
            if not math.isfinite(target) or target <= 1:
                await reply(update, "Target odds must be greater than 1.00.")
                return True
            kept = list(booking.get("selections") or [])
            removed = []
            # Remove the highest-odds legs first until the product reaches the requested ceiling.
            while len(kept) > 1 and math.prod(float(x["odds"]) for x in kept) > target:
                leg = max(kept, key=lambda x: float(x["odds"]))
                kept.remove(leg)
                removed.append(leg)
            if math.prod(float(x["odds"]) for x in kept) > target:
                await reply(update, "Even one remaining selection exceeds that target. Choose a higher target.")
                return True
            context.user_data["pending_sportybet_groups"] = [kept]
            context.user_data.pop("slip_flow", None)
            new_odds = math.prod(float(x["odds"]) for x in kept)
            await update.effective_message.reply_text(f"🎯 TRIM PREVIEW\nTarget: {target:.2f}\nNew odds: {new_odds:.2f}\nRetained: {len(kept)} | Removed: {len(removed)}\n\nRemoved legs:\n" + ("\n".join(f"• {x['fixture']} — {x['selection']}" for x in removed) or "None") + "\n\nConfirm to create a new SportyBet code.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Generate trimmed code", callback_data="sb:manage_confirm"), InlineKeyboardButton("✖️ Cancel", callback_data="sb:manage_cancel")]]))
            return True

        if state == "analyze":
            if not re.search(r"\s+(?:vs?\.?|v)\s+", message, flags=re.IGNORECASE):
                await reply(update, "Please include both teams, for example: Brighton vs Brentford.")
                return True
            context.user_data.pop("slip_flow", None)
            # Preserve the separator: get_analysis() joins args into one query.
            context.args = [message]
            await deepdive_command(update, context)
            return True

        if state == "build":
            selections, bookmaker = parse_slip_lines(message)
            slip_id = await asyncio.to_thread(create_slip, user_id, selections, bookmaker)
            context.user_data.pop("slip_flow", None)
            await reply(update, format_slip(slip_id, selections, bookmaker))
            return True

        if state == "edit":
            parts = [part.strip() for part in message.split("|")]
            if len(parts) != 6:
                await reply(update, "Format: SLIP_ID | LEG_NUMBER | FIXTURE | MARKET | SELECTION | ODDS\nExample: 42 | 2 | Arsenal vs Chelsea | 1X2 | Arsenal | 1.85")
                return True
            slip_id, leg_number = int(parts[0]), int(parts[1])
            slip = await asyncio.to_thread(get_slip, user_id, slip_id)
            if not slip:
                await reply(update, "Slip not found. Use /myslips to see your active slip IDs.")
                return True
            if not 1 <= leg_number <= len(slip["selections"]):
                await reply(update, f"Leg number must be between 1 and {len(slip['selections'])}.")
                return True
            odds = float(parts[5])
            if not math.isfinite(odds) or odds <= 1.0 or odds > 1000 or not all(parts[2:5]):
                await reply(update, "Invalid fixture, market, selection or odds. Odds must be above 1.00.")
                return True
            slip["selections"][leg_number - 1] = {
                "fixture": parts[2][:180], "market": parts[3][:100],
                "selection": parts[4][:120], "odds": round(odds, 4),
            }
            await asyncio.to_thread(update_slip, user_id, slip_id, slip["selections"])
            context.user_data.pop("slip_flow", None)
            await reply(update, format_slip(slip_id, slip["selections"], slip["bookmaker"]))
            return True

        if state == "merge":
            ids = [int(value) for value in re.split(r"[ ,]+", message) if value]
            if len(ids) != 2 or ids[0] == ids[1]:
                await reply(update, "Send exactly two different slip IDs, separated by a space. Example: 12 15")
                return True
            first, second = await asyncio.gather(
                asyncio.to_thread(get_slip, user_id, ids[0]),
                asyncio.to_thread(get_slip, user_id, ids[1]),
            )
            if not first or not second:
                await reply(update, "One or both slips were not found in your account. Use /myslips.")
                return True
            selections = first["selections"] + second["selections"]
            if len(selections) > 50:
                await reply(update, "Merged slip would exceed the 50-selection limit.")
                return True
            new_id = await asyncio.to_thread(create_slip, user_id, selections, "Merged")
            context.user_data.pop("slip_flow", None)
            await reply(update, format_slip(new_id, selections, "Merged") + f"\n\nSource slips #{ids[0]} and #{ids[1]} remain unchanged.")
            return True

        if state == "split":
            parts = [part.strip() for part in message.split("|")]
            if len(parts) != 2:
                await reply(update, "Format: SLIP_ID | LEGS_PER_NEW_SLIP\nExample: 21 | 3")
                return True
            slip_id, chunk_size = int(parts[0]), int(parts[1])
            slip = await asyncio.to_thread(get_slip, user_id, slip_id)
            if not slip:
                await reply(update, "Slip not found. Use /myslips.")
                return True
            if chunk_size < 1 or chunk_size >= len(slip["selections"]):
                await reply(update, f"Choose a number from 1 to {len(slip['selections']) - 1}.")
                return True
            created = []
            selections = slip["selections"]
            for offset in range(0, len(selections), chunk_size):
                chunk = selections[offset:offset + chunk_size]
                new_id = await asyncio.to_thread(create_slip, user_id, chunk, "Split")
                created.append(f"#{new_id} ({len(chunk)} legs, odds {math.prod(float(x['odds']) for x in chunk):.2f})")
            context.user_data.pop("slip_flow", None)
            await reply(update, "✂️ SPLIT COMPLETE\n\n" + "\n".join(created) + f"\n\nOriginal slip #{slip_id} remains unchanged. These are internal records, not bookmaker codes.")
            return True
    except (ValueError, TypeError) as exc:
        await reply(update, f"Could not process that input: {str(exc)[:250]}\nSend /cancel to stop.")
        return True
    except Exception as exc:
        logger.exception("Slip workflow failed")
        await reply(update, f"Slip operation failed: {type(exc).__name__}: {str(exc)[:200]}")
        context.user_data.pop("slip_flow", None)
        return True
    return False


async def sportybet_build_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":", 1)[-1]
    if action == "proof":
        legs = context.user_data.get("pending_sportybet_research") or []
        if not legs:
            await query.message.reply_text("The research report has expired. Build the slip again to regenerate its evidence.")
            return
        report = _format_autobuild_research_report(legs)
        for offset in range(0, len(report), 3500):
            await query.message.reply_text(report[offset:offset + 3500])
        return
    if action == "manage_cancel":
        context.user_data.pop("pending_sportybet_groups", None)
        await query.edit_message_text("Operation cancelled. Original SportyBet booking codes remain unchanged.")
        return
    if action == "manage_confirm":
        groups = context.user_data.get("pending_sportybet_groups") or []
        if not groups:
            await query.edit_message_text("This preview has expired. Start the operation again.")
            return
        await query.edit_message_text("⏳ Creating new SportyBet Kenya booking code(s)…")
        results = []
        try:
            for index, group in enumerate(groups, 1):
                result = await asyncio.to_thread(create_sportybet_booking, group)
                code = result.get("bookingCode")
                if not code:
                    raise SportyBetError("SportyBet did not return a booking code.")
                results.append((index, code, result.get("shareURL"), len(group)))
            lines = ["✅ SPORTYBET KENYA — NEW BOOKING CODE(S)", "━━━━━━━━━━━━━━━━━━━━"]
            for index, code, url, count in results:
                lines.append(f"{index}. CODE: {code} | {count} selections")
                if url:
                    lines.append(f"   {url}")
            lines.extend(["", "These are new shareable slips. Original booking codes remain unchanged. No wager has been placed."])
            await query.edit_message_text("\n".join(lines))
            context.user_data.pop("pending_sportybet_groups", None)
        except SportyBetError as exc:
            lines = ["⚠️ SPORTYBET DID NOT CONFIRM ALL REQUESTED CODES", str(exc)[:300]]
            if results:
                lines.append("Codes already confirmed by SportyBet:")
                lines.extend(f"{code} ({count} selections)" for _, code, _, count in results)
            lines.append("The booking request is not automatically repeated.")
            await query.edit_message_text("\n".join(lines))
            context.user_data.pop("pending_sportybet_groups", None)
        return
    pending = context.user_data.get("pending_sportybet_booking")
    if action == "cancel":
        context.user_data.pop("pending_sportybet_booking", None)
        context.user_data.pop("pending_sportybet_research", None)
        await query.edit_message_text("Slip generation cancelled. No booking code was created.")
        return
    if action != "confirm" or not pending:
        await query.edit_message_text("This slip preview has expired. Start again from 🤖 Build a slip.")
        return

    # Never retry a booking POST automatically: a timeout may happen after the
    # bookmaker has already created the share code.
    await query.edit_message_text("⏳ Generating your genuine SportyBet Kenya booking code…")
    try:
        result = await asyncio.to_thread(create_sportybet_booking, pending)
        code = result.get("bookingCode")
        if not code:
            raise SportyBetError("SportyBet did not return a booking code.")
        actual_legs = result.get("legs") or []
        try:
            saved_slip_id = await asyncio.to_thread(create_slip, update.effective_user.id, pending, "SportyBet")
        except Exception as storage_error:
            saved_slip_id = None
            logger.warning("Booking code created but internal slip archive failed: %s", storage_error)
        lines = [
            "✅ SPORTYBET KENYA BOOKING CODE CREATED",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🎟️ CODE: {code}",
            f"Selections in response: {len(actual_legs) if actual_legs else len(pending)}/{len(pending)}",
            "",
            "Open SportyBet Kenya, load this code and verify every selection and the current odds.",
            "No wager has been placed.",
        ]
        if saved_slip_id is not None:
            lines.append(f"Saved to PUNTER KENYA slip history as #{saved_slip_id}.")
        if result.get("shareURL"):
            lines.extend(["", f"Slip link: {result['shareURL']}"])
        if result.get("unavailable"):
            lines.extend(["", f"⚠️ SportyBet reported {len(result['unavailable'])} unavailable selection(s). Check the loaded code carefully."])
        await query.edit_message_text("\n".join(lines))
        context.user_data.pop("pending_sportybet_booking", None)
        context.user_data.pop("pending_sportybet_research", None)
    except SportyBetError as exc:
        logger.warning("SportyBet booking creation failed: %s", exc)
        await query.edit_message_text(
            "⚠️ SPORTYBET DID NOT CONFIRM A BOOKING CODE\n\n"
            + str(exc)[:450]
            + "\n\nThe request was not automatically repeated. Refresh fixtures and rebuild if no code appears."
        )
        context.user_data.pop("pending_sportybet_booking", None)
        context.user_data.pop("pending_sportybet_research", None)
    except Exception as exc:
        logger.exception("Unexpected SportyBet booking error")
        await query.edit_message_text(
            f"⚠️ Could not verify the booking response ({type(exc).__name__}). "
            "The request was not automatically repeated. Check SportyBet before trying again."
        )
        context.user_data.pop("pending_sportybet_booking", None)


async def build_sportybet_command(update, context):
    """Default /build and /slip commands automatically select qualifying SportyBet picks."""
    # Preserve user-supplied target, market and leg-count arguments.
    await autobuild_sportybet_command(update, context)


async def manual_sportybet_command(update, context):
    context.user_data["slip_flow"] = "sportybet_build"
    await reply(update, "📝 MANUAL SPORTYBET KENYA SLIP\nSend one selection per line: Fixture | Market | Selection\n/cancel to stop.")


async def edit_sportybet_command(update, context):
    context.user_data["slip_flow"] = "sportybet_edit_code"
    await reply(update, "Send the SportyBet Kenya booking code you want to edit. /cancel to stop.")


async def merge_sportybet_command(update, context):
    context.user_data["slip_flow"] = "sportybet_merge_codes"
    await reply(update, "Send 2–10 SportyBet Kenya booking codes separated by spaces or new lines. /cancel to stop.")


async def split_sportybet_command(update, context):
    context.user_data["slip_flow"] = "sportybet_split_code"
    await reply(update, "Send the SportyBet Kenya booking code you want to split. /cancel to stop.")


async def trim_sportybet_command(update, context):
    context.user_data["slip_flow"] = "sportybet_trim_code"
    await reply(update, "Send the SportyBet Kenya booking code to trim. /cancel to stop.")


async def cancel_slip_command(update, context):
    context.user_data.pop("slip_flow", None)
    await reply(update, "Cancelled. Your existing slips have not been changed.")


async def my_slips_command(update, context):
    try:
        rows = await asyncio.to_thread(list_slips, update.effective_user.id, 15)
        if not rows:
            await reply(update, "🎟️ No internal slip records are saved yet. Build or manage a SportyBet Kenya booking code to get started.")
            return
        lines = ["🎟️ YOUR INTERNAL SLIP RECORDS", ""]
        for row in rows:
            total = math.prod(float(x["odds"]) for x in row["selections"])
            lines.append(f"#{row['slip_id']} · {len(row['selections'])} legs · {total:.2f} odds · {row['bookmaker']}")
        lines.append("\nUse the Edit, Merge or Split menu actions with these slip IDs.")
        await reply(update, "\n".join(lines))
    except Exception as exc:
        logger.exception("Could not list slips")
        await reply(update, f"Could not load slips: {type(exc).__name__}: {str(exc)[:200]}")


async def ai_natural_language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route ordinary language into existing deterministic PUNTER workflows."""
    message = (update.effective_message.text or "").strip()
    if not message or message.startswith("/"):
        return False
    # A plain "Home vs Away" request is deterministic: route it directly to
    # match research before checking Gemini. This keeps the core fixture workflow
    # available even when Gemini is slow, rate-limited, misconfigured or offline.
    direct_fixture = _plain_fixture_request(message)
    if direct_fixture is not None:
        context.args = direct_fixture["fixture"].split()
        await deepdive_command(update, context)
        return True

    # A target-odds reply must never be sent to Gemini. This also recovers
    # gracefully if Telegram user_data lost the guided slip state between
    # the menu prompt and the user's numeric reply.
    target_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:odds?)?\s*", message, re.I)
    if target_match:
        target = float(target_match.group(1))
        if math.isfinite(target) and target > 1.0:
            context.args = [f"{target:.12g}"]
            context.user_data.pop("slip_flow", None)
            context.user_data.pop("autobuild_target", None)
            context.user_data.pop("autobuild_config", None)
            await autobuild_sportybet_command(update, context)
            return True
        await reply(update, "Target odds must be greater than 1.00.")
        return True

    if not ai_is_configured():
        await reply(update,
            "🧠 AI assistant is not connected yet. The bot's existing commands still work. "
            "To enable natural-language requests, add GEMINI_API_KEY to Railway Variables "
            "and redeploy. Never send the key in Telegram.")
        return True
    await reply(update, "🧠 Understanding your request and connecting it to PUNTER's live tools…")
    try:
        intent = await asyncio.to_thread(interpret_request, message)
        action = intent["action"]
        fixture = intent.get("fixture") or ""
        date = intent.get("date") or "both"
        if action == "deepdive":
            if not fixture:
                await reply(update, "Please name both teams, for example: Analyse Brighton vs Brentford.")
                return True
            context.args = fixture.split()
            await deepdive_command(update, context)
        elif action == "odds":
            if not fixture:
                await reply(update, "Please name both teams, for example: Show odds for Brighton vs Brentford.")
                return True
            context.args = fixture.split() + ([date] if date in ("today", "tomorrow") else [])
            await bookmaker_odds_command(update, context)
        elif action == "fixtures":
            context.args = [] if date == "both" else [date]
            await fixtures(update, context)
        elif action == "topvalue":
            market = str(intent.get("market") or "all").casefold()
            context.args = ([market] if market.casefold() != "all" else [])
            if date in ("today", "tomorrow"):
                context.args.append(date)
            await topvalue_command(update, context)
        elif action == "autobuild":
            target = float(intent.get("target_odds") or 5.0)
            market = str(intent.get("market") or "all")
            context.args = [f"{target:g}"] + ([] if market.casefold() == "all" else [market])
            await autobuild_sportybet_command(update, context)
        elif action == "explain":
            await reply(update, intent.get("explanation") or "I can explain betting terms, but match-specific facts and prices must come from PUNTER's live data tools.")
        else:
            await reply(update,
                "I can understand requests such as:\n"
                "• Analyse a named match\n• Compare SportyBet and Betika prices\n"
                "• Find today's fixtures\n• Scan available markets for potential value\n"
                "• Build a slip preview to a target combined odd\n"
                "• Read a betting-slip screenshot")
    except Exception as exc:
        logger.exception("AI request routing failed")
        await reply(update, f"⚠️ AI request could not be completed: {type(exc).__name__}: {str(exc)[:220]}")
    return True


async def ai_slip_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read a slip screenshot, then pass extracted legs through live SportyBet validation."""
    if not ai_is_configured():
        await reply(update, "Screenshot reading needs GEMINI_API_KEY. Add it privately in Railway Variables, then redeploy.")
        return
    photo = update.effective_message.photo
    if not photo:
        return
    await reply(update, "🖼️ Reading the slip image. I will not trust the screenshot's odds; current SportyBet prices will be checked separately.")
    try:
        telegram_file = await context.bot.get_file(photo[-1].file_id)
        image_bytes = bytes(await telegram_file.download_as_bytearray())
        lines = await asyncio.to_thread(read_slip_image, image_bytes, "image/jpeg")
        context.user_data["ai_ocr_lines"] = lines
        context.user_data["slip_flow"] = "sportybet_build"
        preview = ["🧾 EXTRACTED FOOTBALL SELECTIONS — PLEASE CHECK", ""]
        preview.extend(f"{i}. {line}" for i, line in enumerate(lines, 1))
        preview.extend([
            "", "Check the teams, markets and selections above.",
            "Reply VALIDATE to check every leg against live SportyBet Kenya markets and prices.",
            "Or reply /cancel. No booking code has been created yet."
        ])
        await reply(update, "\n".join(preview))
    except Exception as exc:
        logger.exception("Slip screenshot reading failed")
        await reply(update, f"⚠️ I could not reliably read that screenshot: {type(exc).__name__}: {str(exc)[:220]}")


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle home-menu navigation before treating text as slip input."""
    choice = (update.effective_message.text or "").strip()
    menu_choices = {
        "📅 Today's Matches", "🏆 Top Leagues", "🔎 Analyze Match", "ℹ️ iHelp",
        "🤖 Build a slip", "🧠 AI build slip", "✏️ Edit slip", "🔗 Merge slips", "✂️ Split slip", "🎯 Trim to target odds",
        "📋 Bookies & regions", "🎟️ My slips",
        "⚽ Match intelligence", "📅 Fixtures & leagues", "❓ Contact support",
    }
    # A menu tap must always navigate, even if the user was halfway through
    # entering slip IDs or selection details.
    if choice in menu_choices:
        context.user_data.pop("slip_flow", None)
    elif await process_slip_flow(update, context):
        return
    prompts = {}
    if choice == "📅 Today's Matches":
        context.args = ["today"]
        await fixtures(update, context)
    elif choice == "🏆 Top Leagues":
        context.args = []
        await leagues(update, context)
    elif choice == "🔎 Analyze Match":
        context.user_data["slip_flow"] = "analyze"
        await reply(update, "🔎 ANALYZE A MATCH\n\nSend the fixture as: HOME TEAM vs AWAY TEAM\nExample: Arsenal vs Chelsea\n\nSend /cancel to stop.")
    elif choice == "ℹ️ iHelp":
        await start(update, context)
    elif choice in prompts:
        context.user_data["slip_flow"] = prompts[choice][0]
        await reply(update, prompts[choice][1] + "\n\nSend /cancel to stop.")
    elif choice == "🧠 AI build slip" or choice == "🤖 Build a slip":
        context.args = []
        await autobuild_sportybet_command(update, context)
    elif choice == "📝 Manual slip":
        context.user_data["slip_flow"] = "sportybet_build"
        await reply(update,
            "📝 MANUAL SPORTYBET KENYA SLIP\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Send one selection per line in this format:\n"
            "Fixture | Market | Selection\n\n"
            "I'll verify each selection against SportyBet Kenya live markets and show current odds before generating a code. No bet is placed.\n"
            "Send /cancel to stop.")
    elif choice == "✏️ Edit slip":
        context.user_data["slip_flow"] = "sportybet_edit_code"
        await reply(update, "✏️ SPORTYBET SLIP EDITOR\nSend an existing SportyBet Kenya booking code. I will read its selections and let you remove legs before creating a new code. The original code stays unchanged.\n/cancel to stop.")
    elif choice == "🔗 Merge slips":
        context.user_data["slip_flow"] = "sportybet_merge_codes"
        await reply(update, "🔗 MERGE SPORTYBET SLIPS\nSend 2–10 SportyBet Kenya booking codes, separated by spaces or new lines. I will combine their unique selections and preview the result before generating a new code.\n/cancel to stop.")
    elif choice == "✂️ Split slip":
        context.user_data["slip_flow"] = "sportybet_split_code"
        await reply(update, "✂️ SPLIT SPORTYBET SLIP\nSend an existing SportyBet Kenya booking code. Then choose how many selections should go into each new slip.\n/cancel to stop.")
    elif choice == "🎯 Trim to target odds":
        context.user_data["slip_flow"] = "sportybet_trim_code"
        await reply(update, "🎯 TRIM SPORTYBET SLIP\nSend an existing SportyBet Kenya booking code. Then enter your target maximum combined odds. The system removes the highest-odds legs first and previews the result.\n/cancel to stop.")
    elif choice == "📋 Bookies & regions":
        await bookmakers_command(update, context)
    elif choice == "⚽ Match intelligence":
        await fullintel_command(update, context)
    elif choice == "📅 Fixtures & leagues":
        await reply(update,
            "📅 FIXTURES & LEAGUES\n\n"
            "/fixtures — browse upcoming fixtures\n"
            "/leagues SEARCH — find a competition\n"
            "/leaguefixtures LEAGUE [today|tomorrow] — league fixtures\n"
            "/syncleagues — refresh the catalogue")
    elif choice == "❓ Contact support":
        await reply(update,
            "❓ PUNTER KENYA SUPPORT\n\n"
            "For technical issues, use: https://github.com/Carti254/punter-kenya2\n"
            "Never send bookmaker passwords, account credentials or private tokens.")
    elif choice == "/myslips":
        await my_slips_command(update, context)
    elif choice == "🎟️ My slips":
        await my_slips_command(update, context)
    else:
        await ai_natural_language_handler(update, context)



FULLINTEL_MENUS = {
    "fixtures": ("📅 FIXTURES & LEAGUES", [
        [("Upcoming fixtures", "fixtures_list"), ("Today's fixtures", "fixtures_today")],
        [("Tomorrow's fixtures", "fixtures_tomorrow"), ("League catalogue", "leagues")],
        [("Sync leagues", "syncleagues"), ("Sync fixtures", "syncfixtures")],
    ]),
    "teams": ("📊 TEAM INTELLIGENCE", [
        [("Recent form", "form"), ("Goals analysis", "goals")],
        [("Home / away", "homeaway"), ("Team strength", "strength")],
        [("Half-time stats", "halves")],
    ]),
    "matchup": ("⚔️ MATCH INTELLIGENCE", [
        [("Head-to-head", "h2h"), ("Matchup analysis", "matchup")],
        [("Match scenarios", "scenarios"), ("Risk analysis", "risk")],
        [("Full match deepdive", "deepdive"), ("Match forecast", "matchintel")],
        [("Market probabilities", "markets")],
    ]),
    "betting": ("🧠 BETTING & VALUE", [
        [("Value check", "value"), ("Top value status", "topvalue")],
        [("AI picks status", "aipicks"), ("Accumulator guide", "accumulator")],
    ]),
    "bookies": ("💹 BOOKMAKER CENTRE", [
        [("Check bookmaker feeds", "bookies")],
        [("Retrieve match odds", "odds_help"), ("Odds history", "oddshistory_help")],
    ]),
    "tracking": ("📚 TRACKING & TOOLS", [
        [("Forecast performance", "record"), ("Odds calculator", "calculator_help")],
        [("Live-data status", "live"), ("About PUNTER", "about")],
    ]),
}

COMMAND_HELP = {
    "form": "Send: /form HOME TEAM vs AWAY TEAM",
    "goals": "Send: /goals HOME TEAM vs AWAY TEAM",
    "homeaway": "Send: /homeaway HOME TEAM vs AWAY TEAM",
    "strength": "Send: /strength HOME TEAM vs AWAY TEAM",
    "halves": "Send: /halves HOME TEAM vs AWAY TEAM",
    "h2h": "Send: /h2h HOME TEAM vs AWAY TEAM",
    "matchup": "Send: /matchup HOME TEAM vs AWAY TEAM",
    "scenarios": "Send: /scenarios HOME TEAM vs AWAY TEAM",
    "risk": "Send: /risk HOME TEAM vs AWAY TEAM",
    "deepdive": "Send: /deepdive HOME TEAM vs AWAY TEAM",
    "matchintel": "Send: /matchintel HOME TEAM vs AWAY TEAM",
    "markets": "Send: /markets HOME TEAM vs AWAY TEAM",
    "value": "Send: /value HOME TEAM vs AWAY TEAM HOME_ODDS DRAW_ODDS AWAY_ODDS",
    "odds_help": "Send: /odds HOME TEAM vs AWAY TEAM [today|tomorrow]",
    "oddshistory_help": "Send: /oddshistory HOME TEAM vs AWAY TEAM",
    "calculator_help": "Send: /calculator DECIMAL_ODDS STAKE",
    "aipicks": "Run /aipicks [home|draw|away|over15|over25|safest] [today|tomorrow]. Scans fixtures against retrieved bookmaker prices.",
    "accumulator": "Run /accumulator [target decimal odds]. Builds a cautious experimental combination only from selections passing scanner filters; it may return NO BET.",
}

def fullintel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Fixtures & Leagues", callback_data="fi:fixtures"),
         InlineKeyboardButton("📊 Team Intelligence", callback_data="fi:teams")],
        [InlineKeyboardButton("⚔️ Match Intelligence", callback_data="fi:matchup"),
         InlineKeyboardButton("🧠 Betting & Value", callback_data="fi:betting")],
        [InlineKeyboardButton("💹 Bookmaker Centre", callback_data="fi:bookies"),
         InlineKeyboardButton("📚 Tracking & Tools", callback_data="fi:tracking")],
    ])

def fullintel_submenu_keyboard(key):
    title, rows = FULLINTEL_MENUS[key]
    keyboard = [[InlineKeyboardButton(label, callback_data=f"fi:{command}") for label, command in row] for row in rows]
    keyboard.append([InlineKeyboardButton("⬅️ Main menu", callback_data="fi:home")])
    return InlineKeyboardMarkup(keyboard)

async def fullintel_command(update, context):
    await update.effective_message.reply_text(
        "⚽ PUNTER KENYA — INTELLIGENCE CENTRE\n\nChoose a section. Each section opens its own commands.",
        reply_markup=fullintel_keyboard(),
    )

async def aipicks_status_command(update, context):
    """Run the same transparent fixture-wide statistical and price scan."""
    await topvalue_command(update, context)


def enrich_with_specialist_models(rows):
    """Run specialist models plus X and cross-bookmaker checks for every candidate fixture."""
    rows, specialist = enrich_specialist_candidates(rows, max_fixtures=3)
    rows = list(rows or [])
    stats = {
        "fixtures_checked": int(specialist.get("fixtures_checked") or 0),
        "matched_prices": int(specialist.get("matched_prices") or 0),
        "specialist_panels": 0, "cross_bookmaker_matches": 0,
        "x_posts": 0, "x_fixtures_checked": 0, "odds_snapshots_saved": 0,
        "reason": specialist.get("reason") or "",
    }
    if not rows:
        return rows, stats

    games_by_day = {}
    for day_text in sorted({str(row.get("day") or "")[:10] for row in rows if row.get("day")}):
        try:
            day_value = datetime.strptime(day_text, "%Y-%m-%d").date()
            result = fetch_bookmaker_odds(day_value)
            games = []
            for provider in (result.get("bookmakers") or {}).values():
                games.extend(provider.get("games") or [])
            games_by_day[day_text] = games
            try:
                stats["odds_snapshots_saved"] += save_price_observations(games)
            except Exception as exc:
                logger.warning("Autobuild odds snapshots could not be stored: %s", exc)
        except Exception as exc:
            logger.warning("Autobuild bookmaker comparison unavailable for %s: %s", day_text, exc)
            games_by_day[day_text] = []

    fixture_groups = {}
    for row in rows:
        key = (str(row.get("day") or "")[:10], str(row.get("home") or ""), str(row.get("away") or ""))
        fixture_groups.setdefault(key, []).append(row)

    x_token = os.getenv("X_BEARER_TOKEN")
    for group_index, ((day_text, home, away), candidates) in enumerate(fixture_groups.items()):
        games = games_by_day.get(day_text, [])
        for candidate in candidates:
            wanted_market = _normal_market_text(candidate.get("market"))
            wanted_selection = _normal_market_text(candidate.get("selection"))
            alternatives = []
            for game in games:
                if str(game.get("bookmaker") or "").casefold() == "sportybet":
                    continue
                if not find_bookmaker_event([game], home, away):
                    continue
                for market in game.get("markets") or []:
                    if (_normal_market_text(market.get("market")) == wanted_market
                            and _normal_market_text(market.get("selection")) == wanted_selection):
                        try:
                            price = float(market.get("odds"))
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(price) and price > 1:
                            alternatives.append({"bookmaker": game.get("bookmaker") or "Unknown", "odds": price})
            if alternatives:
                best = max(alternatives, key=lambda item: item["odds"])
                candidate["other_bookmaker_prices"] = alternatives
                candidate["best_other_bookmaker"] = best
                candidate["cross_bookmaker_edge"] = best["odds"] / float(candidate["odds"]) - 1.0
                candidate["other_bookmaker_evidence"] = True
                stats["cross_bookmaker_matches"] += 1

        if x_token and group_index < 12:
            try:
                from x_research import search_match_posts
                report = search_match_posts(home, away, limit=12, token=x_token)
                posts = [{
                    "username": post.get("username"),
                    "created_at": post.get("created_at"),
                    "verified_account": bool(post.get("verified")),
                    "text": str(post.get("text") or "")[:350],
                    "url": post.get("url"),
                    "warning": "Unverified public-post lead; not confirmed team news.",
                } for post in (report.get("posts") or [])[:8]]
                stats["x_fixtures_checked"] += 1
                stats["x_posts"] += int(report.get("result_count") or 0)
                for candidate in candidates:
                    candidate["x_research_evidence"] = posts
                    candidate["x_research_warning"] = report.get("warning")
                    # Keep API-Football injury/suspension evidence intact.
                    # Public X posts are leads only and must never overwrite verified provider data.
            except Exception as exc:
                logger.info("X research unavailable for %s vs %s: %s", home, away, exc)

    try:
        rows, price_history_stats = enrich_candidates_with_price_history(rows)
        stats["price_history"] = price_history_stats
    except Exception as history_error:
        logger.info("Autobuild historical price enrichment unavailable: %s", history_error)
        stats["price_history"] = {"reason": f"Price history unavailable: {type(history_error).__name__}"}

    gaps = []
    if not specialist.get("enabled"):
        gaps.append("API-Football specialist research not configured")
    if not x_token:
        gaps.append("X public-post research not configured")
    if not stats["cross_bookmaker_matches"]:
        gaps.append("no exact cross-bookmaker market/selection match")
    if gaps:
        stats["reason"] = (stats["reason"] + " " if stats["reason"] else "") + "; ".join(gaps)
    else:
        stats["reason"] = "Specialist, X and cross-bookmaker sources checked; social posts remain unverified leads."
    return rows, stats


def _format_autobuild_research_report(legs):
    """Render the evidence actually attached to the selected candidate rows."""
    lines = ["📊 PUNTER — SELECTION RESEARCH EVIDENCE", "Each figure below is a model/provider output, not a guarantee.", ""]
    for index, leg in enumerate(legs or [], 1):
        evidence = leg.get("research_evidence") or {}
        checks = leg.get("research_checks") or []
        gaps = leg.get("research_gaps") or []
        odds = float(leg.get("odds") or 0)
        probability = float(leg.get("probability") or 0)
        implied = float(leg.get("implied_probability") or (1 / odds if odds > 1 else 0))
        lines.extend([
            f"{index}. {leg.get('fixture') or (str(leg.get('home') or '') + ' vs ' + str(leg.get('away') or ''))}",
            f"Competition: {leg.get('league') or 'Not supplied'} | Kick-off: {leg.get('kickoff') or 'Not supplied'}",
            f"Market: {leg.get('market') or 'Not supplied'} — {leg.get('selection') or 'Not supplied'} @ {odds:.2f}",
            f"Model estimate: {probability:.1%} | Odds-implied: {implied:.1%} | Edge: {float(leg.get('edge') or 0):+.1%} | Estimated EV: {float(leg.get('ev') or 0):+.1%}",
            f"Evidence grade: {leg.get('research_grade') or 'Unassigned'} — {leg.get('research_grade_reason') or 'No grade explanation'}",
            f"Model confidence/source: {leg.get('model_confidence') or 'Not supplied'} / {leg.get('probability_source') or 'Not supplied'}",
            f"Recent form: {evidence.get('home_form') or 'Unavailable'} vs {evidence.get('away_form') or 'Unavailable'}",
            f"Recent sample: {evidence.get('home_sample') or 0} home-team matches; {evidence.get('away_sample') or 0} away-team matches",
            f"Goals per match: home team {evidence.get('home_scored_avg') if evidence.get('home_scored_avg') is not None else 'N/A'} scored / {evidence.get('home_conceded_avg') if evidence.get('home_conceded_avg') is not None else 'N/A'} conceded; away team {evidence.get('away_scored_avg') if evidence.get('away_scored_avg') is not None else 'N/A'} scored / {evidence.get('away_conceded_avg') if evidence.get('away_conceded_avg') is not None else 'N/A'} conceded",
            f"Venue splits: home sample {evidence.get('home_home_sample') or 0} (GF {evidence.get('home_home_gf') if evidence.get('home_home_gf') is not None else 'N/A'}, GA {evidence.get('home_home_ga') if evidence.get('home_home_ga') is not None else 'N/A'}); away sample {evidence.get('away_away_sample') or 0} (GF {evidence.get('away_away_gf') if evidence.get('away_away_gf') is not None else 'N/A'}, GA {evidence.get('away_away_ga') if evidence.get('away_away_ga') is not None else 'N/A'})",
            f"H2H: {evidence.get('h2h_count') or 0} same-competition meetings",
            f"Expected goals: home {evidence.get('expected_home_goals') if evidence.get('expected_home_goals') is not None else 'N/A'}; away {evidence.get('expected_away_goals') if evidence.get('expected_away_goals') is not None else 'N/A'}",
            f"Elo ratings: home {evidence.get('elo_home_rating') if evidence.get('elo_home_rating') is not None else 'N/A'}; away {evidence.get('elo_away_rating') if evidence.get('elo_away_rating') is not None else 'N/A'}; matches used {evidence.get('elo_matches_used') or 'N/A'}",
            f"Monte Carlo cross-check: {evidence.get('monte_carlo_selected_probability') if evidence.get('monte_carlo_selected_probability') is not None else 'Not available'}",
            f"Standings evidence: {evidence.get('standings_evidence') or 'Not available'}",
            f"Early-goal evidence: {evidence.get('early_goal_evidence') or 'Not available'}",
            f"Specialist market evidence: {evidence.get('specialist_evidence') or 'Not available'}",
            f"Team news / lineup evidence: {leg.get('team_news_evidence') or leg.get('lineup_evidence') or 'Not available'}",
            f"Evidence providers recorded: {', '.join(leg.get('external_evidence_sources') or []) or 'No external provider recorded'}",
            f"Fixture/source link: {leg.get('url') or 'Not supplied'}",
        ])
        if checks:
            lines.append("Checks actually completed: " + "; ".join(str(x) for x in checks))
        else:
            lines.append("Checks actually completed: none recorded")
        if gaps:
            lines.append("Evidence NOT available: " + "; ".join(str(x) for x in gaps))
        else:
            lines.append("Evidence gaps: none recorded by the current assessment")
        lines.append("Recent home-team results: " + (", ".join(str(x) for x in (evidence.get("home_recent_matches") or [])[:5]) or "Not supplied"))
        lines.append("Recent away-team results: " + (", ".join(str(x) for x in (evidence.get("away_recent_matches") or [])[:5]) or "Not supplied"))
        lines.append("")
    lines.append("Important: PUNTER's probability and EV are model estimates, not independently proven winning chances. Missing evidence is shown as unavailable; it is never treated as verified.")
    return "\n".join(lines)


async def autobuild_sportybet_command(update, context):
    message_text = str(getattr(update.effective_message, "text", "") or "").strip()
    message_parts = message_text.split(maxsplit=1)
    raw_tail = message_parts[1].strip() if message_text.startswith("/") and len(message_parts) > 1 else ""
    args = raw_tail.split() if raw_tail else list(context.args or [])
    guided = context.user_data.pop("autobuild_config", None)
    if guided:
        args = []
    if not guided and not args:
        context.user_data["slip_flow"] = "autobuild_target"
        await reply(update, "🎟️ BUILD YOUR SPORTYBET SLIP\n\nWhat target combined odds do you want?\nExample: 2, 5, 10, 20 or 50.\n/cancel to stop.")
        return
    if guided:
        try:
            target = float(guided["target"])
        except (KeyError, TypeError, ValueError, OverflowError):
            await reply(update, "Invalid saved target. Enter your target combined odds again.")
            return
    else:
        if len(args) != 1 or not re.fullmatch(r"\d+(?:\.\d+)?(?:odds)?", str(args[0]), re.I):
            await reply(update, "Enter only your target combined odds, for example: /build 2 or /build 50. PUNTER chooses the markets and number of legs automatically.")
            return
        target = float(re.sub(r"odds$", "", str(args[0]), flags=re.I))

    if not math.isfinite(target) or not 1.0 < target <= 1000.0:
        await reply(update, "Target odds must be greater than 1.00 and no higher than 1,000.")
        return

    # The target changes only the accumulator search objective. Every target
    # uses the same research pipeline and the same 2–20 distinct-fixture bounds.
    # Do not force five legs for larger targets; let the odds/probability search
    # determine the smallest qualifying combination without weakening evidence.
    min_legs = 2
    max_legs = 20
    market_filter = "all"
    await reply(update, f"🔎 Finding matches for target odds {target:.2f}…")
    try:
        # Keep each accumulator strictly within the current EAT calendar day.
        # Search today's fixtures only; never mix tomorrow's matches into this slip.
        scan_days = ("today",)
        rows, diagnostics = await asyncio.wait_for(
            asyncio.to_thread(
                scan_value_candidates, scan_days, market_filter, "SportyBet",
            ),
            timeout=180,
        )
        scanned_count = len(rows)

        # User preference: exclude the Big-Five domestic top divisions.
        # Cup competitions remain eligible; the filter is country + competition aware.
        big_five_top_flights = {
            "england": ("premier league",),
            "spain": ("laliga", "la liga", "primera division"),
            "germany": ("bundesliga",),
            "italy": ("serie a",),
            "france": ("ligue 1", "ligue1"),
        }
        country_aliases = {
            "united kingdom": "england", "england": "england",
            "españa": "spain", "spain": "spain",
            "deutschland": "germany", "germany": "germany",
            "italia": "italy", "italy": "italy", "france": "france",
        }
        def is_big_five_top_flight(row):
            country = country_aliases.get(str(row.get("country") or "").casefold(),
                                          str(row.get("country") or "").casefold())
            league = str(row.get("league") or "").casefold()
            terms = big_five_top_flights.get(country)
            if terms:
                return any(term in league for term in terms)
            # Country-less records: exclude only unmistakable labels.
            return any(term in league for term in (
                "english premier league", "la liga", "laliga", "bundesliga",
                "italian serie a", "ligue 1",
            ))
        before_big_five_filter = len(rows)
        rows = [row for row in rows if not is_big_five_top_flight(row)]
        big_five_excluded = before_big_five_filter - len(rows)

        # Keep the live builder's critical path short: run the capped specialist
        # match panels, but do not launch secondary X searches or a second full
        # bookmaker-feed download for every build. Those optional sources must not
        # prevent the primary SportyBet research scan from returning.
        specialist_stats = {"reason": "Specialist research not configured or unavailable."}
        price_history_stats = {"matched": 0, "reason": "Stored odds history not checked."}
        try:
            rows, specialist_stats = await asyncio.wait_for(
                asyncio.to_thread(enrich_specialist_candidates, rows, max_fixtures=20),
                timeout=45,
            )
        except Exception as enrichment_error:
            logger.warning("Autobuild specialist enrichment failed: %s", enrichment_error)
            specialist_stats = {
                "reason": f"Specialist enrichment unavailable: {type(enrichment_error).__name__}: {str(enrichment_error)[:120]}"
            }
        # Odds-history lookup is supplementary; failure must not block a live scan.
        try:
            original_rows = rows
            enriched_rows, price_history_stats = await asyncio.wait_for(
                asyncio.to_thread(enrich_candidates_with_price_history, original_rows[:100]),
                timeout=8,
            )
            rows = list(enriched_rows or []) + original_rows[100:]
        except Exception as history_error:
            logger.warning("Autobuild price-history enrichment unavailable: %s", history_error)
            price_history_stats = {"matched": 0, "reason": f"Stored odds history unavailable: {type(history_error).__name__}"}

        # Never reconstruct the entire historical dataset during a live slip request.
        # Use only a recent, already-computed walk-forward result; otherwise leave
        # probabilities uncalibrated and label them experimental in the report.
        calibration_stats = {"applied": False, "reason": "No recent cached walk-forward validation; live scan skipped expensive recalculation."}
        cached_validation = globals().get("_WALK_FORWARD_CALIBRATION_CACHE", {}).get("result")
        cached_at = globals().get("_WALK_FORWARD_CALIBRATION_CACHE", {}).get("timestamp", 0)
        if cached_validation and time.time() - cached_at < 21600:
            rows, calibration_stats = apply_walk_forward_calibration(rows, cached_validation)

        # Same evidence/value gate as /accumulator: minimum recent and venue samples,
        # LOW confidence rejected, BTTS/Under 2.5/2UP excluded, and at least 3% EV.
        rows, research_stats = await asyncio.to_thread(
            prepare_candidate_pool, rows, marginal_ev_floor=0.03, use_dixon_coles=True
        )
        if not rows:
            context.user_data.pop("pending_sportybet_booking", None)
            note = "; ".join(str(x) for x in diagnostics[:4])
            await reply(
                update,
                "🚫 NO QUALIFYING SPORTYBET SLIP\n"
                f"Target: {target:.2f}\nMarkets scanned: {scanned_count}\n"
                f"Research/value-qualified selections: 0\n"
                f"Big-Five top-flight fixtures excluded: {big_five_excluded}\n"
                f"Rejected by research/value screen: {research_stats.get('rejected', 0)}\n"
                "PUNTER will not pad the slip with weak, unmodelled or negative-value selections.\n"
                f"Specialist research: {specialist_stats.get('reason', 'not available')}\n"
                + (f"Feed notes: {note}" if note else "")
            )
            return

        # Research every eligible row first; the target is used only by the
        # accumulator search. Count fixtures, not market rows, before building.
        qualified_fixture_keys = {
            (str(x.get("day") or x.get("match_date") or "")[:10],
             canonical_team_key(x.get("home")), canonical_team_key(x.get("away")))
            for x in rows if x.get("home") and x.get("away")
        }
        if len(qualified_fixture_keys) < 2:
            context.user_data.pop("pending_sportybet_booking", None)
            rejection_breakdown = research_stats.get("rejection_reasons") or {}
            rejection_text = ", ".join(
                f"{reason}={count}" for reason, count in rejection_breakdown.items()
            ) or "No rejection counts recorded"
            await reply(
                update,
                f"🚫 NO QUALIFYING SLIP — {target:.2f} TARGET\n"
                f"Research-qualified selections: {len(rows)} across {len(qualified_fixture_keys)} distinct fixtures.\n"
                f"Research grades: A={research_stats.get('research_grade_a', 0)}, "
                f"B={research_stats.get('research_grade_b', 0)} "
                f"(B selections have incomplete venue-specific evidence).\n"
                f"Rejected rows: {research_stats.get('rejected', 0)}. "
                f"Rejection breakdown: {rejection_text}.\n"
                "At least two different matches are required. Multiple markets from one match "
                "cannot count as separate accumulator legs. No weak selections were added.\n"
                + ("Scanner diagnostics: " + " | ".join(str(x)[:220] for x in diagnostics[-5:])
                   if diagnostics else "Scanner diagnostics: unavailable.")
            )
            return

        build = build_accumulator(
            rows, target, min_legs=min_legs, max_legs=max_legs,
            ranking="value",
            # Keep a broader set of researched markets available to the
            # combination search; do not narrow the candidate pool prematurely.
            max_candidates=500,
            beam_width=1200,
        )
        chosen = build["selections"]
        if not chosen or not build.get("target_reached"):
            context.user_data.pop("pending_sportybet_booking", None)
            fixture_count = len({(x.get("day"), x.get("home"), x.get("away")) for x in rows})
            await reply(
                update,
                f"🚫 NO QUALIFYING SLIP — {target:.2f} TARGET\n"
                f"Research-qualified selections: {len(rows)} across {fixture_count} fixtures.\n"
                f"Research rejection breakdown: {', '.join(f'{k}={v}' for k, v in (research_stats.get('rejection_reasons') or {}).items()) or 'not available'}.\n"
                "No combination reached the target within the permitted 20% range. "
                "No lower-odds substitute or booking code was created."
            )
            return

        resolved = await asyncio.to_thread(prepare_sportybet_candidates, chosen)
        resolved_fixture_keys = {
            (str(x.get("day") or x.get("match_date") or "")[:10],
             canonical_team_key(x.get("home") or x.get("fixture", "").split(" vs ")[0]),
             canonical_team_key(x.get("away") or x.get("fixture", "").split(" vs ")[-1]))
            for x in resolved
        }
        if len(resolved) != len(chosen) or len(resolved_fixture_keys) != len(chosen) or len(chosen) < 2:
            context.user_data.pop("pending_sportybet_booking", None)
            await reply(
                update,
                f"🚫 SLIP VALIDATION FAILED\nPUNTER selected {len(chosen)} legs and SportyBet "
                f"resolved {len(resolved)}; distinct resolved fixtures: {len(resolved_fixture_keys)}. "
                "No partial slip or booking code was created."
            )
            return

        protected_keys = {
            "eventId", "marketId", "specifier", "outcomeId",
            "fixture", "market", "selection", "odds", "startTime",
        }
        for live_leg, model_leg in zip(resolved, chosen):
            live_leg.update({key: value for key, value in model_leg.items() if key not in protected_keys})

        combined = math.prod(float(x["odds"]) for x in resolved)
        if not (target <= combined <= target * 1.20):
            context.user_data.pop("pending_sportybet_booking", None)
            await reply(
                update,
                f"🚫 LIVE PRICE VALIDATION FAILED\nThe revalidated combined odds are {combined:.2f}, "
                f"outside the permitted range for {target:.2f}. Rebuild the slip; no partial substitute."
            )
            return

        combined_probability = float(
            build.get("combined_probability") or
            math.prod(float(x["probability"]) for x in resolved)
        )
        naive_probability = float(
            build.get("naive_independent_probability") or combined_probability
        )
        correlation_factor = float(build.get("correlation_adjustment_factor") or 1.0)
        context.user_data["pending_sportybet_booking"] = resolved
        # Keep the exact assessed rows alongside the clean preview so the user can
        # inspect the source evidence for every selected leg before creating a code.
        context.user_data["pending_sportybet_research"] = [dict(x) for x in resolved]

        # Research remains active internally; Telegram shows only the match and selection.
        lines = [
            f"🎟️ SPORTYBET PARLAY — {target:.2f} TARGET",
            f"Combined odds: {combined:.2f}",
            f"Selections: {len(resolved)}",
            "",
        ]
        for i, leg in enumerate(resolved, 1):
            lines.append(
                f"{i}. {leg['fixture']} — {leg['market']}: "
                f"{leg['selection']} @ {leg['odds']:.2f}"
            )

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Show research evidence", callback_data="sb:proof")],
            [InlineKeyboardButton("Generate SportyBet code", callback_data="sb:confirm"),
             InlineKeyboardButton("Cancel", callback_data="sb:cancel")],
        ])
        # Split long high-leg-count reports into Telegram-safe messages.
        chunks, current = [], ""
        for line in lines:
            addition = line + "\n"
            if current and len(current) + len(addition) > 3500:
                chunks.append(current.rstrip())
                current = ""
            current += addition
        if current.strip():
            chunks.append(current.rstrip())
        for index, chunk in enumerate(chunks):
            await update.effective_message.reply_text(
                chunk,
                reply_markup=markup if index == len(chunks) - 1 else None,
            )
    except Exception as exc:
        logger.exception("SportyBet auto-builder failed")
        context.user_data.pop("pending_sportybet_booking", None)
        await reply(update, f"SportyBet scan failed: {type(exc).__name__}: {str(exc)[:180]}")

async def accumulator_status_command(update, context):
    """Build a cautious combination only from scanner-qualified selections."""
    args = [str(x).strip() for x in (context.args or [])]
    target = 3.0
    if args:
        try:
            target = float(args[0])
            if not 1.50 <= target <= 50:
                raise ValueError
        except ValueError:
            await reply(update, "Usage: /accumulator [target decimal odds, 1.50–50]. Example: /accumulator 3.00")
            return

    try:
        rows, diagnostics = await asyncio.to_thread(scan_value_candidates, ("today", "tomorrow"), "all")
        # Keep this command on the same research/value gate as the SportyBet
        # auto-builder. The old implementation selected the six highest raw
        # probabilities, bypassed the 3% EV screen and hard-coded six legs.
        rows, research_stats = prepare_candidate_pool(rows, marginal_ev_floor=0.03)
    except Exception as exc:
        logger.exception("Accumulator scan failed")
        await reply(update, f"❌ ACCUMULATOR SCAN FAILED: {type(exc).__name__}: {str(exc)[:250]}")
        return

    fixture_count = len({
        (str(x.get("day") or "")[:10], canonical_team_key(x.get("home")), canonical_team_key(x.get("away")))
        for x in rows if x.get("home") and x.get("away")
    })
    if not rows or fixture_count == 0:
        await reply(update, "⚠️ NO QUALIFIED ACCUMULATOR CANDIDATES\n"
                    + ("Feed notes: " + "; ".join(diagnostics[:5]) if diagnostics else "No model-supported bookmaker prices were retrieved.")
                    + f"\nResearch screen: {research_stats.get('rejected', 0)} rejected; {research_stats.get('eligible', 0)} eligible.")
        return

    # The engine chooses the number of legs automatically from distinct fixtures.
    build = build_accumulator(rows, target, max_legs=20, ranking="value")
    chosen, combined = build["selections"], build["combined_odds"]
    if not chosen or not build.get("target_reached"):
        await reply(update, f"NO BET: no qualifying combination reached your {target:.2f} target within the 20% tolerance. "
                    "No partial slip has been presented as a completed accumulator. "
                    f"Qualified fixtures: {fixture_count}; eligible markets: {len(rows)}.")
        return

    probability_product = float(build.get("combined_probability") or math.prod(item["probability"] for item in chosen))
    naive_probability_product = math.prod(item["probability"] for item in chosen)
    lines = [
        "🧮 PUNTER KENYA — EXPERIMENTAL ACCUMULATOR",
        f"Target: {target:.2f} | Combined decimal odds: {combined:.2f} | TARGET REACHED",
        f"Automatic leg count: {len(chosen)} | Qualified fixtures: {fixture_count}",
        f"Research screen: {research_stats['eligible']} eligible; {research_stats['rejected']} rejected",
        "",
    ]
    for number, item in enumerate(chosen, 1):
        lines.extend([
            f"{number}. {item['home']} vs {item['away']} — {item.get('league') or 'Competition'}",
            f"   {item['market']}: {item['selection']} @ {item['odds']:.2f} ({item.get('bookmaker') or 'Unknown'})",
            f"   Model probability: {item['probability']:.1%} | Estimated EV: {item.get('ev', 0):+.1%}",
            f"   Evidence gaps: {', '.join(item.get('research_gaps') or []) or 'None reported'}",
            *([f"   Confirmed lineups: {', '.join(x.get('team','') for x in (item.get('lineup_evidence') or {}).get('sides', []))}."]
              if (item.get("lineup_evidence") or {}).get("sides") else []),
            *([f"   Reported absences: {len((item.get('team_news_evidence') or {}).get('reported_absences', []))} (API-Football)."]
              if item.get("team_news_evidence") else []),
            *([f"   xG sample: home {item['xg_evidence'].get('home_sample')} / away {item['xg_evidence'].get('away_sample')}."]
              if item.get("xg_evidence") else []),
            *([f"   Tactical profile: {((item.get('tactical_evidence') or {}).get('home') or {}).get('descriptive_style','not available')}; away {((item.get('tactical_evidence') or {}).get('away') or {}).get('descriptive_style','not available')}."]
              if item.get("tactical_evidence") else []),
            *([f"   Odds movement: {item['odds_history_evidence'].get('movement_direction')} ({item['odds_history_evidence'].get('movement_fraction',0):+.1%}, {item['odds_history_evidence'].get('observations')} snapshots)."]
              if (item.get('odds_history_evidence') or {}).get('status') == 'observed' else []),
            *([f"   Best other bookmaker: {item['best_other_bookmaker'].get('bookmaker')} @ {item['best_other_bookmaker'].get('odds'):.2f}."]
              if item.get("best_other_bookmaker") else []),
            *([f"   Gemini review: {item['ai_research_review'].get('assessment','')[:180]}"]
              if item.get("ai_research_review") else []),
            *([f"   Calibration: temperature {item['calibration_evidence'].get('temperature'):.2f}; holdout n={item['calibration_evidence'].get('holdout')}."]
              if item.get("calibration_evidence") else []),
        ])
    lines.extend([
        "",
        f"Naive independent-leg probability product: {naive_probability_product:.2%}",
        f"Dependence-adjusted model estimate: {probability_product:.2%} (factor {build.get('correlation_adjustment_factor', 1.0):.3f}).",
        "Same-competition/date dependence uses a conservative heuristic haircut, not measured pairwise correlation; this is not a calibrated accumulator probability.",
    ])
    if build.get("correlation_warnings"):
        lines.append("Correlation warning: " + "; ".join(build["correlation_warnings"]))
    lines.append("Experimental model estimates only. Confirm every price and settlement rule. No BTTS or Under 2.5.")
    await reply(update, "\n".join(lines))


async def fullintel_callback(update, context):
    query = update.callback_query
    await query.answer()
    key = (query.data or "").split(":", 1)[-1]
    if key == "home":
        await query.edit_message_text(
            "⚽ PUNTER KENYA — INTELLIGENCE CENTRE\n\nChoose a section.",
            reply_markup=fullintel_keyboard(),
        )
        return
    if key in FULLINTEL_MENUS:
        title, _ = FULLINTEL_MENUS[key]
        await query.edit_message_text(title + "\n\nChoose a command:", reply_markup=fullintel_submenu_keyboard(key))
        return
    if key in ("fixtures_today", "fixtures_tomorrow", "fixtures_list"):
        command = "/fixtures today" if key.endswith("today") else "/fixtures tomorrow" if key.endswith("tomorrow") else "/fixtures"
        await query.edit_message_text("Run this command in the chat:\n" + command, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main menu", callback_data="fi:home")]]))
        return
    if key in ("leagues", "syncleagues", "syncfixtures", "bookies", "record", "live", "about", "topvalue"):
        await query.edit_message_text("Run this command in the chat:\n/" + key, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main menu", callback_data="fi:home")]]))
        return
    if key in COMMAND_HELP:
        await query.edit_message_text(COMMAND_HELP[key], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main menu", callback_data="fi:home")]]))
        return
    await query.edit_message_text("Command not available.", reply_markup=fullintel_keyboard())



async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await start(update, context)


async def about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Display data-provider attribution and project information."""
    await reply(
        update,
        "ℹ️ ABOUT PUNTER KENYA\n\n"
        "Fixture data provider: open-matchday\n"
        "Repository: https://github.com/oladeeayo/open-matchday\n\n"
        "PUNTER KENYA is an independent project and is not affiliated with the data provider.",
    )


# ============================================================
# DATABASE TEST
# ============================================================

async def dbtest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "🔄 Testing database connection...",
    )

    try:

        result = await asyncio.to_thread(
            test_connection
        )

        await reply(
            update,
            "✅ DATABASE CONNECTION SUCCESSFUL\n\n"
            f"Database: {result.get('database_name', 'unknown')}\n"
            f"Server: {result.get('database_version', 'unknown')}"
        )

    except Exception as exc:

        logger.exception("Database test failed")

        await reply(
            update,
            f"❌ DATABASE CONNECTION FAILED\n\n{str(exc)[:700]}",
        )


# ============================================================
# GOAL API DIAGNOSTICS
# ============================================================

async def diagnose(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "🧪 Running GOAL API diagnostics: "
        "leagues, fixtures and live matches...",
    )

    tests = [
        (
            "LEAGUES",
            "leagues",
            {"limit": 5, "offset": 0},
            extract_leagues,
        ),
        (
            "FIXTURES",
            "fixtures",
            {"limit": 5, "offset": 0},
            extract_fixtures,
        ),
        (
            "LIVE",
            "fixtures/live",
            {},
            extract_fixtures,
        ),
    ]

    for label, endpoint, params, extractor in tests:

        try:

            payload = await asyncio.to_thread(
                goal_request,
                endpoint,
                params,
            )

            rows = extractor(payload)
            meta = response_metadata(payload)

            message = (
                f"✅ {label} ENDPOINT\n"
                f"Endpoint: /{endpoint}\n"
                f"Records detected: {len(rows)}\n"
                f"Pagination metadata: "
                f"{str(meta)[:500] if meta else 'not supplied'}"
            )

            if not rows and isinstance(payload, dict):

                message += (
                    "\nTop-level response keys: "
                    + ", ".join(map(str, payload.keys()))
                )

            if label == "LEAGUES" and rows:

                samples = []

                for raw in rows[:5]:

                    league = normalise_league(raw)

                    if league:

                        samples.append(
                            f"• {league['name']} — "
                            f"{league['country']} "
                            f"(ID: {league['id'] or 'not supplied'})"
                        )

                if samples:

                    message += (
                        "\n\nSample competitions:\n"
                        + "\n".join(samples)
                    )

            await reply(update, message)

        except Exception as exc:

            logger.exception(
                "GOAL API %s diagnostic failed",
                label,
            )

            await reply(
                update,
                f"❌ {label} ENDPOINT FAILED\n\n{str(exc)[:700]}",
            )


# ============================================================
# LEAGUE CATALOGUE
# ============================================================

async def leagues(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Search the locally synced full league catalogue."""
    query = " ".join(context.args).strip()

    try:
        connection = await asyncio.to_thread(get_connection)
        try:
            with connection.cursor() as cursor:
                if query:
                    pattern = f"%{query}%"
                    cursor.execute(
                        """
                        SELECT provider_league_id, name, region
                        FROM leagues
                        WHERE provider = %s AND (name LIKE %s OR region LIKE %s)
                        ORDER BY name LIMIT 35
                        """,
                        (PROVIDER_NAME, pattern, pattern),
                    )
                    rows = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS total FROM leagues
                        WHERE provider = %s AND (name LIKE %s OR region LIKE %s)
                        """,
                        (PROVIDER_NAME, pattern, pattern),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT provider_league_id, name, region
                        FROM leagues WHERE provider = %s
                        ORDER BY name LIMIT 35
                        """,
                        (PROVIDER_NAME,),
                    )
                    rows = cursor.fetchall()
                    cursor.execute(
                        "SELECT COUNT(*) AS total FROM leagues WHERE provider = %s",
                        (PROVIDER_NAME,),
                    )
                count_row = cursor.fetchone()
                total = count_row["total"] if count_row else 0
        finally:
            connection.close()

        if not total:
            await reply(
                update,
                "🏆 The local league catalogue is empty. Run /syncleagues first "
                "to download all available competitions.",
            )
            return

        heading = (
            f"🏆 LEAGUE SEARCH — {total} matches"
            if query else f"🏆 LEAGUE CATALOGUE — {total} competitions"
        )
        output = [heading, ""]
        for row in rows:
            output.append(
                f"• {row['name']} | {row.get('region') or 'Region unavailable'}"
            )
        if total > len(rows):
            output.extend(["", f"Showing {len(rows)}. Narrow the search with /leagues SEARCH."])
        await reply(update, "\n".join(output))

    except Exception as exc:
        logger.exception("League catalogue lookup failed")
        await reply(update, f"❌ LEAGUE LOOKUP FAILED\n\n{str(exc)[:700]}")


async def syncleagues(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await reply(
        update,
        "🔄 Downloading the current-season open-matchday league catalogue...",
    )
    try:
        raw_rows = await asyncio.to_thread(fetch_all_leagues)
        if not raw_rows:
            await reply(update, "⚠️ open-matchday returned no current-season league records. Nothing was saved.")
            return

        stats = await asyncio.to_thread(save_leagues_to_database, raw_rows)
        await reply(
            update,
            "✅ FULL LEAGUE CATALOGUE SYNC COMPLETE\n\n"
            f"open-matchday records collected: {len(raw_rows)}\n"
            f"Valid leagues saved/updated: {stats['saved']}\n"
            f"Records skipped: {stats['skipped']}\n\n"
            "Search any synced competition using /leagues SEARCH.",
        )
    except Exception as exc:
        logger.exception("Full league catalogue sync failed")
        await reply(update, f"❌ LEAGUE SYNC FAILED\n\n{str(exc)[:800]}")


async def leaguefixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Show one competition's fixtures for today or tomorrow."""
    args = list(context.args or [])
    if not args:
        await reply(
            update,
            "Usage: /leaguefixtures COMPETITION [today|tomorrow]\n"
            "Examples:\n"
            "/leaguefixtures Premier League today\n"
            "/leaguefixtures La Liga tomorrow\n"
            "/leaguefixtures Bundesliga\n\n"
            "If you omit the day, both today and tomorrow are shown.",
        )
        return

    day_filter = None
    final_word = args[-1].casefold()
    if final_word in ("today", "tomorrow"):
        day_filter = final_word
        args = args[:-1]

    query = " ".join(args).strip()
    if not query:
        await reply(update, "Please include a competition name.")
        return

    try:
        # Accept the exact display format emitted by /leagues:
        # "Competition Name | Region".
        parts = [part.strip() for part in query.split("|", 1)]
        competition_name = parts[0]
        competition_region = parts[1] if len(parts) == 2 else None

        def find_league():
            connection = get_connection()
            try:
                with connection.cursor() as cursor:
                    if competition_region:
                        cursor.execute(
                            """
                            SELECT provider_league_id, name, region
                            FROM leagues
                            WHERE provider = %s
                              AND LOWER(TRIM(name)) = LOWER(TRIM(%s))
                            ORDER BY name, region
                            LIMIT 500
                            """,
                            (PROVIDER_NAME, competition_name),
                        )
                        candidates = cursor.fetchall()
                        wanted_region = competition_region.casefold().strip()
                        matches = [
                            row for row in candidates
                            if (row.get("region") or "").casefold().strip() == wanted_region
                        ]
                    else:
                        cursor.execute(
                            """
                            SELECT provider_league_id, name, region
                            FROM leagues
                            WHERE provider = %s AND
                                  (LOWER(TRIM(name)) = LOWER(TRIM(%s))
                                   OR provider_league_id = %s)
                            ORDER BY name, region LIMIT 100
                            """,
                            (PROVIDER_NAME, competition_name, query),
                        )
                        matches = cursor.fetchall()
                        if not matches:
                            cursor.execute(
                                """
                                SELECT provider_league_id, name, region
                                FROM leagues
                                WHERE provider = %s AND name LIKE %s
                                ORDER BY name, region LIMIT 100
                                """,
                                (PROVIDER_NAME, f"%{competition_name}%"),
                            )
                            matches = cursor.fetchall()

                    unique = {}
                    for row in matches:
                        unique[row["provider_league_id"]] = row
                    return list(unique.values())
            finally:
                connection.close()

        matches = await asyncio.to_thread(find_league)
        if not matches:
            await reply(
                update,
                f"🏆 I couldn't find a competition matching “{query}”.\n"
                "Search the catalogue with /leagues SEARCH.",
            )
            return

        if len(matches) > 1:
            choices = [
                f"• {row['name']} | {row.get('region') or 'Region unavailable'}"
                for row in matches
            ]
            await reply(
                update,
                "Several competitions match that name. Choose the exact competition name and run:\n"
                "/leaguefixtures EXACT COMPETITION NAME today\n\n"
                + "\n".join(choices),
            )
            return

        league_id = matches[0]["provider_league_id"]
        league_name = matches[0]["name"]
        if day_filter:
            selected_days = [day_filter]
        else:
            selected_days = ["today", "tomorrow"]

        raw_rows = []
        for selected_day in selected_days:
            day_rows = await asyncio.to_thread(
                fetch_openmatchday_fixture_rows,
                14,
                league_id,
                selected_day,
            )
            raw_rows.extend(day_rows)

        selected = upcoming_fixtures(raw_rows, limit=40, include_past=False)
        if not selected:
            day_label = day_filter or "today and tomorrow"
            await reply(
                update,
                f"No fixtures were returned for {league_name} on {day_label}. "
                "The provider may not have coverage for that competition on the selected day.",
            )
            return

        day_label = day_filter.upper() if day_filter else "TODAY & TOMORROW"
        lines = format_fixture_list(
            selected,
            f"⚽ PUNTER KENYA  ·  {league_name.upper()}\n🏟 {len(selected)} matches · {day_label}",
        )
        await reply(update, "\n".join(lines))

    except Exception as exc:
        logger.exception("League fixture retrieval failed")
        await reply(
            update,
            f"❌ LEAGUE FIXTURE REQUEST FAILED\n\n{str(exc)[:700]}",
        )


def country_flag(country):
    """Return a flag for common country names; safely fall back when unknown."""
    value = str(country or "").strip().casefold()
    flags = {
        "usa": "🇺🇸", "united states": "🇺🇸", "united states of america": "🇺🇸",
        "brazil": "🇧🇷", "argentina": "🇦🇷", "mexico": "🇲🇽",
        "australia": "🇦🇺", "japan": "🇯🇵", "china": "🇨🇳",
        "england": "🏴", "scotland": "🏴", "wales": "🏴",
        "spain": "🇪🇸", "germany": "🇩🇪", "france": "🇫🇷",
        "italy": "🇮🇹", "portugal": "🇵🇹", "netherlands": "🇳🇱",
        "belgium": "🇧🇪", "turkey": "🇹🇷", "türkiye": "🇹🇷",
        "kenya": "🇰🇪", "south africa": "🇿🇦", "morocco": "🇲🇦",
        "egypt": "🇪🇬", "nigeria": "🇳🇬", "ghana": "🇬🇭",
        "saudi arabia": "🇸🇦", "south korea": "🇰🇷", "india": "🇮🇳",
        "sweden": "🇸🇪", "norway": "🇳🇴", "denmark": "🇩🇰",
        "switzerland": "🇨🇭", "austria": "🇦🇹", "poland": "🇵🇱",
        "ukraine": "🇺🇦", "greece": "🇬🇷", "croatia": "🇭🇷",
        "serbia": "🇷🇸", "czech republic": "🇨🇿", "czechia": "🇨🇿",
        "ireland": "🇮🇪", "northern ireland": "🇬🇧", "finland": "🇫🇮",
        "romania": "🇷🇴", "bulgaria": "🇧🇬", "cyprus": "🇨🇾",
        "israel": "🇮🇱", "united arab emirates": "🇦🇪",
    }
    return flags.get(value, "🌍")


def format_fixture_list(selected, heading, page=None, total_pages=None, total=None):
    """Create a compact, date-grouped Telegram fixture digest."""
    lines = [heading]
    if page is not None and total_pages is not None:
        first = (page - 1) * 20 + 1
        last = first + len(selected) - 1
        lines.append(f"📄 Page {page}/{total_pages}  ·  Matches {first}–{last} of {total}")
    lines.append("━━━━━━━━━━━━━━━━━━")

    current_date = None
    for _, item in selected:
        kickoff = item["kickoff_at"].replace(tzinfo=timezone.utc).astimezone(EAT)
        date_label = kickoff.strftime("%A · %d %b %Y")
        if date_label != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"📅 {date_label.upper()}")
            current_date = date_label

        competition = str(item.get("competition") or "Competition unavailable").strip()
        flag = country_flag(item.get("country"))
        lines.append(f"{kickoff:%H:%M} EAT  ·  {flag} {competition}")
        lines.append(f"   {item['home_team']}  vs  {item['away_team']}")

    return lines


async def fixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Browse today's and tomorrow's fixtures. Usage: /fixtures [today|tomorrow] [page]."""
    try:
        args = list(context.args or [])
        day_filter = None
        if args and args[0].casefold() in ("today", "tomorrow"):
            day_filter = args.pop(0).casefold()

        page = 1
        if args:
            if len(args) != 1 or not str(args[0]).isdigit():
                await reply(
                    update,
                    "Usage: /fixtures [today|tomorrow] [PAGE]\n"
                    "Examples: /fixtures today | /fixtures tomorrow | /fixtures 2",
                )
                return
            page = int(args[0])
            if page < 1 or page > 1000:
                await reply(update, "Enter a page number between 1 and 1000.")
                return

        selected_days = [day_filter] if day_filter else ["today", "tomorrow"]
        label = day_filter.upper() if day_filter else "TODAY & TOMORROW"
        await reply(update, f"⚽ Fetching {label.lower()} fixtures from open-matchday...")

        raw_rows = []
        for selected_day in selected_days:
            rows = await asyncio.to_thread(
                fetch_openmatchday_fixture_rows,
                14,
                None,
                selected_day,
            )
            raw_rows.extend(rows)

        # Re-evaluate against the current clock when rendering; never show elapsed kickoffs.
        all_selected = upcoming_fixtures(raw_rows, include_past=False)
        page_size = 20
        total = len(all_selected)
        total_pages = max(1, math.ceil(total / page_size))

        if not total:
            await reply(
                update,
                f"No fixtures were returned for {label.lower()}. "
                "Try /fixtures tomorrow or choose a competition with /leaguefixtures.",
            )
            return

        if page > total_pages:
            await reply(
                update,
                f"Only {total} unique fixtures were found across {total_pages} page(s).\n"
                f"Use /fixtures {day_filter + ' ' if day_filter else ''}{total_pages} for the last page.",
            )
            return

        start_index = (page - 1) * page_size
        selected = all_selected[start_index:start_index + page_size]
        lines = format_fixture_list(
            selected,
            f"⚽ PUNTER KENYA  ·  {label} FIXTURES\n🏟 {total} unique matches",
            page=page,
            total_pages=total_pages,
            total=total,
        )
        if page < total_pages:
            lines.extend(["", f"➡️ NEXT PAGE  /fixtures {day_filter + ' ' if day_filter else ''}{page + 1}"])
        if page > 1:
            lines.append(f"⬅️ PREVIOUS PAGE  /fixtures {day_filter + ' ' if day_filter else ''}{page - 1}")
        await reply(update, "\n".join(lines))

    except Exception as exc:
        logger.exception("Fixture retrieval failed")
        await reply(update, f"❌ FIXTURE REQUEST FAILED\n\n{str(exc)[:700]}")


# FIXTURE DATABASE SYNC
# ============================================================

async def syncfixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "🔄 Retrieving upcoming fixtures and saving valid records to the database...",
    )

    try:

        raw_rows = await asyncio.to_thread(
            fetch_upcoming_fixture_rows,
            14,
        )

        selected = [
            raw
            for raw, _ in upcoming_fixtures(
                raw_rows,
                limit=1000,
            )
        ]

        if not selected:

            await reply(
                update,
                "⚠️ No upcoming fixtures were returned by the 14-day date search. "
                "Nothing was saved.\n"
                f"Date records received: {len(raw_rows)}",
            )

            return

        stats = await asyncio.to_thread(
            save_fixtures_to_database,
            selected,
        )

        await reply(
            update,
            "✅ FIXTURE DATABASE SYNC COMPLETE\n\n"
            f"open-matchday records received: {len(raw_rows)}\n"
            f"Future fixtures selected: {len(selected)}\n"
            f"Valid records saved/updated: {stats['saved']}\n"
            f"Invalid records skipped: {stats['skipped']}\n\n"
            "Coverage: date-based retrieval across the next 14 days, "
            "with pagination and duplicate removal.",
        )

    except Exception as exc:

        logger.exception(
            "Fixture database sync failed"
        )

        await reply(
            update,
            f"❌ FIXTURE SYNC FAILED\n\n{str(exc)[:800]}",
        )


# ============================================================
# LIVE MATCHES
# ============================================================

async def live(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await reply(
        update,
        "⚠️ LIVE MATCH TRACKING IS NOT AVAILABLE FROM OPENLIGADB.\n\n"
        "The free open-matchday feed supplies fixtures and results, but it does not "
        "provide a dependable live clock or real-time match status. "
        "PUNTER KENYA will not label scheduled matches as live or invent scores.",
    )




def team_name_matches_query(query_name, actual_name):
    """
    Match provider variants conservatively but more flexibly across competitions.
    Handles accents, punctuation, club suffixes, common abbreviations and reordered
    names without accepting a match based on a single generic token.
    """
    from difflib import SequenceMatcher

    ignored = {
        "fc", "cf", "sc", "afc", "club", "football", "soccer", "the",
        "de", "del", "da", "do", "dos", "la", "el", "ud", "cd", "ac",
        "as", "sv", "fk", "sk", "bk", "if", "ik", "ks", "calcio",
    }
    aliases = {
        "atl": "atletico", "ath": "athletic", "dep": "deportivo",
        "man utd": "manchester united", "man city": "manchester city",
        "inter": "internazionale", "psv eindhoven": "psv",
        "sporting lisbon": "sporting cp", "red star": "crvena zvezda",
        "bayern munich": "bayern munchen", "paris saint germain": "paris sg",
    }

    def normalised(value):
        value = unicodedata.normalize("NFKD", str(value or ""))
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        phrase = " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
        phrase = aliases.get(phrase, phrase)
        tokens = [token for token in phrase.split() if token not in ignored]
        return " ".join(tokens)

    wanted_phrase = normalised(query_name)
    actual_phrase = normalised(actual_name)
    if not wanted_phrase or not actual_phrase:
        return False
    if wanted_phrase == actual_phrase:
        return True

    wanted = set(wanted_phrase.split())
    actual = set(actual_phrase.split())
    if not wanted or not actual:
        return False
    overlap = len(wanted & actual)
    if overlap == 0:
        return False

    # A complete shorter name contained in the longer name is a strong match.
    if wanted.issubset(actual) or actual.issubset(wanted):
        return True

    # Token overlap helps when one provider adds a city/club word or reverses
    # a multi-word name. Require at least two shared meaningful tokens.
    containment = overlap / min(len(wanted), len(actual))
    jaccard = overlap / len(wanted | actual)
    if overlap >= 2 and containment >= 0.80 and jaccard >= 0.50:
        return True

    # Allow moderate spelling/abbreviation variation, but never fuzzy-match
    # short single-word clubs or names sharing only one token.
    if min(len(wanted_phrase), len(actual_phrase)) >= 8 and overlap >= 1:
        ratio = SequenceMatcher(None, wanted_phrase, actual_phrase).ratio()
        if ratio >= 0.82 and containment >= 0.50:
            return True
    return False


# ============================================================
# PRE-MATCH MODEL — RECENT RESULTS + POISSON SCORE DISTRIBUTION
# ============================================================

def entity_id(value):
    if isinstance(value, dict):
        return first_value(value, "id", "teamId", "team_id", "apiId")
    return None


def fixture_team_id(raw, side):
    direct = first_value(raw, f"{side}TeamId", f"{side}_team_id")
    if direct is not None:
        return str(direct)
    nested_key = "team1" if side == "home" else "team2"
    nested = first_value(raw, f"{side}Team", side.lower(), nested_key)
    value = entity_id(nested)
    return str(value) if value is not None else None


def poisson_probability(goals, expected):
    return math.exp(-expected) * (expected ** goals) / math.factorial(goals)


def build_pre_match_prediction(raw_fixture, fixture):
    """Build a cautious pre-lineup forecast from verified recent team results."""
    home_id = fixture_team_id(raw_fixture, "home")
    away_id = fixture_team_id(raw_fixture, "away")
    if not home_id or not away_id:
        return {
            "available": False,
            "reason": "The fixture feed did not include both team IDs, so recent team results could not be linked safely.",
        }

    def get_recent(team_id, team_name):
        payload = goal_request(f"teams/{team_id}/results", {"limit": 10, "offset": 0})
        rows = extract_list(payload, ("results", "matches", "fixtures"))
        parsed = []
        for row in rows:
            item = normalise_fixture(row)
            if not item or item["home_score"] is None or item["away_score"] is None:
                continue
            row_home_id = fixture_team_id(row, "home")
            row_away_id = fixture_team_id(row, "away")
            if row_home_id == team_id:
                is_home = True
            elif row_away_id == team_id:
                is_home = False
            elif team_name_matches_query(item["home_team"], team_name):
                is_home = True
            elif team_name_matches_query(item["away_team"], team_name):
                is_home = False
            else:
                continue

            home_goals = item["home_score"]
            away_goals = item["away_score"]
            gf, ga = (home_goals, away_goals) if is_home else (away_goals, home_goals)
            if gf > ga:
                result = "W"
            elif gf == ga:
                result = "D"
            else:
                result = "L"
            opponent = item["away_team"] if is_home else item["home_team"]
            parsed.append({
                "date": item["kickoff_at"],
                "opponent": opponent,
                "venue": "H" if is_home else "A",
                "score": f"{home_goals}-{away_goals}",
                "gf": gf,
                "ga": ga,
                "result": result,
            })
        parsed.sort(key=lambda x: x["date"] or datetime.min, reverse=True)
        return parsed[:10]

    with ThreadPoolExecutor(max_workers=2) as pool:
        home_future = pool.submit(get_recent, home_id, fixture["home_team"])
        away_future = pool.submit(get_recent, away_id, fixture["away_team"])
        home_results = home_future.result()
        away_results = away_future.result()

    if len(home_results) < 5 or len(away_results) < 5:
        return {
            "available": False,
            "reason": f"Insufficient verified recent results (home: {len(home_results)}, away: {len(away_results)}; at least 5 each required).",
        }

    def averages(rows):
        return (
            sum(x["gf"] for x in rows) / len(rows),
            sum(x["ga"] for x in rows) / len(rows),
        )

    home_scored, home_conceded = averages(home_results)
    away_scored, away_conceded = averages(away_results)

    # Modest home advantage; rates are bounded to avoid unstable extreme outputs.
    lambda_home = max(0.15, min(4.0, ((home_scored + away_conceded) / 2) * 1.08))
    lambda_away = max(0.15, min(4.0, ((away_scored + home_conceded) / 2) * 0.92))

    score_grid = []
    for hg in range(9):
        for ag in range(9):
            probability = poisson_probability(hg, lambda_home) * poisson_probability(ag, lambda_away)
            score_grid.append((probability, hg, ag))

    home_win = sum(p for p, hg, ag in score_grid if hg > ag)
    draw = sum(p for p, hg, ag in score_grid if hg == ag)
    away_win = sum(p for p, hg, ag in score_grid if hg < ag)
    over_15 = sum(p for p, hg, ag in score_grid if hg + ag >= 2)
    over_25 = sum(p for p, hg, ag in score_grid if hg + ag >= 3)
    most_likely = max(score_grid, key=lambda x: x[0])

    probabilities = {"1": home_win, "X": draw, "2": away_win}
    top_outcome = max(probabilities, key=probabilities.get)
    sample_strength = min(1.0, (len(home_results) + len(away_results)) / 20)
    confidence = "LOW" if sample_strength < 0.7 else "MODERATE"
    if max(probabilities.values()) >= 0.58 and sample_strength >= 0.9:
        confidence = "MODERATE"

    return {
        "available": True,
        "home_results": len(home_results),
        "away_results": len(away_results),
        "home_scored": home_scored,
        "home_conceded": home_conceded,
        "away_scored": away_scored,
        "away_conceded": away_conceded,
        "home_form": "".join(x["result"] for x in reversed(home_results)),
        "away_form": "".join(x["result"] for x in reversed(away_results)),
        "home_recent_matches": home_results,
        "away_recent_matches": away_results,
        "home_home_sample": len(home_venue_results),
        "away_away_sample": len(away_venue_results),
        "home_home_gf": (sum(x["gf"] for x in home_venue_results) / len(home_venue_results)) if home_venue_results else None,
        "home_home_ga": (sum(x["ga"] for x in home_venue_results) / len(home_venue_results)) if home_venue_results else None,
        "away_away_gf": (sum(x["gf"] for x in away_venue_results) / len(away_venue_results)) if away_venue_results else None,
        "away_away_ga": (sum(x["ga"] for x in away_venue_results) / len(away_venue_results)) if away_venue_results else None,
        "h2h_count": len(h2h_results),
        "h2h_results": h2h_results,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "elo_home_rating": elo_home_rating,
        "elo_away_rating": elo_away_rating,
        "elo_matches_used": len(elo_rows),
        "monte_carlo": monte_carlo,
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
        "over_15": over_15,
        "over_25": over_25,
        "score": (most_likely[1], most_likely[2]),
        "top_outcome": top_outcome,
        "confidence": confidence,
        "home_home_sample": len(home_venue_results),
        "away_away_sample": len(away_venue_results),
        "home_home_gf": (sum(match["gf"] for match in home_venue_results) / len(home_venue_results)) if home_venue_results else None,
        "home_home_ga": (sum(match["ga"] for match in home_venue_results) / len(home_venue_results)) if home_venue_results else None,
        "away_away_gf": (sum(match["gf"] for match in away_venue_results) / len(away_venue_results)) if away_venue_results else None,
        "away_away_ga": (sum(match["ga"] for match in away_venue_results) / len(away_venue_results)) if away_venue_results else None,
        "h2h_count": len(h2h_results),
        "h2h_results": h2h_results,
        "elo_home_rating": elo_home_rating,
        "elo_away_rating": elo_away_rating,
        "elo_matches_used": len(elo_rows),
        "monte_carlo": monte_carlo,
        "model_version": "venue-weighted-poisson-elo-mc-v1",
    }



def build_openmatchday_prediction(raw_fixture, fixture, historical_rows=None, as_of_date=None, model_variant="current", include_monte_carlo=True, team_history_rows=None):
    """Forecast from competition results strictly before as_of_date when supplied."""
    folder = str(raw_fixture.get("leagueShortcut") or "").strip()
    if not folder:
        return {"available": False, "reason": "The fixture has no competition reference for retrieving its results."}

    if historical_rows is not None:
        rows = list(historical_rows)
    else:
        try:
            rows = fetch_openmatchday_csv(folder, "results")
        except Exception as exc:
            logger.warning("Could not load historical results for %s: %s", folder, exc)
            return {"available": False, "reason": "Historical results could not be retrieved for this competition."}

    team_rows = list(team_history_rows) if team_history_rows is not None else list(rows)

    if as_of_date is not None:
        cutoff_date = as_of_date.date() if isinstance(as_of_date, datetime) else datetime.strptime(str(as_of_date)[:10], "%Y-%m-%d").date()
        def before_cutoff(source_rows):
            prior = []
            for row in source_rows:
                try:
                    row_date = datetime.strptime(str(row.get("date") or ""), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if row_date < cutoff_date:
                    prior.append(row)
            return prior
        rows = before_cutoff(rows)
        team_rows = before_cutoff(team_rows)

    def parse_team_history(team_name):
        parsed = []
        for row in team_rows:
            home = str(row.get("home") or "").strip()
            away = str(row.get("away") or "").strip()
            try:
                home_goals = int(row.get("ft_home"))
                away_goals = int(row.get("ft_away"))
            except (TypeError, ValueError):
                continue

            if team_name_matches_query(team_name, home):
                is_home = True
            elif team_name_matches_query(team_name, away):
                is_home = False
            else:
                continue

            gf, ga = (home_goals, away_goals) if is_home else (away_goals, home_goals)
            result = "W" if gf > ga else "D" if gf == ga else "L"
            try:
                match_date = datetime.strptime(str(row.get("date", "")), "%Y-%m-%d")
            except ValueError:
                match_date = None
            parsed.append({
                "date": match_date,
                "opponent": away if is_home else home,
                "venue": "H" if is_home else "A",
                "score": f"{home_goals}-{away_goals}",
                "gf": gf,
                "ga": ga,
                "result": result,
            })

        parsed.sort(key=lambda item: item["date"] or datetime.min, reverse=True)
        return parsed[:10]

    home_results = parse_team_history(fixture["home_team"])
    away_results = parse_team_history(fixture["away_team"])
    if len(home_results) < 5 or len(away_results) < 5:
        return {
            "available": False,
            "reason": (
                f"Insufficient recent results in this competition "
                f"(home: {len(home_results)}, away: {len(away_results)}; "
                "at least 5 each required)."
            ),
        }

    def averages(history):
        return (
            sum(match["gf"] for match in history) / len(history),
            sum(match["ga"] for match in history) / len(history),
        )

    home_scored, home_conceded = averages(home_results)
    away_scored, away_conceded = averages(away_results)

    if model_variant == "venue_weighted":
        # Candidate model: recent venue-specific rates, exponentially weighted,
        # then shrunk towards the competition scoring baseline to reduce small-sample noise.
        def weighted_rate(history, field):
            selected = history[:8]
            if len(selected) < 3:
                selected = history[:8]
            weights = [0.82 ** index for index in range(len(selected))]
            return (sum(row[field] * weight for row, weight in zip(selected, weights)) / sum(weights)
                    if selected else None)

        home_venue = [row for row in home_results if row["venue"] == "H"][:8]
        away_venue = [row for row in away_results if row["venue"] == "A"][:8]
        if len(home_venue) < 3:
            home_venue = home_results[:8]
        if len(away_venue) < 3:
            away_venue = away_results[:8]

        league_totals = []
        for result_row in rows:
            try:
                league_totals.append((int(result_row.get("ft_home")) + int(result_row.get("ft_away"))) / 2)
            except (TypeError, ValueError):
                continue
        league_rate = sum(league_totals) / len(league_totals) if league_totals else 1.25
        league_rate = max(0.35, min(2.5, league_rate))

        def shrink(value, sample_size):
            weight = sample_size / (sample_size + 4.0)
            return weight * value + (1.0 - weight) * league_rate

        home_attack = shrink(weighted_rate(home_venue, "gf"), len(home_venue))
        home_defence = shrink(weighted_rate(home_venue, "ga"), len(home_venue))
        away_attack = shrink(weighted_rate(away_venue, "gf"), len(away_venue))
        away_defence = shrink(weighted_rate(away_venue, "ga"), len(away_venue))
        lambda_home = max(0.15, min(4.0, (home_attack * away_defence / league_rate) * 1.05))
        lambda_away = max(0.15, min(4.0, (away_attack * home_defence / league_rate) * 0.95))
    else:
        lambda_home = max(0.15, min(4.0, ((home_scored + away_conceded) / 2) * 1.08))
        lambda_away = max(0.15, min(4.0, ((away_scored + home_conceded) / 2) * 0.92))

    # Chronological competition Elo, strictly excluding the forecast date.
    elo_rows = []
    for result_row in rows:
        try:
            elo_rows.append({
                "date": result_row.get("date"), "home": result_row.get("home"),
                "away": result_row.get("away"), "home_goals": result_row.get("ft_home"),
                "away_goals": result_row.get("ft_away"),
            })
        except Exception:
            continue
    elo_ratings = build_elo_ratings(
        elo_rows, before_date=(as_of_date.date().isoformat() if isinstance(as_of_date, datetime)
                               else str(as_of_date)[:10] if as_of_date else None)
    )
    elo_home_rating = elo_ratings.get(fixture["home_team"])
    elo_away_rating = elo_ratings.get(fixture["away_team"])
    # Reproducible Monte Carlo cross-check from the same transparent goal-rate inputs.
    monte_carlo = monte_carlo_scoreline(lambda_home, lambda_away, simulations=10000) if include_monte_carlo else None

    grid = []
    for home_goals in range(9):
        for away_goals in range(9):
            probability = (
                poisson_probability(home_goals, lambda_home)
                * poisson_probability(away_goals, lambda_away)
            )
            grid.append((probability, home_goals, away_goals))

    home_win = sum(p for p, hg, ag in grid if hg > ag)
    draw = sum(p for p, hg, ag in grid if hg == ag)
    away_win = sum(p for p, hg, ag in grid if hg < ag)
    over_15 = sum(p for p, hg, ag in grid if hg + ag >= 2)
    over_25 = sum(p for p, hg, ag in grid if hg + ag >= 3)
    most_likely = max(grid, key=lambda item: item[0])
    probabilities = {"1": home_win, "X": draw, "2": away_win}
    top_outcome = max(probabilities, key=probabilities.get)
    # The research gate already requires at least five verified matches
    # for EACH team. Do not then label a valid 5+5 sample LOW merely because
    # the combined total is below 14; that made the confidence gate stricter
    # than the documented minimum and discarded otherwise assessable fixtures.
    # Confidence reflects sample depth only, not certainty of the outcome.
    combined_sample = len(home_results) + len(away_results)
    confidence = "LOW" if min(len(home_results), len(away_results)) < 5 else (
        "HIGH" if combined_sample >= 16 else "MODERATE"
    )

    # Same-competition head-to-head sample, restricted to completed results
    # already available before this fixture's forecast cutoff.
    h2h_results = []
    home_name, away_name = fixture["home_team"], fixture["away_team"]
    for result_row in rows:
        rh = str(result_row.get("home") or "").strip()
        ra = str(result_row.get("away") or "").strip()
        if not rh or not ra:
            continue
        direct = team_name_matches_query(home_name, rh) and team_name_matches_query(away_name, ra)
        reverse = team_name_matches_query(home_name, ra) and team_name_matches_query(away_name, rh)
        if not (direct or reverse):
            continue
        try:
            rhg, rag = int(result_row.get("ft_home")), int(result_row.get("ft_away"))
        except (TypeError, ValueError):
            continue
        try:
            rdate = datetime.strptime(str(result_row.get("date") or ""), "%Y-%m-%d")
        except ValueError:
            rdate = None
        h2h_results.append({
            "date": rdate,
            "home_goals": rhg if direct else rag,
            "away_goals": rag if direct else rhg,
        })
    h2h_results.sort(key=lambda item: item["date"] or datetime.min, reverse=True)
    h2h_results = h2h_results[:5]
    home_venue_results = [match for match in home_results if match["venue"] == "H"]
    away_venue_results = [match for match in away_results if match["venue"] == "A"]
    home_home_gf, home_home_ga = averages(home_venue_results) if home_venue_results else (None, None)
    away_away_gf, away_away_ga = averages(away_venue_results) if away_venue_results else (None, None)

    # Return observed evidence fields required by the accumulator research gate.
    return {
        "available": True,
        "home_results": len(home_results),
        "away_results": len(away_results),
        "home_scored": home_scored,
        "home_conceded": home_conceded,
        "away_scored": away_scored,
        "away_conceded": away_conceded,
        "home_form": "".join(match["result"] for match in reversed(home_results)),
        "away_form": "".join(match["result"] for match in reversed(away_results)),
        "home_recent_matches": home_results,
        "away_recent_matches": away_results,
        "home_home_sample": len(home_venue_results),
        "away_away_sample": len(away_venue_results),
        "home_home_gf": home_home_gf,
        "home_home_ga": home_home_ga,
        "away_away_gf": away_away_gf,
        "away_away_ga": away_away_ga,
        "h2h_count": len(h2h_results),
        "h2h_results": [
            {"date": item["date"].date().isoformat() if item.get("date") else None,
             "home_goals": item["home_goals"], "away_goals": item["away_goals"]}
            for item in h2h_results
        ],
        "elo_home_rating": elo_home_rating,
        "elo_away_rating": elo_away_rating,
        "elo_matches_used": len(elo_rows),
        "monte_carlo": monte_carlo,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
        "over_15": over_15,
        "over_25": over_25,
        "score": (most_likely[1], most_likely[2]),
        "top_outcome": top_outcome,
        "confidence": confidence,
    }



# ============================================================
# PUNTER KENYA — MATCH ANALYSIS COMMANDS
# Historical-result analysis only; no unsupported live/odds claims.
# ============================================================

def load_competition_analysis(raw_fixture, fixture):
    folder = str(raw_fixture.get("leagueShortcut") or "").strip()
    if not folder:
        return {"available": False, "reason": "Competition reference is missing."}
    try:
        rows = fetch_openmatchday_csv(folder, "results")
    except Exception as exc:
        logger.warning("Analysis results unavailable for %s: %s", folder, exc)
        return {"available": False, "reason": "Historical results could not be downloaded."}

    def history(team):
        matches = []
        for row in rows:
            home = str(row.get("home") or "").strip()
            away = str(row.get("away") or "").strip()
            if not home or not away:
                continue
            try:
                hg, ag = int(row.get("ft_home")), int(row.get("ft_away"))
            except (TypeError, ValueError):
                continue
            if team_name_matches_query(team, home):
                is_home = True
            elif team_name_matches_query(team, away):
                is_home = False
            else:
                continue
            gf, ga = (hg, ag) if is_home else (ag, hg)
            result = "W" if gf > ga else "D" if gf == ga else "L"
            try:
                date = datetime.strptime(str(row.get("date", "")), "%Y-%m-%d")
            except ValueError:
                date = None
            matches.append({
                "date": date, "opponent": away if is_home else home,
                "venue": "H" if is_home else "A", "gf": gf, "ga": ga,
                "home_goals": hg, "away_goals": ag, "result": result,
                "points": 3 if result == "W" else 1 if result == "D" else 0,
            })
        matches.sort(key=lambda x: x["date"] or datetime.min, reverse=True)
        return matches

    home = history(fixture["home_team"])
    away = history(fixture["away_team"])
    if len(home) < 5 or len(away) < 5:
        return {
            "available": False,
            "reason": f"Only {len(home)} home-team and {len(away)} away-team results found; at least five each are required.",
        }

    def metrics(matches):
        n = len(matches)
        gf = sum(x["gf"] for x in matches) / n
        ga = sum(x["ga"] for x in matches) / n
        points = sum(x["points"] for x in matches) / n
        scored = sum(x["gf"] > 0 for x in matches) / n
        conceded = sum(x["ga"] > 0 for x in matches) / n
        clean = sum(x["ga"] == 0 for x in matches) / n
        failed = sum(x["gf"] == 0 for x in matches) / n
        goal_totals = [x["gf"] + x["ga"] for x in matches]
        over15 = sum(v >= 2 for v in goal_totals) / n
        over25 = sum(v >= 3 for v in goal_totals) / n
        variance = sum((x["points"] - points) ** 2 for x in matches) / n
        consistency = max(0, 100 - math.sqrt(variance) * 24)
        return {
            "n": n, "gf": gf, "ga": ga, "points": points,
            "scored_rate": scored, "conceded_rate": conceded,
            "clean_rate": clean, "failed_rate": failed,
            "over15": over15, "over25": over25,
            "consistency": consistency,
            "form": "".join(x["result"] for x in reversed(matches[:5])),
        }

    # Venue-specific performance uses the same available competition history.
    def venue_metrics(matches, venue):
        selected = [x for x in matches if x["venue"] == venue]
        if len(selected) < 3:
            return None
        n = len(selected)
        return {
            "n": n,
            "gf": sum(x["gf"] for x in selected) / n,
            "ga": sum(x["ga"] for x in selected) / n,
            "points": sum(x["points"] for x in selected) / n,
            "win_rate": sum(x["result"] == "W" for x in selected) / n,
        }

    h2h = []
    for row in rows:
        home_name, away_name = str(row.get("home") or ""), str(row.get("away") or "")
        if not ((team_name_matches_query(fixture["home_team"], home_name)
                 and team_name_matches_query(fixture["away_team"], away_name))
                or (team_name_matches_query(fixture["home_team"], away_name)
                    and team_name_matches_query(fixture["away_team"], home_name))):
            continue
        try:
            hg, ag = int(row.get("ft_home")), int(row.get("ft_away"))
        except (TypeError, ValueError):
            continue
        try:
            date = datetime.strptime(str(row.get("date", "")), "%Y-%m-%d")
        except ValueError:
            date = None
        h2h.append({"date": date, "home": home_name, "away": away_name, "hg": hg, "ag": ag})
    h2h.sort(key=lambda x: x["date"] or datetime.min, reverse=True)

    return {
        "available": True, "league_shortcut": folder, "home": home, "away": away,
        "home_metrics": metrics(home), "away_metrics": metrics(away),
        "home_at_home": venue_metrics(home, "H"),
        "away_away": venue_metrics(away, "A"),
        "h2h": h2h[:10], "competition": fixture.get("competition"),
    }


def find_analysis_fixture(query):
    parts = re.split(r"\s+(?:vs?\.?|v)\s+", query, maxsplit=1, flags=re.I)
    if len(parts) != 2 or not all(x.strip() for x in parts):
        return None, None, "Use both team names: TEAM A vs TEAM B."
    first, second = parts[0].strip(), parts[1].strip()
    rows = fetch_matchintel_fixture_rows(14)
    found = []
    for raw in rows:
        item = normalise_fixture(raw)
        if not item:
            continue
        if ((team_name_matches_query(first, item["home_team"]) and team_name_matches_query(second, item["away_team"]))
            or (team_name_matches_query(first, item["away_team"]) and team_name_matches_query(second, item["home_team"]))):
            found.append((raw, item))
    if not found:
        return None, None, "Fixture not found in the next 14 days. Check /fixtures or verify the team names."
    if len(found) > 1:
        return None, None, "More than one fixture matched. Include the competition or use the exact team names."
    return found[0][0], found[0][1], None


async def get_analysis(update, context):
    query = " ".join(context.args).strip()
    if not query:
        await reply(update, "Usage: /COMMAND TEAM A vs TEAM B")
        return None
    try:
        raw, fixture, error = await asyncio.to_thread(find_analysis_fixture, query)
        if error:
            await reply(update, "⚠️ " + error)
            return None
        data = await asyncio.to_thread(load_competition_analysis, raw, fixture)
        if not data.get("available"):
            await reply(update, "⚠️ ANALYSIS UNAVAILABLE\n\n" + data.get("reason", "Insufficient data."))
            return None
        return fixture, data
    except Exception as exc:
        logger.exception("Match analysis command failed")
        await reply(update, f"❌ Analysis failed: {str(exc)[:350]}")
        return None


def analysis_header(command, fixture):
    return f"⚽ PUNTER KENYA | {command}\n{fixture['home_team']} vs {fixture['away_team']}\n🏆 {fixture.get('competition') or 'Competition'}\n\n"


async def form_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    h, a = d["home_metrics"], d["away_metrics"]
    await reply(update, analysis_header("FORM REPORT", fixture) +
        f"HOME — {fixture['home_team']}\nLast five: {h['form']}\nPoints per match: {h['points']:.2f}\nWin rate: {sum(x['result']=='W' for x in d['home'][:10])/len(d['home']):.0%}\n\n"
        f"AWAY — {fixture['away_team']}\nLast five: {a['form']}\nPoints per match: {a['points']:.2f}\nWin rate: {sum(x['result']=='W' for x in d['away'][:10])/len(d['away']):.0%}\n\n"
        "Based on the latest available completed competition results; opponent strength is not adjusted.")

async def goals_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    h, a = d["home_metrics"], d["away_metrics"]
    await reply(update, analysis_header("GOAL INTELLIGENCE", fixture) +
        f"{fixture['home_team']}\nGF: {h['gf']:.2f} per match | GA: {h['ga']:.2f}\nScored in: {h['scored_rate']:.0%} | Conceded in: {h['conceded_rate']:.0%}\nClean sheets: {h['clean_rate']:.0%} | Failed to score: {h['failed_rate']:.0%}\n\n"
        f"{fixture['away_team']}\nGF: {a['gf']:.2f} per match | GA: {a['ga']:.2f}\nScored in: {a['scored_rate']:.0%} | Conceded in: {a['conceded_rate']:.0%}\nClean sheets: {a['clean_rate']:.0%} | Failed to score: {a['failed_rate']:.0%}\n\n"
        "GF = goals scored; GA = goals conceded. Rates use up to 10 recent matches.")

async def homeaway_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    h, a = d["home_at_home"], d["away_away"]
    def fmt(x):
        return "Insufficient venue sample (need at least 3 matches)." if not x else f"Matches: {x['n']} | GF: {x['gf']:.2f} | GA: {x['ga']:.2f} | Points/match: {x['points']:.2f} | Win rate: {x['win_rate']:.0%}"
    await reply(update, analysis_header("HOME / AWAY REPORT", fixture) +
        f"HOME TEAM AT HOME\n{fixture['home_team']}\n{fmt(h)}\n\nAWAY TEAM AWAY FROM HOME\n{fixture['away_team']}\n{fmt(a)}\n\nVenue splits are calculated only from the team's latest 10 available results.")

async def halves_command(update, context):
    query = " ".join(context.args).strip()
    if not query:
        await reply(update, "Usage: /halves TEAM A vs TEAM B"); return
    await reply(update, "⏱ HALF-TIME INTELLIGENCE\n\nThe current open-matchday results feed provides full-time scores only. It does not provide verified half-time scores or goal-minute events. PUNTER will not invent first-half or second-half statistics.\n\nThis module will activate when a reliable half-time/event source is connected.")

async def h2h_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    if not d["h2h"]:
        await reply(update, analysis_header("HEAD-TO-HEAD", fixture) + "No completed head-to-head matches were found in this competition's available results.")
        return
    lines = [analysis_header("HEAD-TO-HEAD", fixture), "Recent meetings (newest first):"]
    for m in d["h2h"]:
        date = m["date"].strftime("%d %b %Y") if m["date"] else "Date n/a"
        lines.append(f"• {date} | {m['home']} {m['hg']}-{m['ag']} {m['away']}")
    await reply(update, "\n".join(lines))

async def strength_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    h, a = d["home_metrics"], d["away_metrics"]
    await reply(update, analysis_header("TEAM STRENGTH", fixture) +
        f"{fixture['home_team']}\nAttack: {h['gf']:.2f} goals/match | Defence: {h['ga']:.2f} conceded/match\nConsistency indicator: {h['consistency']:.0f}/100 | Points/match: {h['points']:.2f}\n\n"
        f"{fixture['away_team']}\nAttack: {a['gf']:.2f} goals/match | Defence: {a['ga']:.2f} conceded/match\nConsistency indicator: {a['consistency']:.0f}/100 | Points/match: {a['points']:.2f}\n\n"
        "Consistency is a simple points-variation indicator, not a proven predictive rating. Strength is not adjusted for opponent quality.")

async def matchup_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    h, a = d["home_metrics"], d["away_metrics"]
    home_edge = (h["gf"] - a["ga"]) - (a["gf"] - h["ga"])
    edge = "Home-side statistical lean" if home_edge > .15 else "Away-side statistical lean" if home_edge < -.15 else "No clear statistical edge"
    await reply(update, analysis_header("MATCHUP ANALYSIS", fixture) +
        f"Home attack vs away defence: {h['gf']:.2f} scored against {a['ga']:.2f} conceded per match.\n"
        f"Away attack vs home defence: {a['gf']:.2f} scored against {h['ga']:.2f} conceded per match.\n\n"
        f"Comparison: {edge}.\n\n"
        "This is a statistical comparison, not tactical video analysis. Opponent strength and lineups are not available.")

async def markets_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    pred = await asyncio.to_thread(build_openmatchday_prediction, {
        "leagueShortcut": d["league_shortcut"]
    }, fixture)
    if not pred.get("available"):
        await reply(update, analysis_header("MARKET PROBABILITIES", fixture) + "Model probabilities unavailable: " + pred.get("reason", "insufficient data")); return
    await reply(update, analysis_header("MARKET PROBABILITIES", fixture) +
        f"1X2 estimates\n1 — Home win: {pred['home_win']:.1%}\nX — Draw: {pred['draw']:.1%}\n2 — Away win: {pred['away_win']:.1%}\n\n"
        f"Goal estimates\nOver 1.5: {pred['over_15']:.1%}\nOver 2.5: {pred['over_25']:.1%}\nMost likely score: {pred['score'][0]}-{pred['score'][1]}\n\n"
        "These are model estimates, not bookmaker prices or guaranteed outcomes. No BTTS or Under 2.5 recommendations are generated.")

async def scenarios_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    pred = await asyncio.to_thread(build_openmatchday_prediction, {
        "leagueShortcut": d["league_shortcut"]
    }, fixture)
    if not pred.get("available"):
        await reply(update, analysis_header("MATCH SCENARIOS", fixture) + pred.get("reason", "Insufficient data.")); return
    h, a = pred["lambda_home"], pred["lambda_away"]
    total = h + a
    await reply(update, analysis_header("MATCH SCENARIOS", fixture) +
        f"Expected goals: {h:.2f} – {a:.2f}\n\n"
        f"• Home-control scenario: more likely if {fixture['home_team']} converts its home scoring edge.\n"
        f"• Away-threat scenario: {fixture['away_team']} has a route through its estimated {a:.2f} goals.\n"
        f"• Lower-scoring scenario: possible if both attacks fall below their recent averages.\n\n"
        f"Model total-goal expectation: {total:.2f}.\nThese are conditional scenarios, not independent probability forecasts.")

async def risk_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    h, a = d["home_metrics"], d["away_metrics"]
    flags = []
    if min(h["n"], a["n"]) < 8: flags.append("Limited sample: fewer than 8 results for at least one team.")
    if abs(h["points"] - a["points"]) < .35: flags.append("Teams have similar recent points rates.")
    if min(h["consistency"], a["consistency"]) < 45: flags.append("At least one team has volatile recent points results.")
    if abs(h["gf"] - a["ga"]) < .25 and abs(a["gf"] - h["ga"]) < .25: flags.append("No clear scoring-versus-defence mismatch.")
    if not flags: flags.append("No basic statistical warning flag detected; this does not mean the match is safe.")
    await reply(update, analysis_header("RISK SCREEN", fixture) +
        "⚠️ DATA LIMITATIONS\n• No confirmed lineups, injuries, suspensions, xG, odds or live events.\n\n"
        "RISK FLAGS\n" + "\n".join("• " + x for x in flags) +
        "\n\nDecision: do not place a bet from this screen alone. Check market price and value first.")

async def value_command(update, context):
    args = context.args
    if len(args) < 4:
        await reply(update, "Usage: /value TEAM A vs TEAM B HOME_ODDS DRAW_ODDS AWAY_ODDS\nExample: /value Brighton vs Brentford 2.10 3.40 3.50\nUse decimal odds from your bookmaker."); return
    try:
        odds = [float(x) for x in args[-3:]]
        if any(x <= 1 for x in odds): raise ValueError
    except ValueError:
        await reply(update, "Enter three valid decimal odds greater than 1.00."); return
    query = " ".join(args[:-3])
    result = await get_analysis(update, type("Args", (), {"args": query.split()})())
    if not result: return
    fixture, d = result
    raw, item, err = await asyncio.to_thread(find_analysis_fixture, query)
    pred = await asyncio.to_thread(build_openmatchday_prediction, raw, item) if not err else {"available": False}
    if not pred.get("available"):
        await reply(update, analysis_header("VALUE CHECK", fixture) + "Model probabilities unavailable. No value verdict can be calculated."); return
    probs = [pred["home_win"], pred["draw"], pred["away_win"]]
    labels = ["Home", "Draw", "Away"]
    lines = [analysis_header("VALUE CHECK", fixture), "Your supplied odds:"]
    for label, odd, p in zip(labels, odds, probs):
        implied = 1 / odd
        ev = p * odd - 1
        fair = 1 / p if p > 0 else float("inf")
        verdict = "MODEL EDGE" if ev > .03 else "NO EDGE"
        lines.append(f"• {label}: odds {odd:.2f} | model {p:.1%} | fair odds {fair:.2f} | EV {ev:+.1%} | {verdict}")
    lines.extend(["", "Odds are user-supplied and have not been independently verified. Model probabilities are uncalibrated estimates; positive EV is not proof of a profitable bet."])
    await reply(update, "\n".join(lines))

def _score_probability(prediction, condition):
    """Probability of a score-grid condition using the existing Poisson goal model."""
    lh = prediction.get("lambda_home")
    la = prediction.get("lambda_away")
    if lh is None or la is None:
        return None
    grid = []
    for hg in range(11):
        for ag in range(11):
            p = poisson_probability(hg, lh) * poisson_probability(ag, la)
            grid.append((hg, ag, p))
    return sum(p for hg, ag, p in grid if condition(hg, ag))


def _normal_market_text(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.+\-:]", " ", str(value or "").casefold())).strip()


def _market_probability_for_row(prediction, row):
    """Estimate supported markets from the scoreline distribution; never invent unavailable data."""
    raw_market = str(row.get("market") or "").casefold()
    raw_selection = str(row.get("selection") or "").casefold()
    if any(token in raw_market for token in ("&", " + ", "combo", "combined")) or " & " in raw_selection or " and " in raw_selection:
        return None, "combined market requires a joint probability model"
    # These markets have settlement rules or time horizons different from a
    # standard full-time scoreline. Never reuse an ordinary 1X2 probability.
    special_market_text = f"{raw_market} {raw_selection}".casefold()
    if any(token in special_market_text for token in (
        "2up", "two up", "early payout", "first half", "1st half",
        "second half", "2nd half", "half time", "half-time",
    )):
        return None, "special or period-specific market has no dedicated PUNTER probability model"
    market = _normal_market_text(row.get("market"))
    selection = _normal_market_text(row.get("selection"))
    specifier = _normal_market_text(row.get("specifier") or row.get("handicap"))
    text = f"{market} {selection} {specifier}".strip()
    lh, la = prediction.get("lambda_home"), prediction.get("lambda_away")
    if lh is None or la is None:
        return None, "model goal rates unavailable"

    def line_from_text(default=None):
        match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", text)
        return float(match.group(1)) if match else default

    # Match result and double chance.
    home_p = prediction.get("home_win")
    draw_p = prediction.get("draw")
    away_p = prediction.get("away_win")
    if any(x in market for x in ("double chance", "1x", "12", "x2")):
        if selection in ("1x", "home or draw", "home/draw"):
            return home_p + draw_p, None
        if selection in ("12", "1 or 2", "home or away", "home/away"):
            return home_p + away_p, None
        if selection in ("x2", "draw or away", "draw/away"):
            return draw_p + away_p, None
    if any(x in market for x in ("draw no bet", "dnb")):
        if selection in ("1", "home", "home win"):
            denom = home_p + away_p
            return (home_p / denom if denom else None), None
        if selection in ("2", "away", "away win"):
            denom = home_p + away_p
            return (away_p / denom if denom else None), None
    if any(x in market for x in ("1x2", "match result", "full time result", "fulltime result", "regular time result")):
        if selection in ("1", "home", "home win"): return home_p, None
        if selection in ("x", "draw", "tie"): return draw_p, None
        if selection in ("2", "away", "away win"): return away_p, None

    # Both teams to score.
    if any(x in market for x in ("both teams", "btts", "team to score")):
        if selection in ("yes", "y"):
            return _score_probability(prediction, lambda h, a: h > 0 and a > 0), None
        if selection in ("no", "n"):
            return _score_probability(prediction, lambda h, a: h == 0 or a == 0), None

    # Exact score / correct score.
    if any(x in market for x in ("correct score", "exact score", "scorecast")):
        match = re.search(r"(\d+)\s*[:\-]\s*(\d+)", selection)
        if match:
            hg, ag = int(match.group(1)), int(match.group(2))
            return _score_probability(prediction, lambda h, a: h == hg and a == ag), None

    # Team total goals.
    team_total = any(x in market for x in ("home team total", "home goals", "team 1 total")) or "home team" in market
    away_total = any(x in market for x in ("away team total", "away goals", "team 2 total")) or "away team" in market
    if team_total or away_total:
        line = line_from_text()
        if line is not None and ("over" in selection or "under" in selection):
            rate_home = team_total
            over = "over" in selection
            return _score_probability(prediction, lambda h, a: ((h if rate_home else a) > line if over else (h if rate_home else a) < line)), None

    # Total match goals, including lines other than 1.5 / 2.5.
    if any(x in market for x in ("total goals", "over under", "goals over", "goals under", "total match", "goal line")):
        line = line_from_text()
        if line is not None and ("over" in selection or "under" in selection):
            over = "over" in selection
            return _score_probability(prediction, lambda h, a: (h + a > line if over else h + a < line)), None

    # Handicap / Asian handicap / European handicap: evaluate the adjusted score.
    if any(x in market for x in ("handicap", "asian handicap", "european handicap")):
        line = line_from_text()
        if line is not None:
            if selection in ("1", "home", "home win"):
                return _score_probability(prediction, lambda h, a: h + line > a), None
            if selection in ("2", "away", "away win"):
                return _score_probability(prediction, lambda h, a: a - line > h), None
            if selection in ("x", "draw"):
                return _score_probability(prediction, lambda h, a: h + line == a), None

    return None, "market requires data not available in the current model (e.g. corners, cards, player or half-time events)"


def _market_probability(prediction, market_key):
    aliases = {
        "home": "home_win", "draw": "draw", "away": "away_win",
        "over15": "over_15", "over25": "over_25",
    }
    key = aliases.get(market_key)
    return prediction.get(key) if key else None


def _odds_market_key(market):
    name = _normal_market_text(market)
    known = ("1x2", "match result", "double chance", "draw no bet", "handicap",
             "asian handicap", "european handicap", "total goals", "over under",
             "both teams", "btts", "correct score", "exact score", "home team",
             "away team", "team total", "corners", "cards", "booking", "player",
             "half time", "first half", "second half", "total match")
    return name if any(term in name for term in known) else None


def _selection_probability_key(market_key, selection, handicap=None):
    return _normal_market_text(selection) or None


def scan_value_candidates(day_filters=("today", "tomorrow"), market_filter="all", bookmaker_only=None, progress_callback=None):
    """Fetch the fixture catalogue and SportyBet feed once, then score requested EAT dates."""
    def progress(message):
        if callable(progress_callback):
            try:
                progress_callback(str(message))
            except Exception:
                logger.debug("Autobuild progress callback failed.", exc_info=True)
    progress("Step 1/4: downloading the fixture catalogue…")
    candidates, diagnostics = [], []
    today = datetime.now(EAT).date()
    target_dates = set()
    for day in day_filters:
        if day == "today":
            target_dates.add(today)
        elif day == "tomorrow":
            target_dates.add(today + timedelta(days=1))
        # The accumulator builder intentionally accepts today's fixtures only.
        # Ignore other date strings rather than silently widening the scan window.
    if not target_dates:
        return [], ["No scan dates were requested."]

    # Keep optional BSD/PitchAPI discovery out of the critical scan path.
    # Those sources still enrich matched candidates later, but cannot block
    # primary fixture retrieval and SportyBet price matching.
    try:
        raw_fixtures = fetch_openmatchday_fixture_rows(1, None, "today")
        all_fixtures = upcoming_fixtures(raw_fixtures)
        progress(f"Step 2/4: fixture catalogue ready ({len(all_fixtures)} upcoming fixtures). Fetching SportyBet prices…")
    except Exception as exc:
        raise RuntimeError(f"Fixture catalogue failed: {type(exc).__name__}: {str(exc)[:180]}") from exc

    fixtures_by_day = {day: [] for day in target_dates}
    for raw, fixture in all_fixtures:
        # upcoming_fixtures() returns normalise_fixture() records; their
        # canonical timestamp field is kickoff_at (naive UTC), not kickoffUtc.
        kickoff = fixture.get("kickoff_at") or fixture.get("kickoffUtc")
        try:
            parsed = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local_day = parsed.astimezone(EAT).date()
        except (TypeError, ValueError):
            continue
        if local_day in fixtures_by_day:
            fixtures_by_day[local_day].append((raw, fixture))

    for day in sorted(target_dates):
        diagnostics.append(f"{day.isoformat()}: {len(fixtures_by_day[day])} upcoming fixture records")

    try:
        if str(bookmaker_only or "").casefold() == "sportybet":
            priced_games = fetch_sportybet_odds(sorted(target_dates), max_pages=30)
            feed_errors = {}
            progress(f"Step 3/4: SportyBet returned {len(priced_games)} priced events. Matching fixtures and checking markets…")
        else:
            # Existing combined provider path remains available to other commands.
            all_games = []
            feed_errors = {}
            for day in sorted(target_dates):
                result = fetch_bookmaker_odds(day)
                for provider in result.get("bookmakers", {}).values():
                    all_games.extend(provider.get("games", []))
                feed_errors.update(result.get("errors", {}))
            priced_games = all_games
    except Exception as exc:
        raise RuntimeError(f"Bookmaker feed failed: {type(exc).__name__}: {str(exc)[:180]}") from exc

    for day in sorted(target_dates):
        day_fixtures = fixtures_by_day[day]
        day_games = []
        for game in priced_games:
            kickoff = game.get("kickoff_eat")
            if str(kickoff or "")[:10] == day.isoformat():
                day_games.append(game)
        # SportyBet is the price source and must also be a fixture-discovery
        # source. The external open-matchday catalogue is incomplete for some
        # dates/competitions; add its unmatched priced events rather than silently
        # excluding them from research. Exact SportyBet names and kickoff are kept;
        # evidence gates still decide whether any selection can qualify.
        existing_pairs = {
            (canonical_team_key(fixture.get("home_team")),
             canonical_team_key(fixture.get("away_team")))
            for _, fixture in day_fixtures
        }
        sportybet_added = 0
        for game in day_games:
            home = str(game.get("home") or "").strip()
            away = str(game.get("away") or "").strip()
            kickoff_text = str(game.get("kickoff_eat") or "").strip()
            if not home or not away or not kickoff_text:
                continue
            pair = (canonical_team_key(home), canonical_team_key(away))
            if pair in existing_pairs:
                continue
            try:
                kickoff_local = datetime.fromisoformat(kickoff_text)
                if kickoff_local.tzinfo is None:
                    kickoff_local = kickoff_local.replace(tzinfo=EAT)
                kickoff_utc = kickoff_local.astimezone(timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError):
                continue
            competition = str(game.get("competition") or "SportyBet competition").strip()
            raw_sportybet = {
                "id": f"sportybet:{game.get('event_id') or pair[0] + ':' + pair[1]}",
                "home": home, "away": away,
                "kickoffUtc": kickoff_utc.isoformat(),
                "matchStatus": "Scheduled",
                "leagueName": competition, "countryName": "",
                "leagueShortcut": "__sportybet_discovery__",
            }
            fixture_sportybet = {
                "provider_match_id": raw_sportybet["id"],
                "home_team": home, "away_team": away,
                "kickoff_at": kickoff_utc,
                "competition": competition, "country": "",
            }
            day_fixtures.append((raw_sportybet, fixture_sportybet))
            existing_pairs.add(pair)
            sportybet_added += 1
        diagnostics.append(
            f"{day.isoformat()}: {len(day_fixtures)} research fixtures after adding "
            f"{sportybet_added} unmatched SportyBet-native events; {len(day_games)} SportyBet events retrieved"
        )
        matched_events = 0
        unsupported = set()
        prediction_unavailable = 0
        history_fallback_attempted = 0
        history_fallback_recovered = 0
        history_failure_reasons = Counter()
        market_rows_seen = 0
        # Resolve live SportyBet matches first, then prefetch history only for
        # the leagues that actually have matched events. Do this concurrently:
        # serial per-league HTTP requests were a major cold-start bottleneck.
        historical_cache = {}
        matched_fixtures = []
        history_folders = set()
        for raw, fixture in day_fixtures:
            matches = find_bookmaker_event(day_games, fixture["home_team"], fixture["away_team"])
            if not matches:
                continue
            matched_fixtures.append((raw, fixture, matches))
            folder = str(raw.get("leagueShortcut") or "").strip()
            if folder:
                history_folders.add(folder)

        def fetch_history(folder):
            try:
                return folder, fetch_openmatchday_results_history(folder)
            except Exception as exc:
                logger.warning("Autobuild history fetch failed for %s: %s", folder, exc)
                return folder, []

        if history_folders:
            with ThreadPoolExecutor(max_workers=min(12, len(history_folders))) as history_pool:
                history_futures = [history_pool.submit(fetch_history, folder) for folder in sorted(history_folders)]
                for future in as_completed(history_futures):
                    folder, history_rows = future.result()
                    historical_cache[folder] = history_rows

        progress(f"Step 4/4: checking {len(matched_fixtures)} SportyBet-matched fixtures against {len(day_games)} priced events for {day.isoformat()}…")
        for fixture_index, (raw, fixture, matches) in enumerate(matched_fixtures, 1):
            home_name, away_name = fixture["home_team"], fixture["away_team"]
            progress(f"{day.isoformat()}: fixture {fixture_index}/{len(day_fixtures)} — matching {home_name} vs {away_name}…")
            matched_events += len(matches)
            folder = str(raw.get("leagueShortcut") or "").strip()
            local_history = historical_cache.get(folder, [])
            try:
                prediction = build_openmatchday_prediction(
                    raw, fixture, historical_rows=local_history,
                    as_of_date=fixture.get("kickoff_at"),
                    model_variant="venue_weighted"
                )
                # Retry every unavailable prediction with cross-competition
                # team history, not only the exact "Insufficient recent results"
                # message. A local results feed can be empty, stale, or malformed
                # while the teams still have usable results in another competition.
                # Keep the fixture's own competition rows for league baseline,
                # H2H and Elo; broaden only the two teams' recent-form samples.
                if not prediction.get("available") and folder:
                    history_fallback_attempted += 1
                    original_reason = str(prediction.get("reason") or "unknown model-history failure")
                    try:
                        cross_competition_rows = fetch_openmatchday_team_history_rows()
                        fallback_prediction = build_openmatchday_prediction(
                            raw, fixture, historical_rows=local_history,
                            as_of_date=fixture.get("kickoff_at"),
                            team_history_rows=cross_competition_rows,
                            model_variant="venue_weighted"
                        )
                        if fallback_prediction.get("available"):
                            prediction = fallback_prediction
                            history_fallback_recovered += 1
                        else:
                            history_failure_reasons[original_reason[:100]] += 1
                            history_failure_reasons[str(fallback_prediction.get("reason") or "cross-competition history insufficient")[:100]] += 1
                    except Exception as fallback_exc:
                        history_failure_reasons[
                            f"cross-competition fallback error: {type(fallback_exc).__name__}"[:100]
                        ] += 1
                        logger.warning(
                            "Cross-competition history fallback failed for %s vs %s: %s",
                            home_name, away_name, fallback_exc
                        )
            except Exception as exc:
                prediction_unavailable += 1
                logger.exception("Autobuild prediction failed for %s vs %s", home_name, away_name)
                progress(f"{day.isoformat()}: prediction error for {home_name} vs {away_name} ({type(exc).__name__}).")
                continue
            if not prediction.get("available"):
                prediction_unavailable += 1
                # Keep the exact SportyBet fixture in the research pool. BSD can
                # independently supply supported 1X2, goal-total and corner
                # probabilities even when verified history is still unavailable.
                prediction = {}
            progress(f"{day.isoformat()}: matched {home_name} vs {away_name}; evaluating {sum(len(g.get('markets') or []) for g in matches)} market rows…")
            for game in matches:
                for row in game.get("markets", []):
                    market_rows_seen += 1
                    market_name = str(row.get("market") or "Unknown")
                    selection_name = str(row.get("selection") or "Unknown")
                    market_text_for_sources = _normal_market_text(
                        market_name + " " + selection_name + " " + str(row.get("specifier") or row.get("handicap") or "")
                    )
                    # These exclusions apply at scan time as well as in the
                    # research gate and accumulator engine.
                    if ("btts" in market_text_for_sources or
                            "both teams to score" in market_text_for_sources or
                            ("under" in market_text_for_sources and "2.5" in market_text_for_sources) or
                            "draw no bet" in market_text_for_sources or
                            re.search(r"(?<![a-z])dnb(?![a-z])", market_text_for_sources) or
                            (any(term in market_text_for_sources for term in ("total goals", "over under", "goals over", "goals under", "total match", "goal line", "asian handicap", "handicap"))
                             and bool(re.search(r"(?<!\d)\d+\.0+(?!\d)|(?<!\d)\d+(?![\d.])", market_text_for_sources))) or
                            "2up" in market_text_for_sources or
                            "early payout" in market_text_for_sources):
                        continue
                    probability, reason = _market_probability_for_row(prediction, row)
                    specialist_market = any(term in market_text_for_sources
                        for term in ("corner", "card", "booking", "total shots", "shots on target",
                                     "shots on goal", "first half", "half time", "1st half"))
                    external_market_supported = any(term in market_text_for_sources
                        for term in ("1x2", "match result", "full time result", "double chance",
                                     "draw no bet", "dnb", "total goals", "over under", "goals over",
                                     "goals under", "total corners", "corner"))
                    if probability is None and not specialist_market and not external_market_supported:
                        if reason:
                            unsupported.add(market_name)
                        continue
                    if market_filter not in ("all", "safest"):
                        aliases = {
                            "btts": "both teams to score",
                            "goals": "total goals",
                            "totals": "total goals",
                            "dnb": "draw no bet",
                            "dc": "double chance",
                            "correctscore": "correct score",
                            "ht": "half time",
                        }
                        wanted_filters = [
                            aliases.get(_normal_market_text(value), _normal_market_text(value))
                            for value in str(market_filter).split(",") if value.strip()
                        ]
                        market_text = _normal_market_text(market_name)
                        selection_text = _normal_market_text(selection_name)
                        if wanted_filters and not any(
                            wanted in market_text or wanted in selection_text
                            for wanted in wanted_filters
                        ):
                            continue
                    odds = row.get("odds")
                    if not odds or odds <= 1:
                        continue
                    if probability is not None and (probability <= 0 or probability >= 1):
                        continue
                    ev = probability * odds - 1 if probability is not None else None
                    candidates.append({
                        "home": fixture["home_team"], "away": fixture["away_team"],
                        "league": fixture.get("competition") or fixture.get("leagueName") or "Competition",
                        "country": fixture.get("country") or fixture.get("countryName") or "",
                        "day": day.isoformat(), "bookmaker": game.get("bookmaker"),
                        "market": market_name, "selection": selection_name,
                        "market_key": _normal_market_text(market_name) + "|" + _normal_market_text(selection_name),
                        "odds": odds, "probability": probability, "ev": ev,
                        "fair_odds": (1 / probability) if probability is not None else None,
                        "model_confidence": prediction.get("confidence"),
                        "model_version": prediction.get("model_version") or "venue-weighted-poisson-elo-mc-v1",
                        "probability_source": "PUNTER venue-weighted Poisson scoreline model" if probability is not None else None,
                        "home_form": prediction.get("home_form"),
                        "away_form": prediction.get("away_form"),
                        "home_recent_matches": prediction.get("home_recent_matches") or [],
                        "away_recent_matches": prediction.get("away_recent_matches") or [],
                        "home_sample": prediction.get("home_results"),
                        "away_sample": prediction.get("away_results"),
                        "home_scored_avg": prediction.get("home_scored"),
                        "home_conceded_avg": prediction.get("home_conceded"),
                        "away_scored_avg": prediction.get("away_scored"),
                        "away_conceded_avg": prediction.get("away_conceded"),
                        "home_home_sample": prediction.get("home_home_sample"),
                        "away_away_sample": prediction.get("away_away_sample"),
                        "home_home_gf": prediction.get("home_home_gf"),
                        "home_home_ga": prediction.get("home_home_ga"),
                        "away_away_gf": prediction.get("away_away_gf"),
                        "away_away_ga": prediction.get("away_away_ga"),
                        "h2h_count": prediction.get("h2h_count", 0),
                        "h2h_results": prediction.get("h2h_results", []),
                        "lambda_home": prediction.get("lambda_home"),
                        "lambda_away": prediction.get("lambda_away"),
                        "elo_home_rating": prediction.get("elo_home_rating"),
                        "elo_away_rating": prediction.get("elo_away_rating"),
                        "elo_matches_used": prediction.get("elo_matches_used"),
                        "monte_carlo": prediction.get("monte_carlo"),
                        "kickoff": game.get("kickoff_eat") or fixture.get("kickoffUtc"),
                        "url": game.get("url"),
                        "event_id": game.get("event_id"),
                        "market_id": row.get("market_id"),
                        "outcome_id": row.get("outcome_id"),
                        "specifier": row.get("specifier") or row.get("handicap"),
                    })
        diagnostics.append(
            f"{day.isoformat()}: {matched_events} matched SportyBet events; "
            f"{market_rows_seen} market rows inspected; {len(candidates)} total candidate rows accumulated; "
            f"{prediction_unavailable} matched fixtures lacked usable model history"
        )
        if history_fallback_attempted:
            diagnostics.append(
                f"{day.isoformat()}: cross-competition history fallback attempted for "
                f"{history_fallback_attempted} fixtures; recovered {history_fallback_recovered}"
            )
        if history_failure_reasons:
            diagnostics.append(
                "History fallback details: " + "; ".join(
                    f"{reason} ({count})"
                    for reason, count in history_failure_reasons.most_common(4)
                )
            )
        if not matched_events:
            diagnostics.append(f"{day.isoformat()}: no confidently matched SportyBet events")
        if unsupported:
            diagnostics.append("Markets retrieved but not model-supported yet: " + ", ".join(sorted(unsupported)[:12]))

    try:
    # Silently enrich exact SportyBet fixture matches with free BSD and PitchAPI
    # evidence. External probabilities are cross-checks; exact BSD-supported
    # result, goal-total and corner markets may supply missing probabilities.
    # SportyBet prices remain the executable prices used by the accumulator.
        candidates, free_source_stats = enrich_free_football_sources(candidates, max_fixtures=20)
        logger.info("Free football source enrichment: %s", free_source_stats)
        diagnostics.append(
            "Free-source coverage: "
            f"BSD={'on' if free_source_stats.get('bsd_enabled') else 'off'}, "
            f"PitchAPI={'on' if free_source_stats.get('pitchapi_enabled') else 'off'}, "
            f"fixtures_enriched={free_source_stats.get('fixtures_enriched', 0)}, "
            f"BSD_matches={free_source_stats.get('bsd_matches', 0)}, "
            f"PitchAPI_matches={free_source_stats.get('pitchapi_matches', 0)}, "
            f"external_probabilities_added={free_source_stats.get('external_probabilities_added', 0)}"
            + (f"; {free_source_stats.get('reason')}" if free_source_stats.get('reason') else "")
        )
        if free_source_stats.get("errors"):
            diagnostics.extend("Free source: " + str(x)[:160] for x in free_source_stats["errors"][:5])
    except Exception as source_error:
        logger.warning("Optional free football source enrichment failed: %s", source_error)
        free_source_stats = {"error": f"{type(source_error).__name__}: {str(source_error)[:160]}"}

    for name, error in feed_errors.items():
        diagnostics.append(f"{name}: {str(error)[:100]}")
    candidates.sort(key=lambda x: (x["ev"] if x.get("ev") is not None else -1.0,
                                   x["probability"] if x.get("probability") is not None else 0.0), reverse=True)
    best = {}
    for item in candidates:
        key = (item["home"], item["away"], item["market_key"])
        if key not in best or item["odds"] > best[key]["odds"]:
            best[key] = item
    return sorted(best.values(), key=lambda x: (x["ev"] if x["ev"] is not None else -1.0, x["probability"] if x["probability"] is not None else 0.0), reverse=True), diagnostics


async def topvalue_command(update, context):
    args = [x.casefold() for x in (context.args or [])]
    market = args[0] if args and args[0] not in ("today", "tomorrow") else "all"
    day_arg = next((x for x in args if x in ("today", "tomorrow")), "both")
    day = day_arg
    days = (day,) if day != "both" else ("today", "tomorrow")
    try:
        rows, diagnostics = await asyncio.to_thread(scan_value_candidates, days, market)
    except Exception as exc:
        logger.exception("Value scanner failed")
        await reply(update, f"❌ VALUE SCAN FAILED: {type(exc).__name__}: {str(exc)[:250]}")
        return
    if market == "safest":
        rows.sort(key=lambda x: (x["probability"], x["ev"]), reverse=True)
    if not rows:
        await reply(update,
            "⚠️ NO MODEL-SUPPORTED, MATCHED PRICES FOUND\n\n"
            "No responsible ranked pick is available because fixture, model or bookmaker data could not be matched.\n\n"
            + ("Feed notes: " + "; ".join(diagnostics[:6]) if diagnostics else "No usable bookmaker prices were matched."))
        return
    lines = ["🧠 PUNTER KENYA — RANKED PREDICTIONS", f"Market: {market.upper()} | Dates: {', '.join(days)}",
             "Positive EV is a value indicator, not a condition for showing a prediction.", ""]
    for n, item in enumerate(rows[:12], 1):
        lines.append(f"{n}. {item['home']} vs {item['away']} — {item['league']}")
        lines.append(f"   {item['market']}: {item['selection']} @ {item['odds']:.2f} ({item['bookmaker']})")
        value_label = "POSITIVE ESTIMATED VALUE" if item["ev"] > 0 else "NEGATIVE ESTIMATED VALUE"
        lines.append(f"   Model: {item['probability']:.1%} | Fair odds: {item['fair_odds']:.2f} | Est. EV: {item['ev']:+.1%} ({value_label})")
        lines.append(f"   Kick-off: {item['kickoff'] or 'Not supplied'}")
        if item.get("url"): lines.append(f"   {item['url']}")
        lines.append("")
    lines.append("Important: forecasts remain experimental and are not historically calibrated. Historical results are not xG or lineup-adjusted. Estimated EV is not proof of an edge. Verify prices and rules. Market availability depends on the bookmaker feed and model evidence.")
    message = "\n".join(lines)
    for offset in range(0, len(message), 3600):
        await reply(update, message[offset:offset+3600])


async def aipicks_status_command(update, context):
    # Reuse the same transparent catalogue + price scan; no separate unverified AI claim.
    context.args = list(context.args or [])
    await topvalue_command(update, context)


def ensure_prediction_records_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS punter_prediction_records (
            id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            fixture_key VARCHAR(500) NOT NULL UNIQUE,
            league_shortcut VARCHAR(120) NOT NULL,
            match_date DATE NOT NULL,
            home_team VARCHAR(180) NOT NULL,
            away_team VARCHAR(180) NOT NULL,
            p_home DECIMAL(9,6) NOT NULL,
            p_draw DECIMAL(9,6) NOT NULL,
            p_away DECIMAL(9,6) NOT NULL,
            p_over15 DECIMAL(9,6) NOT NULL,
            p_over25 DECIMAL(9,6) NOT NULL,
            predicted_home_goals INT NOT NULL,
            predicted_away_goals INT NOT NULL,
            actual_home_goals INT NULL,
            actual_away_goals INT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            settled_at TIMESTAMP NULL,
            INDEX idx_prediction_status (status),
            INDEX idx_prediction_league (league_shortcut)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def save_prediction_record(raw_fixture, fixture, prediction):
    connection = get_connection()
    try:
        kickoff = fixture.get("kickoff_at")
        if not kickoff:
            raise RuntimeError("Fixture kickoff date is missing.")
        match_date = kickoff.replace(tzinfo=timezone.utc).astimezone(EAT).date()
        folder = str(raw_fixture.get("leagueShortcut") or "").strip()
        if not folder:
            raise RuntimeError("Competition reference is missing.")
        key = "|".join([
            folder, match_date.isoformat(),
            canonical_team_key(fixture["home_team"]),
            canonical_team_key(fixture["away_team"]),
        ])
        with connection.cursor() as cursor:
            ensure_prediction_records_table(cursor)
            cursor.execute("""
                INSERT IGNORE INTO punter_prediction_records
                (fixture_key, league_shortcut, match_date, home_team, away_team,
                 p_home, p_draw, p_away, p_over15, p_over25,
                 predicted_home_goals, predicted_away_goals)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                key, folder, match_date, fixture["home_team"], fixture["away_team"],
                prediction["home_win"], prediction["draw"], prediction["away_win"],
                prediction["over_15"], prediction["over_25"],
                prediction["score"][0], prediction["score"][1],
            ))
        return True
    finally:
        connection.close()


def refresh_and_read_prediction_records():
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            ensure_prediction_records_table(cursor)
            cursor.execute("""
                SELECT id, league_shortcut, match_date, home_team, away_team,
                       p_home, p_draw, p_away, p_over15, p_over25,
                       actual_home_goals, actual_away_goals, status
                FROM punter_prediction_records
                ORDER BY match_date DESC
            """)
            records = cursor.fetchall()

        pending_by_league = {}
        for record in records:
            if record["status"] == "PENDING":
                pending_by_league.setdefault(record["league_shortcut"], []).append(record)

        settled_now = 0
        for folder, pending in pending_by_league.items():
            try:
                results = fetch_openmatchday_csv(folder, "results")
            except Exception as exc:
                logger.warning("Could not refresh prediction records for %s: %s", folder, exc)
                continue
            for record in pending:
                for row in results:
                    if str(row.get("date") or "").strip() != record["match_date"].isoformat():
                        continue
                    if not (team_name_matches_query(record["home_team"], str(row.get("home") or ""))
                            and team_name_matches_query(record["away_team"], str(row.get("away") or ""))):
                        continue
                    try:
                        hg, ag = int(row.get("ft_home")), int(row.get("ft_away"))
                    except (TypeError, ValueError):
                        continue
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            UPDATE punter_prediction_records
                            SET actual_home_goals=%s, actual_away_goals=%s,
                                status='SETTLED', settled_at=CURRENT_TIMESTAMP
                            WHERE id=%s AND status='PENDING'
                        """, (hg, ag, record["id"]))
                    record["actual_home_goals"] = hg
                    record["actual_away_goals"] = ag
                    record["status"] = "SETTLED"
                    settled_now += 1
                    break

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, league_shortcut, match_date, home_team, away_team,
                       p_home, p_draw, p_away, p_over15, p_over25,
                       actual_home_goals, actual_away_goals, status
                FROM punter_prediction_records
                ORDER BY match_date DESC
            """)
            records = cursor.fetchall()
        return records, settled_now
    finally:
        connection.close()


async def record_command(update, context):
    await reply(update, "📚 Updating PUNTER's prediction history from completed competition results...")
    try:
        records, settled_now = await asyncio.to_thread(refresh_and_read_prediction_records)
        settled = [r for r in records if r["status"] == "SETTLED"]
        pending = [r for r in records if r["status"] == "PENDING"]
        if not settled:
            await reply(update,
                "📊 PUNTER PERFORMANCE RECORD\n\n"
                f"Predictions stored: {len(records)}\nNewly settled: {settled_now}\nPending: {len(pending)}\n"
                "Settled predictions: 0\n\n"
                "No accuracy or profitability claim is possible yet. Run /deepdive on upcoming fixtures to save model forecasts. Results are matched against the competition's completed-results CSV.")
            return
        correct = 0
        brier_total = 0.0
        over15_correct = 0
        over25_correct = 0
        for r in settled:
            hg, ag = int(r["actual_home_goals"]), int(r["actual_away_goals"])
            actual = 0 if hg > ag else 1 if hg == ag else 2
            probs = [float(r["p_home"]), float(r["p_draw"]), float(r["p_away"])]
            predicted = max(range(3), key=lambda i: probs[i])
            correct += predicted == actual
            brier_total += sum((probs[i] - (1.0 if i == actual else 0.0)) ** 2 for i in range(3))
            over15_correct += (float(r["p_over15"]) >= .5) == (hg + ag >= 2)
            over25_correct += (float(r["p_over25"]) >= .5) == (hg + ag >= 3)
        n = len(settled)
        await reply(update,
            "📊 PUNTER PERFORMANCE RECORD\n\n"
            f"Forecasts stored: {len(records)}\nSettled: {n} | Pending: {len(pending)}\n"
            f"Newly settled this check: {settled_now}\n\n"
            f"1X2 top-outcome accuracy: {correct/n:.1%} ({correct}/{n})\n"
            f"1X2 Brier score: {brier_total/n:.3f} (lower is better; 0 is perfect)\n"
            f"Over 1.5 classification accuracy: {over15_correct/n:.1%}\n"
            f"Over 2.5 classification accuracy: {over25_correct/n:.1%}\n\n"
            "This measures model forecasts, not betting profit. No bookmaker odds or stake records are stored, so ROI and profit/loss are not calculated. Small samples are unreliable.")
    except Exception as exc:
        logger.exception("Prediction performance report failed")
        await reply(update,
            "⚠️ PREDICTION TRACKER UNAVAILABLE\n\n"
            f"{str(exc)[:500]}\n\n"
            "The tracker needs database table-creation/write permission. Existing match analysis remains available.")


async def deepdive_command(update, context):
    result = await get_analysis(update, context)
    if not result: return
    fixture, d = result
    h, a = d["home_metrics"], d["away_metrics"]
    raw, item, err = await asyncio.to_thread(find_analysis_fixture, " vs ".join([fixture["home_team"], fixture["away_team"]]))
    pred = await asyncio.to_thread(build_openmatchday_prediction, raw, item) if not err else {"available": False}
    edge = "Home" if h["points"] > a["points"] + .25 else "Away" if a["points"] > h["points"] + .25 else "No clear"
    lines = [analysis_header("FULL MATCH DEEPDIVE", fixture),
        "1. RECENT FORM",
        f"Home: {h['form']} | {h['points']:.2f} points/match",
        f"Away: {a['form']} | {a['points']:.2f} points/match",
        "",
        "2. ATTACK & DEFENCE",
        f"Home GF/GA: {h['gf']:.2f}/{h['ga']:.2f} | scored in {h['scored_rate']:.0%}",
        f"Away GF/GA: {a['gf']:.2f}/{a['ga']:.2f} | scored in {a['scored_rate']:.0%}",
        "",
        "3. VENUE PERFORMANCE",
        f"Home team at home: {d['home_at_home']['n']} matches" if d["home_at_home"] else "Home team at home: insufficient sample",
        f"Away team away: {d['away_away']['n']} matches" if d["away_away"] else "Away team away: insufficient sample",
        "",
        "4. CONSISTENCY",
        f"Home: {h['consistency']:.0f}/100 | Away: {a['consistency']:.0f}/100",
        "5. HEAD-TO-HEAD",
        f"Available meetings: {len(d['h2h'])}",
        "",
        "6. MODEL FORECAST"]
    decision_text = "No betting decision: the statistical forecast is unavailable."
    if pred.get("available"):
        try:
            await asyncio.to_thread(save_prediction_record, raw, item, pred)
            lines.append("Forecast saved to the historical performance tracker.")
        except Exception as exc:
            logger.warning("Could not save prediction record: %s", exc)
            lines.append("Forecast tracking unavailable: database write access is required.")
        lines.extend([f"Expected goals: {pred['lambda_home']:.2f}-{pred['lambda_away']:.2f}",
            f"Most likely score: {pred['score'][0]}-{pred['score'][1]}",
            f"1X2: Home {pred['home_win']:.1%} | Draw {pred['draw']:.1%} | Away {pred['away_win']:.1%}",
            f"Over 1.5: {pred['over_15']:.1%} | Over 2.5: {pred['over_25']:.1%}"])
        # Check actual bookmaker feeds for this exact fixture before deciding.
        try:
            kickoff = parse_datetime(fixture.get("kickoff_at"))
            if kickoff and kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            match_day = kickoff.astimezone(EAT).date() if kickoff else datetime.now(EAT).date()
            odds_result = await asyncio.to_thread(fetch_bookmaker_odds, match_day)
            priced_games = []
            for provider in (odds_result.get("bookmakers") or {}).values():
                priced_games.extend(provider.get("games") or [])
            matched_games = await asyncio.to_thread(
                find_bookmaker_event, priced_games,
                fixture["home_team"], fixture["away_team"]
            )
            exact_games = [
                game for game in matched_games
                if " ".join(str(game.get("home") or "").casefold().split()) == " ".join(fixture["home_team"].casefold().split())
                and " ".join(str(game.get("away") or "").casefold().split()) == " ".join(fixture["away_team"].casefold().split())
            ]
            matched_games = exact_games or matched_games
            evaluated = []
            unsupported_markets = set()
            for game in matched_games:
                for market_row in game.get("markets") or []:
                    probability, reason = _market_probability_for_row(pred, market_row)
                    if probability is None:
                        if reason:
                            unsupported_markets.add(str(market_row.get("market") or "Unknown"))
                        continue
                    price = market_row.get("odds")
                    try:
                        price = float(price)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not (price > 1.0 and 0 < probability < 1):
                        continue
                    evaluated.append({
                        "bookmaker": game.get("bookmaker") or "Bookmaker",
                        "market": market_row.get("market") or "Market",
                        "selection": market_row.get("selection") or "Selection",
                        "odds": price, "probability": probability,
                        "fair_odds": 1 / probability, "ev": probability * price - 1,
                    })
            evaluated.sort(key=lambda row: row["ev"], reverse=True)
            if evaluated:
                lines.extend(["", "7. LIVE BOOKMAKER MARKET CHECK",
                    f"Matched bookmaker feeds: {len({row['bookmaker'] for row in evaluated})}",
                    f"Model-supported priced outcomes evaluated: {len(evaluated)}"])
                for row in evaluated[:8]:
                    lines.append(
                        f"{row['bookmaker']} | {row['market']}: {row['selection']} @ {row['odds']:.2f} "
                        f"| Model {row['probability']:.1%} | Fair {row['fair_odds']:.2f} "
                        f"| Raw estimated EV {row['ev']:+.1%}"
                    )
                best = evaluated[0]
                decision_text = (
                    f"Best provisional comparison: {best['market']} — {best['selection']} "
                    f"at {best['odds']:.2f} ({best['bookmaker']}), raw estimated EV "
                    f"{best['ev']:+.1%}. This is NOT a proven value bet: the model is "
                    "uncalibrated and this calculation does not remove bookmaker margin."
                )
            elif matched_games:
                decision_text = (
                    "Bookmaker fixtures were found, but none of their returned outcomes "
                    "could be responsibly evaluated by the current statistical model. "
                    "Unsupported markets are not assigned invented probabilities."
                )
            else:
                feed_errors = "; ".join(
                    f"{name}: {str(error)[:100]}"
                    for name, error in (odds_result.get("errors") or {}).items()
                )
                decision_text = (
                    "No matching live bookmaker fixture was returned for this match. "
                    "No price comparison or value verdict is possible."
                    + (f" Feed diagnostics: {feed_errors}" if feed_errors else "")
                )
            if unsupported_markets:
                lines.append("Markets retrieved but not model-supported: " + ", ".join(sorted(unsupported_markets)[:12]))
        except Exception as exc:
            logger.warning("Deepdive bookmaker check failed: %s", exc)
            decision_text = f"Bookmaker comparison failed ({type(exc).__name__}). No value verdict is available."
    else:
        lines.append("Forecast withheld: " + pred.get("reason", "insufficient data"))
    lines.extend(["", "8. PLAIN-ENGLISH SUMMARY",
        f"{edge} recent-points advantage. " +
        ("The data shows a measurable difference, but it is not enough on its own to justify a bet." if edge != "No clear" else "Recent points rates are close; this matchup has no clear form leader."),
        "",
        "9. BETTING DECISION",
        decision_text,
        "",
        "DATA QUALITY & LIMITATIONS",
        "Uses up to 10 recent completed results in this competition. No opponent-strength adjustment, xG, confirmed lineups, injuries, suspensions, half-time data or live events. The model is not yet historically calibrated. Estimates are not guarantees."])
    message = "\n".join(lines)
    for start in range(0, len(message), 3800):
        await reply(update, message[start:start+3800])


# ============================================================
# MATCH INTELLIGENCE — VERIFIED MATCH DATA
# ============================================================

def unwrap_api_data(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def as_named_text(value):
    if isinstance(value, dict):
        return str(first_value(
            value, "name", "playerName", "lineupPlayer",
            "teamName", "title", "displayName"
        ) or "")
    if value is None:
        return ""
    return str(value)


def get_collection(value, *keys):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
    return []


def extract_match_statistics(payload):
    data = unwrap_api_data(payload)
    if isinstance(data, dict):
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else data
        match_stats = stats.get("match") if isinstance(stats, dict) else None
        if isinstance(match_stats, dict):
            return (
                get_collection(match_stats, "fullTime", "full_time")
                or get_collection(match_stats, "statistics", "stats")
            )
        return get_collection(stats, "statistics", "stats", "fullTime", "full_time")
    return get_collection(data)


def format_match_lineups(payload):
    data = unwrap_api_data(payload)
    if not isinstance(data, dict):
        return ["Lineups: not published by the data provider yet."]

    lines = []
    for side_name, side_keys in (
        ("HOME", ("home", "homeTeam", "homeLineup")),
        ("AWAY", ("away", "awayTeam", "awayLineup")),
    ):
        side = None
        for key in side_keys:
            if isinstance(data.get(key), dict):
                side = data[key]
                break
        if side is None:
            continue

        formation = first_value(
            side, "formation", "teamFormation"
        ) or first_value(
            data, "homeFormation" if side_name == "HOME" else "awayFormation"
        )
        starters = get_collection(
            side, "startingLineups", "startingLineup", "startingXI", "starting_xi"
        )
        substitutes = get_collection(
            side, "substitutes", "substitutePlayers", "bench"
        )

        lines.append(f"{side_name} XI" + (f" — {formation}" if formation else ""))
        if starters:
            def lineup_order(player):
                return safe_int(first_value(player, "lineupPosition", "positionOrder")) or 999
            for player in sorted(starters, key=lineup_order):
                name = as_named_text(first_value(
                    player, "lineupPlayer", "playerName", "name", "player"
                ))
                number = first_value(player, "lineupNumber", "shirtNumber", "number")
                position = first_value(player, "playerPosition", "position")
                details = []
                if number is not None:
                    details.append(f"#{number}")
                if position:
                    details.append(str(position))
                lines.append("• " + (name or "Player unavailable") +
                             (f" ({', '.join(details)})" if details else ""))
        else:
            lines.append("• Starting XI not published yet.")

        if substitutes:
            names = [
                as_named_text(first_value(p, "lineupPlayer", "playerName", "name", "player"))
                for p in substitutes
            ]
            names = [name for name in names if name]
            if names:
                lines.append("Bench: " + ", ".join(names[:12]))
        lines.append("")

    if not lines:
        return ["Lineups: not published by the data provider yet."]
    return lines


def format_match_odds(payload):
    data = unwrap_api_data(payload)
    rows = get_collection(data, "odds", "bookmakers", "items", "results")
    if isinstance(data, dict) and not rows:
        rows = [data] if any(k in data for k in ("odd1", "oddX", "odd2")) else []

    lines = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        bookmaker = first_value(row, "bookmaker", "bookmakerName", "name") or "Bookmaker"
        prices = []
        for key, label in (("odd1", "1"), ("oddX", "X"), ("odd2", "2")):
            value = row.get(key)
            if value not in (None, ""):
                prices.append(f"{label}: {value}")
        if prices:
            lines.append(f"• {bookmaker} — " + " | ".join(prices))
        over_under = row.get("overUnder")
        if isinstance(over_under, dict):
            selected = [
                f"{key}: {value}" for key, value in list(over_under.items())[:4]
                if value not in (None, "")
            ]
            if selected:
                lines.append("  O/U — " + " | ".join(selected))
    return lines


def format_match_events(payload):
    data = unwrap_api_data(payload)
    rows = get_collection(data, "events", "items", "results")
    lines = []
    for event in rows[:8]:
        if not isinstance(event, dict):
            continue
        minute = first_value(event, "minute", "time", "elapsed")
        kind = first_value(event, "type", "eventType", "detail")
        player = first_value(event, "playerName", "player", "scorer", "name")
        if kind or player:
            lines.append(
                "• " + (f"{minute}' " if minute is not None else "")
                + " — ".join(str(x) for x in (kind, as_named_text(player)) if x)
            )
    return lines


def format_stat_rows(rows):
    lines = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        label = first_value(row, "type", "name", "statistic", "label")
        home = first_value(row, "home", "homeValue", "homeTeam")
        away = first_value(row, "away", "awayValue", "awayTeam")
        if label is not None and (home is not None or away is not None):
            lines.append(f"• {label}: {home if home is not None else '—'} – {away if away is not None else '—'}")
    return lines


async def matchintel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Usage: /matchintel HOME TEAM vs AWAY TEAM
    Finds a scheduled fixture and displays provider-backed match data.
    """
    query = " ".join(context.args).strip()
    if not query:
        await reply(
            update,
            "⚽ MATCH INTELLIGENCE\n\n"
            "Usage: /matchintel HOME TEAM vs AWAY TEAM\n"
            "Example: /matchintel Kaedi vs SNIM\n\n"
            "The bot finds the upcoming fixture and checks match details, "
            "lineups, statistics and available odds.",
        )
        return

    await reply(update, "🔎 Finding the fixture and checking available match data...")

    try:
        normal_query = query.casefold().replace(" v ", " vs ")
        if " vs " in normal_query:
            home_query, away_query = [part.strip() for part in normal_query.split(" vs ", 1)]
        else:
            home_query, away_query = normal_query, ""

        def find_candidates(raw_rows):
            found = []
            for raw in raw_rows:
                item = normalise_fixture(raw)
                if not item:
                    continue
                home = item["home_team"]
                away = item["away_team"]
                if away_query:
                    if (
                        team_name_matches_query(home_query, home)
                        and team_name_matches_query(away_query, away)
                    ):
                        found.append((raw, item))
                    elif (
                        team_name_matches_query(home_query, away)
                        and team_name_matches_query(away_query, home)
                    ):
                        found.append((raw, item))
                elif home_query and (
                    team_name_matches_query(home_query, home)
                    or team_name_matches_query(home_query, away)
                ):
                    found.append((raw, item))
            return found

        # Fast path: one paginated date-range query. Only run the heavier
        # date-by-date catalogue scan if the range endpoint fails or finds no match.
        try:
            raw_rows = await asyncio.wait_for(
                asyncio.to_thread(fetch_matchintel_fixture_rows, 14),
                timeout=20,
            )
            candidates = find_candidates(raw_rows)
        except asyncio.TimeoutError:
            logger.warning("Fast matchintel fixture range search timed out.")
            raw_rows = []
            candidates = []
        except Exception as fast_search_error:
            logger.warning(
                "Fast matchintel fixture range search failed: %s",
                fast_search_error,
            )
            raw_rows = []
            candidates = []

        if not candidates:
            await reply(
                update,
                "🔎 Checking the full date-by-date fixture feed...",
            )
            try:
                raw_rows = await asyncio.wait_for(
                    asyncio.to_thread(fetch_upcoming_fixture_rows, 14),
                    timeout=30,
                )
                candidates = find_candidates(raw_rows)
            except asyncio.TimeoutError:
                await reply(
                    update,
                    "⏱ The fixture provider did not respond in time. "
                    "Try again shortly; Telegram is still connected.",
                )
                return

        if not candidates:
            valid_rows = []
            for raw_fixture in raw_rows:
                parsed_fixture = normalise_fixture(raw_fixture)
                if parsed_fixture:
                    valid_rows.append(parsed_fixture)

            sample_lines = [
                f"• {row['home_team']} vs {row['away_team']}"
                for row in valid_rows[:8]
            ]
            diagnostic = (
                "\n\nProvider fixtures received: "
                + str(len(raw_rows))
                + "\nFixtures successfully parsed: "
                + str(len(valid_rows))
            )
            if sample_lines:
                diagnostic += "\n\nSample fixtures returned:\n" + "\n".join(sample_lines)
            else:
                diagnostic += (
                    "\n\nThe provider returned no fixtures the bot could parse. "
                    "This points to an API response/coverage issue, not your team spelling."
                )

            await reply(
                update,
                "❌ The requested fixture was not found in the provider feed."
                + diagnostic
                + "\n\nUse /fixtures to compare the provider's current fixture list.",
            )
            return

        if len(candidates) > 1:
            lines = ["Several fixtures match. Narrow the search with both team names:", ""]
            for _, item in candidates[:10]:
                kickoff = item["kickoff_at"]
                local_time = (
                    kickoff.replace(tzinfo=timezone.utc).astimezone(EAT)
                    if kickoff else None
                )
                lines.append(
                    f"• {item['home_team']} vs {item['away_team']}"
                    + (f" — {local_time:%d %b %H:%M EAT}" if local_time else "")
                )
            await reply(update, "\n".join(lines))
            return

        raw, item = candidates[0]
        fixture_id = first_value(raw, "id", "fixtureId", "matchId", "matchID", "match_id")
        if fixture_id is None:
            await reply(update, "⚠️ The fixture was found, but the provider did not supply a match ID.")
            return

        kickoff = item["kickoff_at"]
        local_time = (
            kickoff.replace(tzinfo=timezone.utc).astimezone(EAT)
            if kickoff else None
        )
        header = [
            "⚽ MATCH INTELLIGENCE",
            "",
            f"{item['home_team']} vs {item['away_team']}",
            f"🏆 {item['competition'] or 'Competition unavailable'}"
            + (f" | {item['country']}" if item["country"] else ""),
            f"🕒 {local_time:%d %b %Y — %H:%M EAT}" if local_time else "🕒 Kick-off time unavailable",
            f"📍 Status: {item['status'] or 'Scheduled'}",
        ]

        await reply(update, "✅ Fixture found. Checking lineups, statistics, odds and match events...")

        output = header + [
            "",
            "📋 LINEUPS",
            "• Starting lineups are not provided by open-matchday.",
            "",
            "📈 MATCH STATISTICS",
            "• No live or pre-match match statistics are supplied for an upcoming fixture.",
            "",
            "💹 AVAILABLE ODDS",
            "• Verified bookmaker odds are not available through this free data source.",
            "",
            "🗓 MATCH EVENTS",
            "• Live events are not available; this dataset is refreshed daily.",
            "",
            "🧠 PRE-MATCH PREDICTION",
        ]
        await reply(update, "📊 Fixture found. Analysing recent results from this competition...")
        prediction = await asyncio.to_thread(
            build_openmatchday_prediction,
            raw,
            item,
        )

        if prediction.get("available"):
            home_pct = prediction["home_win"] * 100
            draw_pct = prediction["draw"] * 100
            away_pct = prediction["away_win"] * 100
            output.extend([
                f"• Model score: {prediction['score'][0]}–{prediction['score'][1]}",
                f"• 1X2 probabilities: Home {home_pct:.1f}% | Draw {draw_pct:.1f}% | Away {away_pct:.1f}%",
                f"• Expected goals: {prediction['lambda_home']:.2f} – {prediction['lambda_away']:.2f}",
                f"• Over 1.5 goals probability: {prediction['over_15'] * 100:.1f}%",
                f"• Over 2.5 goals probability: {prediction['over_25'] * 100:.1f}%",
                f"• Recent form (oldest → latest): Home {prediction['home_form']} | Away {prediction['away_form']}",
                f"• Average goals scored/conceded: Home {prediction['home_scored']:.2f}/{prediction['home_conceded']:.2f} | Away {prediction['away_scored']:.2f}/{prediction['away_conceded']:.2f}",
                f"• Results used: {prediction['home_results']} for home team; {prediction['away_results']} for away team",
                "",
                "📚 HOME TEAM — RECENT RESULTS (newest first)",
            ])
            for match in prediction["home_recent_matches"]:
                match_date = match["date"].strftime("%d %b %Y") if match["date"] else "Date n/a"
                output.append(f"• {match_date} | {match['venue']} vs {match['opponent']} | {match['score']} | {match['result']}")
            output.extend(["", "📚 AWAY TEAM — RECENT RESULTS (newest first)"])
            for match in prediction["away_recent_matches"]:
                match_date = match["date"].strftime("%d %b %Y") if match["date"] else "Date n/a"
                output.append(f"• {match_date} | {match['venue']} vs {match['opponent']} | {match['score']} | {match['result']}")
            output.extend([
                f"• Model confidence: {prediction['confidence']}",
                f"• Outcome lean: {prediction['top_outcome']} (highest model probability, not a guaranteed result)",
                "• BET / NO BET: NO BET — odds are unavailable, so value cannot be assessed.",
            ])
        else:
            output.extend([
                "• Forecast not produced: " + prediction.get("reason", "Insufficient verified data."),
                "• BET / NO BET: NO BET — insufficient verified data for a responsible forecast.",
            ])

        output.extend([
            "",
            "⚠️ MODEL LIMITATIONS",
            "• Uses up to the 10 most recent completed matches per team in this competition.",
            "• Basic Poisson model; it does not account for injuries, suspensions, lineups, xG or tactical changes.",
            "• Source data is refreshed daily, not live. Probabilities are estimates, not guarantees.",
        ])

        # Telegram messages have a 4096-character limit; split long reports safely.
        message = "\n".join(output)
        for start in range(0, len(message), 3800):
            await reply(update, message[start:start + 3800])

    except Exception as exc:
        logger.exception("Match intelligence request failed")
        await reply(update, f"❌ MATCH INTELLIGENCE FAILED\n\n{str(exc)[:700]}")


# ============================================================
# ODDS API STATUS
# ============================================================

async def odds(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if ODDS_API_KEY:

        await reply(
            update,
            "✅ ODDS_API_KEY is configured.\n\n"
            "This confirms only that a key exists. "
            "A live bookmaker odds feed has not yet "
            "been integrated and verified.",
        )

    else:

        await reply(
            update,
            "⚠️ ODDS_API_KEY is not configured. "
            "Live bookmaker odds collection is not active.",
        )


# ============================================================
# BOOKMAKERS
# ============================================================

async def bookmakers(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await reply(
        update,
        "🏦 PUNTER KENYA BOOKMAKER REGISTRY\n\n"
        "🏆 SportyBet Kenya — sole supported bookmaker; live feed status is checked on demand\n"
        "⏸️ SportPesa — no current dependable free feed verified\n"
        "⏸️ Odibets — no current dependable free feed verified\n"
        "⏸️ Mozzart Bet — no current dependable free feed verified\n"
        "\nOnly SportyBet Kenya is supported for bookmaker odds and booking-code generation. "
        "No bet is placed by the bot.",
    )


# ============================================================
# BOOKMAKER COMPARISON STATUS
# ============================================================

async def compare(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "📊 BOOKMAKER COMPARISON\n\n"
        "Status: not yet active.\n"
        "The comparison engine requires verified odds feeds "
        "and timestamped price observations.",
    )



# ============================================================
# LIVE BOOKMAKER ODDS — READ-ONLY CONNECTORS
# ============================================================

def save_bookmaker_odds_snapshots(games):
    """Persist timestamped prices through the shared tracking service."""
    return save_price_observations(games)


async def bookmakers_command(update, context):
    day = datetime.now(EAT).date()
    await reply(update, f"🔎 Checking the SportyBet Kenya public odds feed for {day:%d %b %Y}...")
    result = await asyncio.to_thread(fetch_bookmaker_odds, day)
    lines = ["🏦 BOOKMAKER FEED CHECK", f"Date: {day:%d %b %Y} (EAT)", ""]
    for name in ("SportyBet",):
        item = result["bookmakers"].get(name)
        if item:
            lines.append(f"✅ {name}: {item['events']} events | {item['priced_events']} with markets")
        else:
            lines.append(f"⚠️ {name}: " + result["errors"].get(name, "No response"))
    lines.extend(["", "PUNTER KENYA bookmaker coverage: SportyBet Kenya only."])
    lines.extend(["This is a read-only public odds-feed check. A successful response does not guarantee continuous availability."])
    await reply(update, "\n".join(lines))


async def bookmaker_odds_command(update, context):
    from datetime import timedelta
    raw_query = " ".join(context.args).strip()
    if not raw_query:
        await reply(update,
            "💹 BOOKMAKER ODDS COMPARISON\n\n"
            "Usage: /odds HOME TEAM vs AWAY TEAM [today|tomorrow]\n"
            "Example: /odds Brighton vs Brentford today\n\n"
            "Checks retrieved SportyBet Kenya prices. Only markets actually returned by the feed are shown.")
        return
    day = datetime.now(EAT).date()
    lowered = raw_query.casefold()
    if lowered.endswith(" tomorrow"):
        day += timedelta(days=1)
        raw_query = raw_query[:-9].strip()
    elif lowered.endswith(" today"):
        raw_query = raw_query[:-6].strip()
    if " vs " not in raw_query.casefold():
        await reply(update, "Please provide both teams: /odds HOME TEAM vs AWAY TEAM [today|tomorrow]")
        return
    home, away = re.split(r"\s+vs\s+", raw_query, maxsplit=1, flags=re.IGNORECASE)
    await reply(update, f"🔎 Retrieving bookmaker prices for {home.strip()} vs {away.strip()} on {day:%d %b}...")
    result = await asyncio.to_thread(fetch_bookmaker_odds, day)
    matches = []
    for name, item in result["bookmakers"].items():
        matches.extend(find_bookmaker_event(item["games"], home.strip(), away.strip()))
    # Keep exact fixture orientation separate; don't silently reverse home/away markets.
    exact = [g for g in matches
             if " ".join(str(g.get("home") or "").casefold().split()) == " ".join(home.casefold().split())
             and " ".join(str(g.get("away") or "").casefold().split()) == " ".join(away.casefold().split())]
    matches = exact or matches
    unique = {(g.get("bookmaker"), g.get("event_id")): g for g in matches}
    matches = list(unique.values())
    snapshots_saved = 0
    snapshot_error = None
    if matches:
        try:
            snapshots_saved = await asyncio.to_thread(save_bookmaker_odds_snapshots, matches)
        except Exception as exc:
            snapshot_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.warning("Could not persist bookmaker odds snapshots: %s", snapshot_error)
    if not matches:
        errors = "; ".join(f"{k}: {v}" for k, v in result["errors"].items())
        await reply(update,
            "❌ No confidently matched bookmaker event was found.\n"
            "The fixture may not be listed by these bookmakers, or team names may differ."
            + (f"\nFeed errors: {errors}" if errors else ""))
        return
    lines = ["💹 BOOKMAKER ODDS COMPARISON",
             f"{home.strip()} vs {away.strip()}",
             f"Date: {day:%d %b %Y} (EAT)", ""]
    for game in matches[:4]:
        lines.append(f"🏦 {game['bookmaker']} — {game.get('home')} vs {game.get('away')}")
        lines.append(f"Kick-off: {game.get('kickoff_eat') or 'Not supplied'}")
        if not game.get("markets"):
            lines.append("No usable prices returned.")
        else:
            for market in game["markets"][:35]:
                handicap = f" ({market['handicap']})" if market.get("handicap") not in (None, "") else ""
                lines.append(f"• {market['market']}: {market['selection']}{handicap} @ {market['odds']:.2f}")
        if game.get("url"):
            lines.append(game["url"])
        lines.append("")
    lines.append(f"Historical price observations saved: {snapshots_saved}.") if snapshots_saved else lines.append("Historical odds storage: not saved.")
    if snapshot_error:
        lines.append(f"Database storage warning: {snapshot_error}")
    lines.append("Prices are snapshots retrieved now and may change before placement. Check the bookmaker slip before betting. No bets are placed by this bot.")
    message = "\n".join(lines)
    for offset in range(0, len(message), 3700):
        await reply(update, message[offset:offset + 3700])


async def odds_history_command(update, context):
    """Show recent stored price observations for a precisely named fixture."""
    raw_query = " ".join(context.args).strip()
    if " vs " not in raw_query.casefold():
        await reply(update, "Usage: /oddshistory HOME TEAM vs AWAY TEAM")
        return
    home, away = re.split(r"\s+vs\s+", raw_query, maxsplit=1, flags=re.IGNORECASE)
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT bookmaker, market_name, selection_name, handicap_value,
                       decimal_odds, fetched_at
                FROM punter_odds_snapshots
                WHERE LOWER(home_team)=LOWER(%s) AND LOWER(away_team)=LOWER(%s)
                ORDER BY fetched_at DESC
                LIMIT 40
            """, (home.strip(), away.strip()))
            rows = cursor.fetchall()
    except Exception as exc:
        await reply(update, "Historical odds are unavailable. Run /odds for this exact fixture first.\n"
                     f"Database message: {type(exc).__name__}: {str(exc)[:180]}")
        return
    finally:
        if connection:
            connection.close()
    if not rows:
        await reply(update, "No stored price history found for that exact home/away team spelling. Run /odds first; each successful retrieval adds a timestamped snapshot.")
        return
    lines = [f"📈 STORED ODDS OBSERVATIONS", f"{home.strip()} vs {away.strip()}", ""]
    for row in rows:
        handicap = f" ({row['handicap_value']})" if row.get("handicap_value") else ""
        lines.append(
            f"{row['bookmaker']} | {row['market_name']} — {row['selection_name']}{handicap} "
            f"@ {float(row['decimal_odds']):.2f} | {row['fetched_at']}"
        )
    lines.append("\nThese are observations captured by PUNTER KENYA, not a complete continuous price history.")
    message = "\n".join(lines)
    for offset in range(0, len(message), 3700):
        await reply(update, message[offset:offset + 3700])


# ============================================================
# ODDS CALCULATOR
# ============================================================

async def calculator(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await reply(
            update,
            "Usage: /calculator DECIMAL_ODDS STAKE\n"
            "Example: /calculator 2.50 100",
        )

        return

    try:

        odds_value = float(context.args[0])

        stake = (
            float(context.args[1])
            if len(context.args) > 1
            else 100.0
        )

        if odds_value <= 1 or stake <= 0:
            raise ValueError

        returned = odds_value * stake

        await reply(
            update,
            "🧮 ODDS CALCULATOR\n\n"
            f"Decimal odds: {odds_value:.2f}\n"
            f"Stake: KSh {stake:,.2f}\n"
            f"Potential return: KSh {returned:,.2f}\n"
            f"Potential profit: KSh {returned - stake:,.2f}\n"
            f"Implied probability: "
            f"{(100 / odds_value):.2f}%\n\n"
            "This is arithmetic, not a prediction of the outcome.",
        )

    except (ValueError, OverflowError):

        await reply(
            update,
            "❌ Enter valid positive numbers. "
            "Example: /calculator 2.50 100",
        )


# ============================================================
# TELEGRAM ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Unhandled Telegram handler error",
        exc_info=context.error,
    )


# ============================================================
# APPLICATION STARTUP
# ============================================================

def collect_walk_forward_forecasts(max_matches=2000):
    """Reconstruct current and venue-weighted Dixon-Coles forecasts before each match date."""
    from dixon_coles_engine import selected_outcome_probability
    samples = []
    leagues = fetch_all_leagues()
    for league in leagues:
        folder = str(league.get("leagueShortcut") or "").strip()
        if not folder:
            continue
        try:
            rows = fetch_openmatchday_csv(folder, "results")
        except Exception as exc:
            logger.warning("Backtest skipped %s: %s", folder, str(exc)[:160])
            continue
        completed = []
        for row in rows:
            try:
                match_date = datetime.strptime(str(row.get("date") or ""), "%Y-%m-%d").date()
                hg, ag = int(row.get("ft_home")), int(row.get("ft_away"))
            except (TypeError, ValueError):
                continue
            home, away = str(row.get("home") or "").strip(), str(row.get("away") or "").strip()
            if home and away and hg >= 0 and ag >= 0:
                completed.append((match_date, home, away, hg, ag))
        completed.sort(key=lambda item: item[0])
        for match_date, home, away, hg, ag in completed:
            prediction = build_openmatchday_prediction(
                {"leagueShortcut": folder},
                {"home_team": home, "away_team": away},
                historical_rows=rows,
                as_of_date=match_date,
                include_monte_carlo=False,
            )
            candidate = build_openmatchday_prediction(
                {"leagueShortcut": folder},
                {"home_team": home, "away_team": away},
                historical_rows=rows,
                as_of_date=match_date,
                model_variant="venue_weighted",
                include_monte_carlo=False,
            )
            if not prediction.get("available") or not candidate.get("available"):
                continue
            # The deployable candidate is venue-weighted Dixon-Coles. Backtest
            # exactly the same 1X2 probability vector that the live gate uses.
            try:
                lh, la = float(candidate["lambda_home"]), float(candidate["lambda_away"])
                candidate_vector = [
                    selected_outcome_probability(lh, la, "1", home, away),
                    selected_outcome_probability(lh, la, "X", home, away),
                    selected_outcome_probability(lh, la, "2", home, away),
                ]
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            if not all(value is not None and math.isfinite(float(value)) and 0 <= float(value) <= 1
                       for value in candidate_vector) or sum(candidate_vector) <= 0:
                continue
            samples.append({
                "match_date": match_date,
                "p_home": prediction["home_win"],
                "p_draw": prediction["draw"],
                "p_away": prediction["away_win"],
                "candidate_home": candidate_vector[0],
                "candidate_draw": candidate_vector[1],
                "candidate_away": candidate_vector[2],
                "actual_home_goals": hg,
                "actual_away_goals": ag,
            })
            if len(samples) >= max_matches:
                return sorted(samples, key=lambda item: item["match_date"])
    return sorted(samples, key=lambda item: item["match_date"])


def evaluate_historical_calibration():
    """Evaluate reconstructed pre-match forecasts and cache the result for six hours.

    The reconstruction is intentionally expensive and is not repeated on every
    accumulator request. Historical simulations omit Monte Carlo because the
    calibration target is the deterministic 1X2 scoreline probability vector.
    """
    cache = globals().setdefault("_WALK_FORWARD_CALIBRATION_CACHE", {})
    if cache.get("result") is not None and time.time() - cache.get("timestamp", 0) < 21600:
        return dict(cache["result"])

    rows = collect_walk_forward_forecasts()
    source = "walk-forward reconstruction from historical competition results"
    rows = sorted(rows, key=lambda row: row["match_date"])
    probabilities, outcomes = [], []
    usable = []
    for row in rows:
        try:
            hg, ag = int(row["actual_home_goals"]), int(row["actual_away_goals"])
            vector = [float(row["p_home"]), float(row["p_draw"]), float(row["p_away"])]
            match_date = row["match_date"]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if hg < 0 or ag < 0 or not all(math.isfinite(p) and 0 <= p <= 1 for p in vector) or sum(vector) <= 0:
            continue
        candidate_vector = [
            float(row.get("candidate_home", 0)),
            float(row.get("candidate_draw", 0)),
            float(row.get("candidate_away", 0)),
        ]
        if not all(math.isfinite(p) and 0 <= p <= 1 for p in candidate_vector) or sum(candidate_vector) <= 0:
            continue
        usable.append((match_date, vector, 0 if hg > ag else 1 if hg == ag else 2, candidate_vector))

    count = len(usable)
    dates = sorted({row[0] for row in usable})
    date_split = int(len(dates) * 0.70)
    if date_split < 1 or date_split >= len(dates):
        result = {"ready": False, "count": count, "train": 0, "holdout": 0, "source": source}
        cache.update({"timestamp": time.time(), "result": result})
        return dict(result)
    cutoff = dates[date_split]
    train = [row for row in usable if row[0] < cutoff]
    holdout = [row for row in usable if row[0] >= cutoff]
    if len(train) < 50 or len(holdout) < 30:
        result = {"ready": False, "count": count, "train": len(train),
                  "holdout": len(holdout), "source": source}
        cache.update({"timestamp": time.time(), "result": result})
        return dict(result)

    train_p = [row[1] for row in train]
    train_y = [row[2] for row in train]
    test_p = [row[1] for row in holdout]
    test_y = [row[2] for row in holdout]

    # Fit temperature on the venue-weighted Dixon-Coles candidate itself,
    # never on the older model and then transfer that temperature to a different model.
    candidate_train_p = [row[3] for row in train]
    candidate_p = [row[3] for row in holdout]
    fitted = fit_temperature(candidate_train_p, train_y, minimum_samples=50)
    candidate_calibrated = [
        apply_temperature(p, fitted["temperature"]) for p in candidate_p
    ]
    candidate_brier = brier_score(candidate_p, test_y)
    candidate_logloss = log_loss(candidate_p, test_y)
    candidate_calibrated_brier = brier_score(candidate_calibrated, test_y)
    candidate_calibrated_logloss = log_loss(candidate_calibrated, test_y)

    # A training-only prevalence baseline is a minimum reference, not a deployable model.
    baseline = [sum(1 for y in train_y if y == outcome) / len(train_y) for outcome in range(3)]
    baseline_p = [baseline for _ in test_y]
    result = {
        "ready": True, "count": count, "train": len(train),
        "holdout": len(holdout), "source": source,
        "temperature": fitted["temperature"],
        "raw_brier": brier_score(test_p, test_y),
        "raw_logloss": log_loss(test_p, test_y),
        "calibrated_brier": candidate_calibrated_brier,
        "calibrated_logloss": candidate_calibrated_logloss,
        "candidate_brier": candidate_brier,
        "candidate_logloss": candidate_logloss,
        "candidate_calibrated_brier": candidate_calibrated_brier,
        "candidate_calibrated_logloss": candidate_calibrated_logloss,
        "baseline_brier": brier_score(baseline_p, test_y),
        "baseline_logloss": log_loss(baseline_p, test_y),
    }
    cache.update({"timestamp": time.time(), "result": result})
    return dict(result)


async def calibrate_command(update, context):
    await reply(update, "🧪 Evaluating historical 1X2 forecasts using an earlier-train / later-holdout split...")
    try:
        result = await asyncio.to_thread(evaluate_historical_calibration)
        if not result["ready"]:
            await reply(update,
                "NOT ENOUGH HISTORICAL DATA TO CALIBRATE\n\n"
                f"Forecast sample: {result['count']}\n"
                f"Training sample: {result['train']} (minimum 50)\n"
                f"Out-of-sample holdout: {result['holdout']} (minimum 30)\n\n"
                "No calibration parameter has been fitted or applied. Keep the model labelled experimental.")
            return
        improved = (
            result["candidate_calibrated_brier"] < result["candidate_brier"]
            and result["candidate_calibrated_logloss"] < result["candidate_logloss"]
        )
        candidate_improved = (
            result["candidate_brier"] < result["raw_brier"]
            and result["candidate_logloss"] < result["raw_logloss"]
            and result["candidate_brier"] < result["baseline_brier"]
            and result["candidate_logloss"] < result["baseline_logloss"]
        )
        await reply(update,
            "📊 HISTORICAL MODEL VALIDATION\\n\\n"
            f"Forecast sample: {result['count']} | Train: {result['train']} | Holdout: {result['holdout']}\\n"
            f"Data source: {result['source']}\\n"
            f"Fitted temperature (not applied): {result['temperature']:.2f}\\n\\n"
            f"Current model Brier: {result['raw_brier']:.4f} | log loss: {result['raw_logloss']:.4f}\\n"
            f"Venue-weighted Dixon-Coles candidate: Brier {result['candidate_brier']:.4f} | log loss {result['candidate_logloss']:.4f}\\n"
            f"Candidate after temperature calibration: Brier {result['candidate_calibrated_brier']:.4f} | log loss {result['candidate_calibrated_logloss']:.4f}\\n"
            f"Training-prevalence baseline: Brier {result['baseline_brier']:.4f} | log loss {result['baseline_logloss']:.4f}\\n\\n"
            + ("Candidate beat both the current model and baseline on both metrics; it may proceed to further validation." if candidate_improved else "Candidate did not beat both the current model and baseline on both metrics. Do not promote it; keep the model experimental.")
            + "\\n"
            + ("Temperature calibration improved both holdout metrics." if improved else "Temperature calibration did not improve both holdout metrics and remains disabled.")
            + "\\n\\nNo live prediction settings are changed automatically.")
    except Exception as exc:
        await reply(update, f"⚠️ Calibration evaluation failed: {type(exc).__name__}: {str(exc)[:250]}")



def refresh_pending_bet_records(user_id=None):
    """Settle logged bets only when one exact team/date result exists in source CSV."""
    pending = list_pending_bets(user_id)
    if not pending:
        return {"checked": 0, "settled": 0, "ambiguous": 0, "requires_market_data": 0, "pending": 0}
    leagues = fetch_all_leagues()
    result_cache = {}
    market_evidence_cache = {}
    settled = ambiguous = checked = requires_market_data = 0
    for bet in pending:
        match_date = bet.get("match_date")
        if not match_date:
            continue
        date_text = match_date.isoformat() if hasattr(match_date, "isoformat") else str(match_date)[:10]
        matches = []
        for league in leagues:
            folder = str(league.get("leagueShortcut") or "").strip()
            if not folder:
                continue
            if folder not in result_cache:
                try:
                    result_cache[folder] = fetch_openmatchday_csv(folder, "results")
                except Exception as exc:
                    logger.warning("Could not load results CSV for %s: %s", folder, exc)
                    result_cache[folder] = []
            for row in result_cache[folder]:
                if str(row.get("date") or "").strip() != date_text:
                    continue
                if canonical_team_key(row.get("home")) != canonical_team_key(bet.get("home_team")):
                    continue
                if canonical_team_key(row.get("away")) != canonical_team_key(bet.get("away_team")):
                    continue
                try:
                    hg, ag = int(row.get("ft_home")), int(row.get("ft_away"))
                except (TypeError, ValueError):
                    continue
                matches.append((hg, ag))
        checked += 1
        unsupported_score_market = False
        if len(matches) == 1:
            try:
                settle_bet(int(bet["user_id"]), int(bet["id"]), matches[0][0], matches[0][1])
                settled += 1
                continue
            except ValueError as exc:
                if "Pending bet record not found" in str(exc):
                    continue
                unsupported_score_market = True
            except Exception:
                logger.exception("CSV-based automatic settlement failed for bet #%s", bet.get("id"))
                continue
        elif len(matches) > 1:
            ambiguous += 1

        # Optional provider fallback covers fixtures absent from the community CSV
        # and markets that require half-time or match-stat evidence.
        if os.getenv("API_FOOTBALL_KEY"):
            cache_key = (date_text, str(bet.get("home_team")), str(bet.get("away_team")))
            try:
                if cache_key not in market_evidence_cache:
                    market_evidence_cache[cache_key] = fetch_match_market_data(
                        bet.get("home_team"), bet.get("away_team"), date_text
                    )
                settle_bet_with_market_evidence(
                    int(bet["user_id"]), int(bet["id"]), market_evidence_cache[cache_key]
                )
                settled += 1
                continue
            except Exception as provider_error:
                logger.info("Provider evidence did not settle bet #%s: %s", bet.get("id"), provider_error)
                if unsupported_score_market or len(matches) > 0:
                    requires_market_data += 1
        elif unsupported_score_market:
            requires_market_data += 1
    return {"checked": checked, "settled": settled, "ambiguous": ambiguous,
            "requires_market_data": requires_market_data,
            "pending": max(0, len(pending) - settled)}


async def marketdata_command(update, context):
    """Probe the optional statistics provider for one exact completed fixture."""
    raw = " ".join(context.args or [])
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 2 or " vs " not in parts[0].casefold():
        await reply(update, "Usage: /marketdata HOME vs AWAY | YYYY-MM-DD\\nExample: /marketdata Arsenal vs Chelsea | 2026-11-08")
        return
    teams = re.split(r"\s+vs\.?\s+", parts[0], maxsplit=1, flags=re.I)
    if len(teams) != 2:
        await reply(update, "Use HOME vs AWAY | YYYY-MM-DD.")
        return
    try:
        data = await asyncio.to_thread(fetch_match_market_data, teams[0], teams[1], parts[1])
        fixture = data["fixture"]
        score = fixture.get("score") or {}
        half = score.get("halftime") or {}
        full = score.get("fulltime") or {}
        def stat_summary(rows):
            totals = {}
            for team_row in rows:
                for stat in team_row.get("statistics") or []:
                    name = str(stat.get("type") or "")
                    value = stat.get("value")
                    if isinstance(value, (int, float)):
                        totals[name] = totals.get(name, 0) + value
            return totals
        full_stats = stat_summary(data["statistics_full_time"])
        half_stats = stat_summary(data["statistics_half_time"])
        lines = [
            "📡 API-FOOTBALL MATCH DATA",
            f"Fixture ID: {data['fixture_id']} | Status: {(fixture.get('fixture') or {}).get('status', {}).get('short', 'unknown')}",
            f"Full-time score: {full.get('home', '—')}-{full.get('away', '—')}",
            f"Half-time score: {half.get('home', '—')}-{half.get('away', '—')}",
            "",
            "Full-time match statistics:",
        ]
        for key in ("Corner Kicks", "Yellow Cards", "Red Cards"):
            lines.append(f"• {key}: {full_stats.get(key, 'not supplied')}")
        lines.append("Half-time statistics: " + (", ".join(f"{k}={v}" for k, v in half_stats.items()) if half_stats else "not supplied by this competition/fixture"))
        lines.append(f"Player-stat response groups: {len(data['players'])} | Match events: {len(data['events'])}")
        lines.extend(["", "Provider data retrieval verified for this fixture only. Market-specific settlement is not enabled until the market's exact bookmaker rules are mapped and tested."])
        await reply(update, "\n".join(lines))
    except Exception as exc:
        await reply(update, f"⚠️ Match-statistics provider unavailable: {type(exc).__name__}: {str(exc)[:250]}")


async def automatic_specialist_forecast_job(context):
    """Settle stored specialist forecasts from completed API-Football match data."""
    try:
        from forecast_tracker import refresh_specialist_forecasts
        result = await asyncio.to_thread(refresh_specialist_forecasts)
        logger.info("Specialist forecast settlement: %s checked, %s newly settled, %s pending.",
                    result.get("checked", 0), result.get("settled", 0), result.get("pending", 0))
    except Exception:
        logger.exception("Scheduled specialist forecast settlement failed")


async def automatic_prediction_results_job(context):
    """Refresh stored model forecasts against newly completed competition results."""
    try:
        records, settled = await asyncio.to_thread(refresh_and_read_prediction_records)
        logger.info(
            "Automatic forecast-result refresh: %s newly settled; %s total records.",
            settled, len(records),
        )
    except Exception:
        logger.exception("Scheduled model forecast-result refresh failed")


async def automatic_price_snapshot_job(context):
    """Capture today's and tomorrow's public bookmaker prices every 30 minutes."""
    try:
        result = await asyncio.to_thread(capture_current_prices)
        logger.info(
            "Automatic odds snapshot: %s events, %s prices saved for %s; feed errors=%s",
            result.get("events", 0), result.get("saved", 0),
            ",".join(result.get("dates", [])), result.get("errors", {}),
        )
    except Exception:
        logger.exception("Scheduled bookmaker price snapshot failed")


async def automatic_bet_settlement_job(context):
    try:
        result = await asyncio.to_thread(refresh_pending_bet_records)
        if result["settled"]:
            logger.info("Automatically settled %s logged bets from completed-results CSV.", result["settled"])
    except Exception:
        logger.exception("Scheduled bet-result refresh failed")


async def betresults_command(update, context):
    await reply(update, "🔄 Checking completed results and settling only exact fixture/date matches...")
    try:
        result = await asyncio.to_thread(refresh_pending_bet_records, update.effective_user.id)
        await reply(update,
            "📊 AUTOMATIC BET SETTLEMENT\n\n"
            f"Pending bets checked: {result['checked']}\n"
            f"Newly settled: {result['settled']}\n"
            f"Ambiguous results left untouched: {result['ambiguous']}\n"
            f"Require market-specific data/manual review: {result['requires_market_data']}\n"
            f"Still pending: {result['pending']}\n\n"
            "Full-time scores settle only score-based markets. Half-time, corners, cards, player props and unsupported Asian lines stay pending until the required market data is available; they are never guessed. Results are matched by exact home team, away team and UTC match date.")
    except Exception as exc:
        await reply(update, f"⚠️ Result refresh failed: {type(exc).__name__}: {str(exc)[:250]}")


def resolve_bet_match_date(home, away):
    """Resolve a logged fixture's EAT date only when the upcoming catalogue has one unique match."""
    rows = fetch_openmatchday_fixture_rows(30)
    matches = []
    for raw in rows:
        fixture = normalise_fixture(raw)
        if not fixture or not fixture.get("kickoff_at"):
            continue
        if canonical_team_key(fixture.get("home_team")) == canonical_team_key(home) and canonical_team_key(fixture.get("away_team")) == canonical_team_key(away):
            matches.append(utc_match_date(fixture["kickoff_at"]))
    dates = set(matches)
    return next(iter(dates)) if len(dates) == 1 else None


async def betlog_command(update, context):
    """Log a real bookmaker selection and its original price/stake."""
    raw = " ".join(context.args or [])
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) not in (5, 6, 7):
        await reply(update, "Usage: /betlog HOME vs AWAY | MARKET | SELECTION | ODDS | STAKE [| BOOKMAKER | YYYY-MM-DD]\nExample: /betlog Arsenal vs Chelsea | 1X2 | Arsenal | 2.10 | 100 | SportyBet | 2026-11-08")
        return
    fixture = re.split(r"\s+vs\.?\s+|\s+v\.?\s+", parts[0], maxsplit=1, flags=re.I)
    if len(fixture) != 2:
        await reply(update, "Enter the fixture as HOME vs AWAY.")
        return
    bookmaker = parts[5] if len(parts) >= 6 and parts[5] else "SportyBet"
    try:
        if len(parts) == 7:
            match_date = datetime.strptime(parts[6], "%Y-%m-%d").date()
        else:
            try:
                match_date = await asyncio.to_thread(resolve_bet_match_date, fixture[0], fixture[1])
            except Exception as date_error:
                logger.warning("Could not resolve bet match date; keeping bet log but automatic settlement will need a date: %s", date_error)
                match_date = None
        bet_id = await asyncio.to_thread(
            record_bet, update.effective_user.id, bookmaker,
            fixture[0], fixture[1], parts[1], parts[2], parts[3], parts[4], match_date
        )
        date_note = match_date.isoformat() if match_date else "Not resolved — automatic settlement unavailable until date is supplied"
        await reply(update, f"✅ BET RECORD SAVED — #{bet_id}\n{fixture[0]} vs {fixture[1]}\n{parts[1]}: {parts[2]} @ {float(parts[3]):.2f}\nStake: {float(parts[4]):.2f} KSh\nMatch date: {date_note}\nOriginal odds and stake are preserved.")
    except Exception as exc:
        await reply(update, f"⚠️ Could not save bet record: {type(exc).__name__}: {str(exc)[:250]}")


async def betsettle_command(update, context):
    """Settle a logged selection against the confirmed full-time score."""
    if len(context.args or []) != 3:
        await reply(update, "Usage: /betsettle BET_ID HOME_GOALS AWAY_GOALS\nExample: /betsettle 12 2 1")
        return
    try:
        bet_id, home_goals, away_goals = map(int, context.args)
        if min(home_goals, away_goals) < 0:
            raise ValueError
        result = await asyncio.to_thread(
            settle_bet, update.effective_user.id, bet_id, home_goals, away_goals
        )
        await reply(update, f"📊 BET #{bet_id} SETTLED: {result['status']}\nFull-time: {home_goals}-{away_goals}\nProfit/loss: {float(result['profit_loss']):+.2f} KSh")
    except Exception as exc:
        await reply(update, f"⚠️ Could not settle bet: {type(exc).__name__}: {str(exc)[:250]}")


async def betstats_command(update, context):
    try:
        stats = await asyncio.to_thread(bet_statistics, update.effective_user.id)
        total = int(stats.get("total") or 0)
        pending = int(stats.get("pending") or 0)
        won, lost, voided = int(stats.get("won") or 0), int(stats.get("lost") or 0), int(stats.get("voided") or 0)
        pnl = float(stats.get("profit_loss") or 0)
        turnover = float(stats.get("settled_stakes") or 0)
        settled = won + lost + voided
        roi = pnl / turnover if turnover else 0.0
        await reply(update, "📈 PUNTER BET PERFORMANCE\n\n"
            f"Logged: {total} | Settled: {settled} | Pending: {pending}\n"
            f"Wins: {won} | Losses: {lost} | Voids: {voided}\n"
            f"Settled stakes: {turnover:.2f} KSh\nProfit/loss: {pnl:+.2f} KSh\nROI: {roi:+.2%}\n\n"
            "This report uses only bets you logged and settled; it is separate from prediction accuracy.")
    except Exception as exc:
        await reply(update, f"⚠️ Bet statistics unavailable: {type(exc).__name__}: {str(exc)[:250]}")




async def bankroll_command(update, context):
    try:
        if context.args:
            await asyncio.to_thread(save_bankroll, update.effective_user.id, float(context.args[0]))
        settings = await asyncio.to_thread(get_settings, update.effective_user.id)
        amount = settings.get("bankroll")
        if amount is None:
            await reply(update, "Bankroll not set. Use /bankroll AMOUNT (KSh).")
        else:
            await reply(update, f"Saved planning bankroll: KSh {float(amount):,.2f}\nUse /stake PROBABILITY ODDS for a capped fractional-Kelly illustration.")
    except Exception as exc:
        await reply(update, f"Bankroll setting failed: {type(exc).__name__}: {str(exc)[:180]}\nUsage: /bankroll 1000")


async def stake_command(update, context):
    if len(context.args or []) != 2:
        await reply(update, "Usage: /stake PROBABILITY ODDS\nExample: /stake 0.58 2.10")
        return
    try:
        probability, odds = map(float, context.args)
        result = await asyncio.to_thread(calculate_user_stake, update.effective_user.id, probability, odds)
        await reply(update, f"STAKE SCENARIO\nBankroll: KSh {result['bankroll']:,.2f}\nStake (25% Kelly, capped at 2%): KSh {result['stake']:,.2f}\n{result['warning']}")
    except Exception as exc:
        await reply(update, f"Stake calculation unavailable: {type(exc).__name__}: {str(exc)[:180]}")


async def council_command(update, context):
    args = list(context.args or [])
    if not args:
        await reply(update, "Usage: /council HOME TEAM vs AWAY TEAM [YYYY-MM-DD]\nRequires API_FOOTBALL_KEY. Example: /council Brighton vs Brentford 2026-11-08")
        return
    match_date = datetime.now(EAT).date()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[-1]):
        match_date = datetime.strptime(args.pop(), "%Y-%m-%d").date()
    raw = " ".join(args)
    split = re.split(r"\s+vs\.?\s+|\s+v\.?\s+", raw, maxsplit=1, flags=re.I)
    if len(split) != 2:
        await reply(update, "Enter the fixture as HOME vs AWAY.")
        return
    home, away = split[0].strip(), split[1].strip()
    await reply(update, f"🧠 SPECIALIST COUNCIL\nResearching recent corners, cards, shots and first-half evidence for {home} vs {away}...")
    try:
        from specialist_models import specialist_council
        report = await asyncio.to_thread(specialist_council, home, away, match_date)
        price_report = None
        try:
            odds_result = await asyncio.to_thread(fetch_bookmaker_odds, match_date)
            priced_games = []
            for provider in (odds_result.get("bookmakers") or {}).values():
                priced_games.extend(provider.get("games") or [])
            matched_games = find_bookmaker_event(priced_games, home, away)
            from specialist_models import specialist_price_assessments
            price_report = specialist_price_assessments(report, matched_games)
        except Exception as price_exc:
            price_report = {"error": f"{type(price_exc).__name__}: {str(price_exc)[:140]}", "assessments": []}
        x_report = None
        if os.getenv("X_BEARER_TOKEN"):
            try:
                from x_research import search_match_posts
                x_report = await asyncio.to_thread(search_match_posts, home, away, 12)
            except Exception as x_exc:
                x_report = {"posts": [], "warning": f"X research unavailable: {type(x_exc).__name__}: {str(x_exc)[:140]}"}
        lines = [f"🧠 SPECIALIST COUNCIL — {report['fixture']}", f"Date: {report['date']}", ""]
        for panel in report["panels"]:
            if not panel["available"]:
                lines.append(f"{panel['role']}: unavailable — {panel['reason']}")
            else:
                market_key = panel["market"]
                selection = f"Over {panel['default_line']}"
                forecast_id = await asyncio.to_thread(
                    record_forecast, f"api-football:{report['fixture_id']}",
                    market_key, selection, panel["over_probability"], None,
                    "specialist-poisson-v1",
                )
                await asyncio.to_thread(
                    record_forecast, f"api-football:{report['fixture_id']}",
                    market_key, f"Under {panel['default_line']}",
                    panel["under_probability"], None, "specialist-poisson-v1",
                )
                lines.append(f"{panel['role']}: expected total {panel['expected_total']:.2f}; Over {panel['default_line']} {panel['over_probability']:.1%}; Under {panel['default_line']} {panel['under_probability']:.1%}; samples {panel['home_sample']}+{panel['away_sample']} | forecast #{forecast_id}")
        lines.extend(["", "💹 LIVE BOOKMAKER PRICE CHECK"])
        if isinstance(price_report, dict) and price_report.get("error"):
            lines.append("Price check unavailable: " + price_report["error"])
        elif price_report:
            for priced in price_report[:6]:
                lines.append(
                    f"{priced['bookmaker']}: {priced['market_name']} — {priced['selection']} "
                    f"@ {priced['odds']:.2f} | model {priced['probability']:.1%} | "
                    f"edge {priced['edge']:+.1%} | estimated ROI {priced['expected_roi']:+.1%}"
                )
            if not price_report:
                lines.append("No exact matching specialist market prices were returned.")
        lines.extend(["", "Player props: use /playerprop HOME vs AWAY | PLAYER | METRIC | LINE"])
        if x_report is not None:
            lines.append("")
            lines.append(f"PUBLIC X RESEARCH: {x_report.get('result_count', len(x_report.get('posts', [])))} recent posts")
            for post in x_report.get("posts", [])[:4]:
                lines.append(f"@{post.get('username') or 'unknown'}: {post.get('text', '')[:180]} {post.get('url') or ''}")
            lines.append(x_report.get("warning") or "X posts are unverified leads, not confirmed team news.")
        elif not os.getenv("X_BEARER_TOKEN"):
            lines.append("PUBLIC X RESEARCH: not configured; set X_BEARER_TOKEN privately to include recent posts.")
        lines.extend(["", report["warning"],
                      "These specialist prices are experimental estimates, not calibrated picks; verify market rules and current prices before use."])
        await reply(update, "\n".join(lines))
    except Exception as exc:
        await reply(update, f"Specialist research unavailable: {type(exc).__name__}: {str(exc)[:220]}")


async def playerprop_command(update, context):
    parts = [x.strip() for x in " ".join(context.args or []).split("|")]
    if len(parts) not in (4, 5):
        await reply(update, "Usage: /playerprop HOME vs AWAY | PLAYER NAME | METRIC | LINE [| YYYY-MM-DD]\nMetrics: shots, shots_on_target, goals, assists, yellow_cards, red_cards")
        return
    fixture = re.split(r"\s+vs\.?\s+|\s+v\.?\s+", parts[0], maxsplit=1, flags=re.I)
    if len(fixture) != 2:
        await reply(update, "Enter the fixture as HOME vs AWAY.")
        return
    match_date = parts[4] if len(parts) == 5 else datetime.now(EAT).date().isoformat()
    try:
        line = float(parts[3])
        await reply(update, f"👤 PLAYER PROP RESEARCH\nChecking {parts[1]} recent appearances for {fixture[0]} vs {fixture[1]}...")
        from specialist_models import player_prop_forecast
        report = await asyncio.to_thread(player_prop_forecast, fixture[0].strip(), fixture[1].strip(),
                                         match_date, parts[1], parts[2], line)
        from forecast_tracker import record_forecast
        forecast_market = "player_" + report["metric"]
        fixture_key = f"api-football-player:{report['fixture_id']}:{report['player_id']}"
        over_id = await asyncio.to_thread(
            record_forecast, fixture_key, forecast_market, f"Over {line:g}",
            report["over_probability"], None, "player-poisson-v1",
        )
        await asyncio.to_thread(
            record_forecast, fixture_key, forecast_market, f"Under {line:g}",
            report["under_probability"], None, "player-poisson-v1",
        )
        await reply(update,
            f"👤 {report['player']} — {report['metric'].replace('_', ' ').title()}\n"
            f"Forecast record: #{over_id} (Over)\n"
            f"Recent appearances: {report['appearances']}\n"
            f"Recent values: {', '.join(str(int(x) if x.is_integer() else x) for x in report['recent_values'])}\n"
            f"Expected per appearance: {report['expected_per_appearance']:.2f}\n"
            f"Over {line:g}: {report['over_probability']:.1%} | Under {line:g}: {report['under_probability']:.1%}\n"
            f"{report['warning']}")
    except Exception as exc:
        await reply(update, f"Player prop unavailable: {type(exc).__name__}: {str(exc)[:220]}")


async def forecastsettle_command(update, context):
    args = list(context.args or [])
    if len(args) != 2:
        await reply(update, "Usage: /forecastsettle FORECAST_ID WON|LOST. Only settle after checking the actual market result.")
        return
    try:
        result = await asyncio.to_thread(settle_forecast, int(args[0]), args[1])
        await reply(update, f"Forecast #{result['id']} recorded as {result['outcome']}.")
    except Exception as exc:
        await reply(update, f"Could not settle forecast: {type(exc).__name__}: {str(exc)[:180]}")


async def forecaststats_command(update, context):
    try:
        report = await asyncio.to_thread(forecast_performance)
        if not report["count"]:
            await reply(update, "No settled specialist forecasts yet. Use /council, then /forecastsettle ID WON|LOST after checking results.")
            return
        lines = ["📊 FORECAST CALIBRATION", f"Settled forecasts: {report['count']}",
                 f"Brier score: {report['brier']:.4f}", f"Log loss: {report['log_loss']:.4f}", ""]
        for market, values in sorted(report["by_market"].items()):
            lines.append(f"{market}: n={values['count']} | Brier {values['brier']:.4f}")
        lines.append("Lower scores are better. This is retrospective scoring, not proof of future profitability.")
        await reply(update, "\n".join(lines))
    except Exception as exc:
        await reply(update, f"Forecast statistics unavailable: {type(exc).__name__}: {str(exc)[:180]}")


async def xresearch_command(update, context):
    raw = " ".join(context.args or [])
    split = re.split(r"\s+vs\.?\s+|\s+v\.?\s+", raw, maxsplit=1, flags=re.I)
    if len(split) != 2:
        await reply(update, "Usage: /xresearch HOME TEAM vs AWAY TEAM")
        return
    await reply(update, "🔎 Searching recent public X posts. Posts are leads, not verified team news...")
    try:
        from x_research import search_match_posts
        report = await asyncio.to_thread(search_match_posts, split[0].strip(), split[1].strip())
        if not report["posts"]:
            await reply(update, "No matching recent public posts were returned by X.")
            return
        lines = ["🗣️ PUBLIC X RESEARCH", f"Results: {report['result_count']}", ""]
        for post in report["posts"][:10]:
            stamp = post.get("created_at") or "time unavailable"
            verified = "✓ " if post.get("verified") else ""
            lines.append(f"{verified}@{post.get('username') or 'unknown'} | {stamp} | ♥{post['likes']} ↻{post['reposts']}\n{post['text'][:500]}\n{post.get('url') or ''}")
        lines.extend(["", report["warning"], "Social posts are not independently verified and must not be treated as confirmed injuries or line-ups."])
        await reply(update, "\n".join(lines))
    except Exception as exc:
        await reply(update, f"X research unavailable: {type(exc).__name__}: {str(exc)[:220]}")


async def clv_command(update, context):
    try:
        report = await asyncio.to_thread(closing_line_value, update.effective_user.id)
        if not report["matched"]:
            await reply(update, f"📉 CLOSING-LINE VALUE\nMatched bets: 0 of {report['bets_checked']} settled bets.\nExact matching odds snapshots are required. Automatic price capture runs every 30 minutes while the bot is online.")
            return
        lines = ["📉 CLOSING-LINE VALUE",
                 f"Matched bets: {report['matched']} of {report['bets_checked']}",
                 f"Average CLV: {report['average_clv']:+.2%}",
                 f"Positive: {report['positive']} | Negative: {report['negative']}", "",
                 "Positive CLV means your taken price beat the latest captured price. This measures price quality, not profit."]
        await reply(update, "\n".join(lines))
    except Exception as exc:
        await reply(update, f"CLV report unavailable: {type(exc).__name__}: {str(exc)[:180]}")


async def rolloverstatus_command(update, context):
    try:
        report = await asyncio.to_thread(rollover_status, update.effective_user.id)
        if not report["configured"]:
            await reply(update, "No rollover plan saved. Use /rollover DAYS DAILY_TARGET_ODDS.")
            return
        lines = ["🔁 ROLLOVER PROGRESS", f"Status: {report['status']}",
                 f"Progress: {report['completed']}/{report['days']} days",
                 f"Target odds/day: {report['target']:.2f}", f"Remaining: {report['remaining']}",
                 f"Wins: {report['wins']} | Losses: {report['losses']}", "",
                 "Record honestly with /rolloverday WON|LOST|MISSED [ACTUAL_ODDS]. A loss breaks the sequence."]
        await reply(update, "\n".join(lines))
    except Exception as exc:
        await reply(update, f"Rollover progress unavailable: {type(exc).__name__}: {str(exc)[:180]}")


async def rolloverday_command(update, context):
    args = list(context.args or [])
    if not args:
        await reply(update, "Usage: /rolloverday WON|LOST|MISSED [ACTUAL_ODDS]")
        return
    try:
        result = await asyncio.to_thread(record_rollover_day, update.effective_user.id, args[0],
                                         float(args[1]) if len(args) > 1 else None)
        await reply(update, f"Rollover day {result['day']}/{result['days']} recorded: {result['result']}. Status: {result['status']}. Use /rolloverstatus.")
    except Exception as exc:
        await reply(update, f"Could not record rollover day: {type(exc).__name__}: {str(exc)[:180]}")


async def automatic_odds_snapshot_job(context):
    try:
        result = await asyncio.to_thread(capture_current_prices)
        logger.info("Scheduled odds capture saved %s prices across %s events; feed errors=%s",
                    result["saved"], result["events"], list(result["errors"]))
    except Exception:
        logger.exception("Scheduled bookmaker price capture failed")


async def preferences_command(update, context):
    """Read, set or clear explicit non-secret user preferences."""
    args = list(context.args or [])
    try:
        if not args:
            settings = await asyncio.to_thread(get_settings, update.effective_user.id)
            try:
                preferences = json.loads(settings.get("preferences_json") or "{}")
            except (TypeError, ValueError):
                preferences = {}
            await reply(update, "🧠 YOUR SAVED PREFERENCES\n" + (json.dumps(preferences, ensure_ascii=False, indent=2) if preferences else "No preferences saved yet.\nExample: /preferences leagues=non-big-five risk=conservative markets=over-1.5"))
            return
        if len(args) == 1 and args[0].casefold() == "clear":
            await asyncio.to_thread(save_preferences, update.effective_user.id, {})
            await reply(update, "Saved preferences cleared.")
            return
        settings = await asyncio.to_thread(get_settings, update.effective_user.id)
        try:
            preferences = json.loads(settings.get("preferences_json") or "{}")
        except (TypeError, ValueError):
            preferences = {}
        for token in args:
            if "=" not in token:
                raise ValueError("Use key=value pairs, e.g. leagues=non-big-five risk=conservative.")
            key, value = token.split("=", 1)
            key = key.strip().casefold()
            value = value.strip()
            if not key or not value or len(key) > 50 or len(value) > 300:
                raise ValueError("Preference keys and values must be non-empty and reasonably short.")
            preferences[key] = value
        await asyncio.to_thread(save_preferences, update.effective_user.id, preferences)
        await reply(update, "✅ Preferences saved. Use /preferences to review or /preferences clear to clear them.")
    except Exception as exc:
        await reply(update, f"Could not update preferences: {type(exc).__name__}: {str(exc)[:180]}")


async def rollover_command(update, context):
    if len(context.args or []) != 2:
        await reply(update, "Usage: /rollover DAYS DAILY_TARGET_ODDS\nExample: /rollover 5 2")
        return
    try:
        days, target = int(context.args[0]), float(context.args[1])
        await asyncio.to_thread(save_rollover, update.effective_user.id, days, target)
        settings = await asyncio.to_thread(get_settings, update.effective_user.id)
        projection = rollover_projection(float(settings.get("bankroll") or 0), days, target)
        await reply(update, f"ROLLOVER PLAN SAVED\nDuration: {days} days\nDaily target odds: {target:.2f}\nIllustrative gross if every day won: KSh {projection['illustrative_gross_if_every_day_won']:,.2f}\n{projection['warning']}")
    except Exception as exc:
        await reply(update, f"Rollover setup failed: {type(exc).__name__}: {str(exc)[:180]}")


async def marketsim_command(update, context):
    raw = " ".join(context.args or [])
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 4:
        await reply(update, "Usage: /marketsim HOME_RATE | AWAY_RATE | MARKET | BOOKMAKER_ODDS\nExample: /marketsim 1.45 | 1.10 | Over 2.5 | 2.05\nSupply researched rates; this is a scenario calculator, not a fixture forecast.")
        return
    try:
        home_rate, away_rate = float(parts[0]), float(parts[1])
        markets = await asyncio.to_thread(market_probabilities, home_rate, away_rate)
        market = parts[2]
        if market not in markets or market.startswith("_"):
            raise ValueError("Unsupported market label. Use: " + ", ".join(k for k in markets if not k.startswith("_")))
        assessment = assess_price(markets[market], float(parts[3]))
        await reply(update, f"MODEL SCENARIO — NOT A VERIFIED PICK\nAssumed rates: {home_rate:.2f} / {away_rate:.2f}\nMarket: {market}\nEstimated probability: {assessment['probability']:.1%}\nImplied probability: {assessment['implied_probability']:.1%}\nEstimated edge: {assessment['edge']:+.1%}\nFair odds: {assessment['fair_odds']:.2f}\nExpected ROI estimate: {assessment['expected_roi']:+.1%}\nPoisson tail mass omitted: {markets['_tail_mass']:.2%}\nIndependent-Poisson assumptions; not calibrated.")
    except Exception as exc:
        await reply(update, f"Market scenario failed: {type(exc).__name__}: {str(exc)[:220]}")



async def sources_command(update, context):
    """Show which optional research providers have keys configured in Railway."""
    status = provider_key_status()
    lines = [
        "🔌 PUNTER KENYA — RESEARCH SOURCES",
        "",
        "BSD football API: " + ("KEY CONFIGURED" if status["BSD_API_KEY"] else "KEY MISSING"),
        "PitchAPI: " + ("KEY CONFIGURED" if status["PITCHAPI_API_KEY"] else "KEY MISSING"),
        "API-Football: " + ("KEY CONFIGURED" if status["API_FOOTBALL_KEY"] else "KEY MISSING"),
        "Open-Meteo: no key required (not used to create betting probabilities).",
        "",
        "A configured key is not proof that the provider accepted it or covers a particular fixture. "
        "Provider failures and missing statistics are logged internally; SportyBet remains the source "
        "of the prices used in slip previews.",
        "",
        "Free key registration:",
        "BSD: https://sports.bzzoiro.com/register/",
        "PitchAPI: https://pitchapi.dev/",
        "API-Football: https://www.api-football.com/",
    ]
    await reply(update, "\n".join(lines))


def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "Telegram token missing. Set BOT_TOKEN "
            "or TELEGRAM_BOT_TOKEN in your environment."
        )

    logger.info("open-matchday provider selected; no API key required.")

    database_enabled = acquire_polling_lock()
    if database_enabled:
        try:
            migration_result = run_migrations()
            logger.info("Database migrations checked: %s", migration_result)
        except Exception:
            release_polling_lock()
            raise

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    if database_enabled and app.job_queue is not None:
        app.job_queue.run_repeating(
            automatic_bet_settlement_job, interval=6 * 60 * 60, first=60,
            name="automatic-bet-results",
        )
        app.job_queue.run_repeating(
            automatic_price_snapshot_job, interval=30 * 60, first=30,
            name="automatic-bookmaker-price-snapshots",
        )
        app.job_queue.run_repeating(
            automatic_prediction_results_job, interval=6 * 60 * 60, first=180,
            name="automatic-model-forecast-results",
        )
        app.job_queue.run_repeating(
            automatic_specialist_forecast_job, interval=6 * 60 * 60, first=240,
            name="automatic-specialist-forecast-results",
        )
    elif database_enabled:
        logger.warning("Telegram JobQueue unavailable; automatic results and price snapshots are disabled.")

    handlers = [
        ("start", start),
        ("help", help_command),
        ("about", about),
        ("leagues", leagues),
        ("syncleagues", syncleagues),
        ("leaguefixtures", leaguefixtures),
        ("matchintel", matchintel),
        ("fullintel", fullintel_command),
        ("sources", sources_command),
        ("form", form_command),
        ("goals", goals_command),
        ("homeaway", homeaway_command),
        ("halves", halves_command),
        ("h2h", h2h_command),
        ("strength", strength_command),
        ("matchup", matchup_command),
        ("markets", markets_command),
        ("risk", risk_command),
        ("scenarios", scenarios_command),
        ("deepdive", deepdive_command),
        ("value", value_command),
        ("topvalue", topvalue_command),
        ("record", record_command),
        ("calibrate", calibrate_command),
        ("betlog", betlog_command),
        ("betsettle", betsettle_command),
        ("betstats", betstats_command),
        ("performance", betstats_command),
        ("bankroll", bankroll_command),
        ("preferences", preferences_command),
        ("stake", stake_command),
        ("rollover", rollover_command),
        ("rolloverstatus", rolloverstatus_command),
        ("rolloverday", rolloverday_command),
        ("clv", clv_command),
        ("council", council_command),
        ("forecastsettle", forecastsettle_command),
        ("forecaststats", forecaststats_command),
        ("xresearch", xresearch_command),
        ("playerprop", playerprop_command),
        ("marketsim", marketsim_command),
        ("betresults", betresults_command),
        ("marketdata", marketdata_command),
        ("fixtures", fixtures),
        ("syncfixtures", syncfixtures),
        ("live", live),
        ("dbtest", dbtest),
        ("calculator", calculator),
        ("bookies", bookmakers_command),
        ("odds", bookmaker_odds_command),
        ("oddshistory", odds_history_command),
        ("aipicks", aipicks_status_command),
        ("accumulator", accumulator_status_command),
        ("myslips", my_slips_command),
        ("build", build_sportybet_command),
        ("manualbuild", manual_sportybet_command),
        ("slip", build_sportybet_command),
        ("autobuild", autobuild_sportybet_command),
        ("edit", edit_sportybet_command),
        ("merge", merge_sportybet_command),
        ("split", split_sportybet_command),
        ("trim", trim_sportybet_command),
        ("cancel", cancel_slip_command),
    ]

    for command, callback in handlers:

        app.add_handler(
            CommandHandler(
                command,
                callback,
            )
        )

    app.add_handler(CallbackQueryHandler(fullintel_callback, pattern=r"^fi:"))
    app.add_handler(CallbackQueryHandler(sportybet_build_callback, pattern=r"^sb:"))
    app.add_handler(MessageHandler(filters.PHOTO, ai_slip_photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^/myslips$"), my_slips_command))
    
    app.add_error_handler(error_handler)

    logger.info(
        "PUNTER KENYA starting; Telegram polling enabled."
    )

    try:
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    finally:
        release_polling_lock()


if __name__ == "__main__":
    main()