
import os
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
import unicodedata
import re
import math
import threading
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
GOAL_API_KEY = os.getenv("GOAL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
GOAL_API_BASE = os.getenv(
    "GOAL_API_BASE",
    "https://api.goal-api.com/v1"
).rstrip("/")

PROVIDER_NAME = "goal_api"
EAT = ZoneInfo("Africa/Nairobi")

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
    status_forcelist=(429, 500, 502, 503, 504),
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
    """Read list payloads without assuming one exact response envelope."""

    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    candidates = list(preferred_keys) + [
        "items",
        "results",
        "data",
    ]

    for key in candidates:
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for inner_key in candidates:
                inner = value.get(inner_key)

                if isinstance(inner, list):
                    return inner

    return []


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

    except requests.HTTPError as exc:

        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )

        body = ""

        if exc.response is not None:
            body = (exc.response.text or "")[:500]

        raise RuntimeError(
            f"GOAL API HTTP {status}: {body}"
        ) from None

    except requests.RequestException as exc:

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
        "match_id",
    )

    home = first_value(
        raw,
        "homeTeamName",
        "homeTeam",
        "home_team",
        "home",
    )

    away = first_value(
        raw,
        "awayTeamName",
        "awayTeam",
        "away_team",
        "away",
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

    home_score = first_value(
        raw,
        "homeTeamScore",
        "homeScore",
        "home_score",
    )

    away_score = first_value(
        raw,
        "awayTeamScore",
        "awayScore",
        "away_score",
    )

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

    return {
        "id": (
            str(league_id)
            if league_id is not None
            else None
        ),
        "name": (
            str(name)
            if name is not None
            else "Name unavailable"
        ),
        "country": (
            str(country)
            if country is not None
            else "Country unavailable"
        ),
        "raw": raw,
    }


def upcoming_fixtures(raw_fixtures, limit=20):

    now = now_utc_naive()
    valid = []
    seen = set()

    for raw in raw_fixtures:
        item = normalise_fixture(raw)
        if not item:
            continue

        fixture_id = item["provider_match_id"]
        if fixture_id in seen:
            continue
        seen.add(fixture_id)

        if item["kickoff_at"] and item["kickoff_at"] >= now:
            valid.append((item["kickoff_at"], raw, item))

    valid.sort(key=lambda row: row[0])
    return [(raw, item) for _, raw, item in valid[:limit]]


def fetch_all_leagues():
    """Fetch every page of the GOAL API league catalogue."""
    collected = []
    offset = 0
    limit = 100

    for _ in range(50):
        payload = goal_request("leagues", {"limit": limit, "offset": offset})
        rows = extract_leagues(payload)
        if not rows:
            break

        collected.extend(rows)
        meta = response_metadata(payload)
        if meta.get("hasMore") is False:
            break

        total = safe_int(meta.get("total"))
        current_offset = safe_int(meta.get("offset"))
        page_limit = safe_int(meta.get("limit")) or limit
        next_offset = (current_offset if current_offset is not None else offset) + len(rows)

        if total is not None and next_offset >= total:
            break
        if next_offset <= offset:
            break

        offset = next_offset
        limit = page_limit

        if len(rows) < limit and meta.get("hasMore") is not True:
            break

    return collected


def fetch_matchintel_fixture_rows(days=14):
    """
    Fast first-pass fixture lookup for /matchintel.
    Use the provider's date-range endpoint and pagination first; the broader
    date-by-date catalogue scan is reserved as a fallback when no match is found.
    """
    first_day = datetime.now(EAT).date() - timedelta(days=1)
    last_day = first_day + timedelta(days=days + 1)
    collected = []
    seen = set()
    offset = 0
    limit = 100

    for _ in range(10):
        payload = goal_request("fixtures", {
            "from": first_day.isoformat(),
            "to": last_day.isoformat(),
            "limit": limit,
            "offset": offset,
        })
        rows = extract_fixtures(payload)
        if not rows:
            break
        for raw in rows:
            item = normalise_fixture(raw)
            if item and item["provider_match_id"] not in seen:
                seen.add(item["provider_match_id"])
                collected.append(raw)
        meta = response_metadata(payload)
        if meta.get("hasMore") is False:
            break
        total = safe_int(meta.get("total"))
        current_offset = safe_int(meta.get("offset"))
        page_limit = safe_int(meta.get("limit")) or limit
        next_offset = (current_offset if current_offset is not None else offset) + len(rows)
        if (total is not None and next_offset >= total) or next_offset <= offset:
            break
        if len(rows) < page_limit and meta.get("hasMore") is not True:
            break
        offset, limit = next_offset, page_limit
    return collected


def fetch_upcoming_fixture_rows(days=14, league_id=None):
    """
    Fetch a date range through GOAL API's documented /fixtures endpoint.
    This avoids requesting every calendar date separately and ensures the
    search is not limited to whichever competitions happen to be in one
    date-endpoint page. Fall back to the date endpoint if range listing fails
    or returns no usable rows.
    """
    first_day = datetime.now(EAT).date() - timedelta(days=1)
    last_day = first_day + timedelta(days=days + 1)

    def collect_pages(endpoint, base_params):
        collected = []
        seen = set()
        offset = 0
        limit = 100

        for _ in range(50):
            params = dict(base_params)
            params.update({"limit": limit, "offset": offset})
            payload = goal_request(endpoint, params)
            rows = extract_fixtures(payload)
            if not rows:
                break

            for raw in rows:
                item = normalise_fixture(raw)
                if item and item["provider_match_id"] not in seen:
                    seen.add(item["provider_match_id"])
                    collected.append(raw)

            meta = response_metadata(payload)
            if meta.get("hasMore") is False:
                break

            total = safe_int(meta.get("total"))
            current_offset = safe_int(meta.get("offset"))
            page_limit = safe_int(meta.get("limit")) or limit
            next_offset = (
                current_offset if current_offset is not None else offset
            ) + len(rows)

            if total is not None and next_offset >= total:
                break
            if next_offset <= offset:
                break
            if len(rows) < page_limit and meta.get("hasMore") is not True:
                break

            offset = next_offset
            limit = page_limit

        return collected

    range_params = {
        "from": first_day.isoformat(),
        "to": last_day.isoformat(),
    }
    if league_id:
        range_params["leagueId"] = str(league_id)

    # The range endpoint can return a partial first page without an error.
    # Always supplement it with date-scoped results so fixtures in other
    # competitions are not silently missed.
    range_rows = []
    try:
        range_rows = collect_pages("fixtures", range_params)
    except Exception:
        logger.warning(
            "GOAL API date-range fixture request failed; using date-scoped "
            "fixture retrieval.",
            exc_info=True,
        )

    dates = [
        first_day + timedelta(days=day_number)
        for day_number in range(days + 2)
    ]

    def fetch_one_date(fixture_date):
        params = {"limit": 100, "offset": 0}
        if league_id:
            params["leagueId"] = str(league_id)
        try:
            return collect_pages(
                f"fixtures/date/{fixture_date.isoformat()}",
                params,
            )
        except Exception:
            logger.warning(
                "Fixture retrieval failed for %s",
                fixture_date.isoformat(),
                exc_info=True,
            )
            return []

    collected = list(range_rows)
    with ThreadPoolExecutor(max_workers=8) as executor:
        for date_rows in executor.map(fetch_one_date, dates):
            collected.extend(date_rows)

    unique = {}
    for raw in collected:
        item = normalise_fixture(raw)
        if item:
            unique[item["provider_match_id"]] = raw

    return list(unique.values())

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
        "⚽ PUNTER KENYA — ODDS INTELLIGENCE\n\n"
        "Football data collection and research system.\n\n"
        "DATA COMMANDS\n"
        "/diagnose — test GOAL API endpoints\n"
        "/leagues [SEARCH] — search the synced competition catalogue\n"
        "/syncleagues — download all available competitions\n"
        "/leaguefixtures LEAGUE NAME — show fixtures for one competition\n"
        "/matchintel HOME vs AWAY — match intelligence report\n"
        "/fixtures — show the nearest upcoming fixtures\n"
        "/syncfixtures — save upcoming fixtures\n"
        "/live — show live matches\n\n"
        "SYSTEM COMMANDS\n"
        "/dbtest — test database connection\n"
        "/odds — check odds API configuration\n"
        "/bookmakers — show bookmaker registry\n"
        "/compare — odds comparison status\n"
        "/calculator ODDS STAKE — calculate returns\n"
        "/help — show this message\n\n"
        "The system will only label a market as valuable "
        "when verified data and prices support it."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await start(update, context)


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
        "🔄 Downloading the complete GOAL API league catalogue. "
        "This may take several API requests...",
    )
    try:
        raw_rows = await asyncio.to_thread(fetch_all_leagues)
        if not raw_rows:
            await reply(update, "⚠️ GOAL API returned no league records. Nothing was saved.")
            return

        stats = await asyncio.to_thread(save_leagues_to_database, raw_rows)
        await reply(
            update,
            "✅ FULL LEAGUE CATALOGUE SYNC COMPLETE\n\n"
            f"API records collected: {len(raw_rows)}\n"
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
    """Show fixtures by competition name; resolve the provider ID internally."""
    query = " ".join(context.args).strip()
    if not query:
        await reply(
            update,
            "Usage: /leaguefixtures La Liga\n"
            "Enter the competition name as shown by /leagues.",
        )
        return

    try:
        # Accept the exact display format emitted by /leagues:
        # "Competition Name | Region". Resolve the region in Python so
        # database collation/whitespace differences cannot trigger a broad fallback.
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

                    # Remove duplicate database rows without exposing provider IDs.
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
                "/leaguefixtures EXACT COMPETITION NAME\n\n"
                + "\n".join(choices),
            )
            return

        league_id = matches[0]["provider_league_id"]
        league_name = matches[0]["name"]
        await reply(update, f"⚽ Fetching the next 14 days of {league_name} fixtures...")

        raw_rows = await asyncio.to_thread(
            fetch_upcoming_fixture_rows,
            14,
            league_id,
        )
        selected = upcoming_fixtures(raw_rows, limit=20)

        if not selected:
            await reply(
                update,
                f"No upcoming fixtures were returned for {league_name} "
                "in the next 14 days. This may also mean the API has no "
                "fixture coverage for the selected competition.",
            )
            return

        lines = [f"⚽ {league_name.upper()} FIXTURES — {len(selected)} shown", ""]
        for _, item in selected:
            kickoff = (
                item["kickoff_at"]
                .replace(tzinfo=timezone.utc)
                .astimezone(EAT)
            )
            lines.append(
                f"🏆 {item['competition'] or league_name}\n"
                f"🏠 {item['home_team']}\n"
                f"🆚 {item['away_team']}\n"
                f"🕒 {kickoff:%d %b %Y — %H:%M EAT}\n"
            )

        await reply(update, "\n".join(lines))

    except Exception as exc:
        logger.exception("League fixture retrieval failed")
        await reply(
            update,
            f"❌ LEAGUE FIXTURE REQUEST FAILED\n\n{str(exc)[:700]}",
        )


async def fixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "⚽ Fetching upcoming fixtures from GOAL API...",
    )

    try:

        raw_rows = await asyncio.to_thread(
            fetch_upcoming_fixture_rows,
            14,
        )

        selected = upcoming_fixtures(
            raw_rows,
            limit=20,
        )

        if not selected:

            await reply(
                update,
                "No upcoming fixtures were returned in the 14-day date search.\n"
                f"Date records received: {len(raw_rows)}\n"
                "Try again later or check the selected competition's coverage.",
            )

            return

        lines = [
            f"⚽ UPCOMING FIXTURES — {len(selected)} shown",
            "",
        ]

        for _, item in selected:

            kickoff = (
                item["kickoff_at"]
                .replace(tzinfo=timezone.utc)
                .astimezone(EAT)
            )

            lines.append(
                f"🏆 {item['competition'] or 'Competition unavailable'}\n"
                f"🏠 {item['home_team']}\n"
                f"🆚 {item['away_team']}\n"
                f"🕒 {kickoff:%d %b %Y — %H:%M EAT}\n"
            )

        await reply(
            update,
            "\n".join(lines),
        )

    except Exception as exc:

        logger.exception(
            "Fixture retrieval failed"
        )

        await reply(
            update,
            f"❌ FIXTURE REQUEST FAILED\n\n{str(exc)[:700]}",
        )


# ============================================================
# FIXTURE DATABASE SYNC
# ============================================================

async def syncfixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "🔄 Retrieving upcoming fixtures and saving valid records to TiDB...",
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
            f"API records received: {len(raw_rows)}\n"
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
        "🔴 Checking live matches...",
    )

    try:

        payload = await asyncio.to_thread(
            goal_request,
            "fixtures/live",
            {},
        )

        rows = extract_fixtures(payload)

        if not rows:

            await reply(
                update,
                "No live matches were returned by the API at this moment.",
            )

            return

        lines = [
            f"🔴 LIVE MATCHES — {len(rows)} returned",
            "",
        ]

        for raw in rows[:25]:

            item = normalise_fixture(raw)

            if not item:
                continue

            hs = (
                item["home_score"]
                if item["home_score"] is not None
                else "—"
            )

            aws = (
                item["away_score"]
                if item["away_score"] is not None
                else "—"
            )

            lines.append(
                f"⚽ {item['home_team']} "
                f"{hs}–{aws} {item['away_team']}\n"
                f"⏱ {item['status'] or 'Live'}"
            )

        await reply(
            update,
            "\n\n".join(lines),
        )

    except Exception as exc:

        logger.exception(
            "Live fixtures request failed"
        )

        await reply(
            update,
            f"❌ LIVE MATCH REQUEST FAILED\n\n{str(exc)[:700]}",
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
    nested = first_value(raw, f"{side}Team", side.lower())
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
            await reply(
                update,
                "❌ I couldn't find that match in the next 14 days of fixtures.\n"
                "Check the team names or use /fixtures to see upcoming matches.",
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
        fixture_id = first_value(raw, "id", "fixtureId", "matchId", "match_id")
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

        endpoint_results = await asyncio.gather(
            asyncio.to_thread(goal_request, f"fixtures/{fixture_id}", {}),
            asyncio.to_thread(goal_request, f"fixtures/{fixture_id}/lineups", {}),
            asyncio.to_thread(goal_request, f"fixtures/{fixture_id}/statistics", {}),
            asyncio.to_thread(goal_request, f"fixtures/{fixture_id}/odds", {}),
            asyncio.to_thread(goal_request, f"fixtures/{fixture_id}/events", {}),
            return_exceptions=True,
        )
        labels = ("DETAILS", "LINEUPS", "STATISTICS", "ODDS", "EVENTS")
        results = dict(zip(labels, endpoint_results))

        details = results["DETAILS"]
        if not isinstance(details, Exception):
            detail_data = unwrap_api_data(details)
            if isinstance(detail_data, dict):
                score_home = first_value(detail_data, "homeTeamScore", "homeScore")
                score_away = first_value(detail_data, "awayTeamScore", "awayScore")
                if score_home is not None or score_away is not None:
                    header.append(f"📊 Score: {score_home if score_home is not None else '—'} – {score_away if score_away is not None else '—'}")

        output = header + ["", "📋 LINEUPS"]
        lineups = results["LINEUPS"]
        if isinstance(lineups, Exception):
            output.append("• Lineup endpoint unavailable: " + str(lineups)[:180])
        else:
            output.extend(format_match_lineups(lineups))

        output.extend(["", "📈 MATCH STATISTICS"])
        stats = results["STATISTICS"]
        if isinstance(stats, Exception):
            output.append("• Statistics unavailable from provider.")
        else:
            stat_lines = format_stat_rows(extract_match_statistics(stats))
            output.extend(stat_lines or ["• No match statistics published yet (often normal before kick-off)."])

        output.extend(["", "💹 AVAILABLE ODDS"])
        odds_result = results["ODDS"]
        if isinstance(odds_result, Exception):
            output.append("• Odds endpoint unavailable for this match.")
        else:
            odds_lines = format_match_odds(odds_result)
            output.extend(odds_lines or ["• No bookmaker prices returned for this match."])

        output.extend(["", "🗓 MATCH EVENTS"])
        events_result = results["EVENTS"]
        if isinstance(events_result, Exception):
            output.append("• Events not available.")
        else:
            event_lines = format_match_events(events_result)
            output.extend(event_lines or ["• No events published yet."])

        output.extend(["", "🧠 PRE-MATCH PREDICTION"])
        await reply(update, "📊 Match data retrieved. Building the pre-match forecast...")
        try:
            prediction = await asyncio.to_thread(build_pre_match_prediction, raw, item)
        except Exception as prediction_error:
            logger.warning("Pre-match model data request failed: %s", prediction_error)
            prediction = {"available": False, "reason": "Recent team results could not be retrieved: " + str(prediction_error)[:220]}

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
                f"• Avg goals scored/conceded: Home {prediction['home_scored']:.2f}/{prediction['home_conceded']:.2f} | Away {prediction['away_scored']:.2f}/{prediction['away_conceded']:.2f}",
                f"• Recent results used: {prediction['home_results']} home-team matches; {prediction['away_results']} away-team matches",
                "",
                "📚 HOME TEAM — LAST MATCHES (newest first)",
            ])
            for match in prediction["home_recent_matches"]:
                match_date = match["date"].strftime("%d %b %Y") if match["date"] else "Date n/a"
                output.append(f"• {match_date} | {match['venue']} vs {match['opponent']} | {match['score']} | {match['result']}")
            output.extend(["", "📚 AWAY TEAM — LAST MATCHES (newest first)"])
            for match in prediction["away_recent_matches"]:
                match_date = match["date"].strftime("%d %b %Y") if match["date"] else "Date n/a"
                output.append(f"• {match_date} | {match['venue']} vs {match['opponent']} | {match['score']} | {match['result']}")
            output.extend([
                f"• Model confidence: {prediction['confidence']}",
                f"• Outcome lean: {prediction['top_outcome']} (highest model probability, not a guaranteed result)",
                "• BET / NO BET: NO BET — verified bookmaker odds are not available for value comparison.",
            ])
        else:
            output.extend([
                "• Forecast not produced: " + prediction.get("reason", "Insufficient verified data."),
                "• BET / NO BET: NO BET — do not stake without a data-backed forecast and verified odds.",
            ])

        output.extend([
            "",
            "⚠️ MODEL LIMITATIONS",
            "• This is a pre-lineup statistical forecast from recent results; it does not invent starting XIs.",
            "• Injuries, suspensions, tactical matchups and predicted lineups are not yet verified model inputs.",
            "• Probabilities are estimates, not guarantees. No stake recommendation is made without verified prices.",
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

    if not GOAL_API_KEY:

        logger.warning(
            "GOAL_API_KEY is not set; "
            "GOAL API commands will fail until configured."
        )

    acquire_polling_lock()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    handlers = [
        ("start", start),
        ("help", help_command),
        ("diagnose", diagnose),
        ("leagues", leagues),
        ("syncleagues", syncleagues),
        ("leaguefixtures", leaguefixtures),
        ("matchintel", matchintel),
        ("fixtures", fixtures),
        ("syncfixtures", syncfixtures),
        ("live", live),
        ("dbtest", dbtest),
        ("odds", odds),
        ("bookmakers", bookmakers),
        ("compare", compare),
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
