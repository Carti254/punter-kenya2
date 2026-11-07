
import os
import logging
import asyncio
import csv
import io
from concurrent.futures import ThreadPoolExecutor
import unicodedata
import re
import math
import threading
import json
import time
import copy
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from database import get_connection, test_connection

# ============================================================
# PUNTER KENYA — TELEGRAM DATA BOT
# Stable foundation: API diagnostics, league discovery,
# fixture retrieval, database storage and utility commands.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
OPENMATCHDAY_RAW_BASE = "https://raw.githubusercontent.com/oladeeayo/open-matchday/master"
OPENMATCHDAY_INDEX_URL = f"{OPENMATCHDAY_RAW_BASE}/LEAGUE_INDEX.md"

PROVIDER_NAME = "open-matchday"
EAT = ZoneInfo("Africa/Nairobi")
FIXTURE_RESPONSE_DIAGNOSTICS = []

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
    """Ensure only one Railway/local process polls Telegram for this bot."""
    global _polling_lock_connection, _polling_lock_keepalive_thread

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s) AS acquired", (POLLING_LOCK_NAME, 10))
            row = cursor.fetchone()
        acquired = row.get("acquired") if isinstance(row, dict) else None
        if acquired != 1:
            raise RuntimeError(
                "Another Punter Kenya process already owns the Telegram polling lock. "
                "Stop the duplicate Railway replica or local bot process."
            )
        _polling_lock_connection = connection
    except Exception:
        connection.close()
        raise

    def keep_lock_alive():
        while not _polling_lock_keepalive_stop.wait(45):
            try:
                # The connection is dedicated to the advisory lock.
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

    if not GOAL_API_KEY:
        raise RuntimeError(
            "GOAL_API_KEY is not configured in Railway variables."
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
    """Return the shared season code used by open-matchday."""
    now = datetime.now(EAT)
    start_year = now.year if now.month >= 7 else now.year - 1
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


def fetch_openmatchday_csv(folder, kind="fixtures"):
    """Fetch one public CSV file; cache successful responses to reduce traffic."""
    season = openmatchday_season_code()
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
    goal_store_response(cache_key, "fixtures", rows)
    return rows


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
            if day_filter:
                if kickoff_eat_date != target_date:
                    continue
            elif kickoff_naive < now or kickoff_naive > cutoff:
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

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_one, league) for league in leagues]
        for future in futures:
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

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "⚽ PUNTER KENYA — FOOTBALL INTELLIGENCE\n\n"
        "Free football fixtures and pre-match research.\n\n"
        "FIXTURES & COMPETITIONS\n"
        "/leagues [SEARCH] — search the 75-competition catalogue\n"
        "/syncleagues — refresh the competition catalogue\n"
        "/fixtures [PAGE] — browse upcoming fixtures\n"
        "/leaguefixtures LEAGUE [today|tomorrow] — choose a league and day\n"
        "/syncfixtures — save upcoming fixtures to the database\n\n"
        "MATCH ANALYSIS\n"
        "/matchintel HOME vs AWAY — initial match assessment\n"
        "/form — recent form and points rate\n"
        "/goals — goals scored/conceded and scoring rates\n"
        "/homeaway — venue-specific performance\n"
        "/halves — half-time data availability\n"
        "/h2h — head-to-head results\n"
        "/strength — attack, defence and consistency\n"
        "/matchup — attack-versus-defence comparison\n"
        "/markets — model probability estimates\n"
        "/risk — uncertainty and risk flags\n"
        "/scenarios — possible match scripts\n"
        "/deepdive — full analysis with simple summary\n"
        "/value — compare supplied 1X2 odds with model estimates\n"
        "/topvalue — value scanner status\n"
        "/live — explain live-data availability\n\n"
        "TOOLS\n"
        "/calculator ODDS STAKE — calculate potential returns\n"
        "/dbtest — test the database connection\n"
        "/about — data source and project information\n"
        "/help — show this message\n\n"
        "Data is refreshed daily, not live. Forecasts are estimates; "
        "the bot does not provide guaranteed picks or verified bookmaker odds."
    )


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

        selected = upcoming_fixtures(raw_rows, limit=40, include_past=True)
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

        all_selected = upcoming_fixtures(raw_rows, include_past=True)
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
    Match full and abbreviated provider team names.
    Examples: Atletico Madrid <-> Atl. Madrid; Athletic Bilbao <-> Ath Bilbao.
    """
    def normalised(value):
        value = unicodedata.normalize("NFKD", str(value or ""))
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        words = re.findall(r"[a-z0-9]+", value.casefold())
        phrase = " ".join(words)

        aliases = {
            "atl madrid": "atletico madrid",
            "atletico de madrid": "atletico madrid",
            "ath bilbao": "athletic bilbao",
            "athletic club": "athletic bilbao",
            "dep la coruna": "deportivo la coruna",
            "deportivo coruna": "deportivo la coruna",
        }
        phrase = aliases.get(phrase, phrase)
        return set(phrase.split())

    wanted = normalised(query_name)
    actual = normalised(actual_name)
    if not wanted or not actual:
        return False
    if wanted.issubset(actual) or actual.issubset(wanted):
        return True

    # Allow common provider short forms when all remaining words agree.
    abbreviations = {
        "atletico": {"atl"},
        "athletic": {"ath"},
        "deportivo": {"dep"},
    }
    for full, short_forms in abbreviations.items():
        if full in wanted and (short_forms & actual):
            wanted = (wanted - {full}) | (short_forms & actual)
        if full in actual and (short_forms & wanted):
            actual = (actual - {full}) | (short_forms & wanted)
    return wanted == actual


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



def build_openmatchday_prediction(raw_fixture, fixture):
    """Forecast a fixture from recent results in its own competition CSV."""
    folder = str(raw_fixture.get("leagueShortcut") or "").strip()
    if not folder:
        return {"available": False, "reason": "The fixture has no competition reference for retrieving its results."}

    try:
        rows = fetch_openmatchday_csv(folder, "results")
    except Exception as exc:
        logger.warning("Could not load historical results for %s: %s", folder, exc)
        return {"available": False, "reason": "Historical results could not be retrieved for this competition."}

    def parse_team_history(team_name):
        parsed = []
        for row in rows:
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
    lambda_home = max(0.15, min(4.0, ((home_scored + away_conceded) / 2) * 1.08))
    lambda_away = max(0.15, min(4.0, ((away_scored + home_conceded) / 2) * 0.92))

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
    confidence = "LOW" if len(home_results) + len(away_results) < 14 else "MODERATE"

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

async def topvalue_command(update, context):
    await reply(update, "📊 TOP VALUE SCANNER\n\nNo verified bookmaker odds feed is connected. PUNTER cannot honestly rank value bets without current prices.\n\nUse /value TEAM A vs TEAM B HOME_ODDS DRAW_ODDS AWAY_ODDS to compare your supplied 1X2 prices with the model. No odds = no value verdict.")

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
    if pred.get("available"):
        lines.extend([f"Expected goals: {pred['lambda_home']:.2f}-{pred['lambda_away']:.2f}",
            f"Most likely score: {pred['score'][0]}-{pred['score'][1]}",
            f"1X2: Home {pred['home_win']:.1%} | Draw {pred['draw']:.1%} | Away {pred['away_win']:.1%}",
            f"Over 1.5: {pred['over_15']:.1%} | Over 2.5: {pred['over_25']:.1%}"])
    else:
        lines.append("Forecast withheld: " + pred.get("reason", "insufficient data"))
    lines.extend(["", "7. PLAIN-ENGLISH SUMMARY",
        f"{edge} recent-points advantage. " +
        ("The data shows a measurable difference, but it is not enough on its own to justify a bet." if edge != "No clear" else "Recent points rates are close; this matchup has no clear form leader."),
        "",
        "8. BETTING DECISION",
        "NO BET — no verified bookmaker odds are connected, so value cannot be established.",
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

    names = [
        "BetPawa",
        "SportyBet",
        "SportPesa",
        "Odibets",
        "Mozzart Bet",
        "1xBet",
    ]

    await reply(
        update,
        "🏦 INITIAL BOOKMAKER REGISTRY\n\n"
        + "\n".join(
            f"• {name}"
            for name in names
        )
        + "\n\nThese are registry names only; "
        "live feeds are not yet verified.",
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

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "Telegram token missing. Set BOT_TOKEN "
            "or TELEGRAM_BOT_TOKEN in Railway."
        )

    logger.info("open-matchday provider selected; no API key required.")

    acquire_polling_lock()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    handlers = [
        ("start", start),
        ("help", help_command),
        ("about", about),
        ("leagues", leagues),
        ("syncleagues", syncleagues),
        ("leaguefixtures", leaguefixtures),
        ("matchintel", matchintel),
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
        ("fixtures", fixtures),
        ("syncfixtures", syncfixtures),
        ("live", live),
        ("dbtest", dbtest),
        ("calculator", calculator),
    ]

    for command, callback in handlers:

        app.add_handler(
            CommandHandler(
                command,
                callback,
            )
        )

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