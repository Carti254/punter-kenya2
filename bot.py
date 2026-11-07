
import os
import logging
import asyncio
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

# Reuse HTTP connections and retry temporary upstream failures.
HTTP = requests.Session()

_RETRY = Retry(
    total=3,
    connect=3,
    read=2,
    status=3,
    backoff_factor=0.6,
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
            timeout=(10, 35),
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
        "date",
    )

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


def fetch_upcoming_fixture_rows(days=14, league_id=None):
    """
    Use the documented date endpoint instead of scanning the first page
    of the entire fixtures database. Include yesterday for UTC/EAT boundary
    coverage and paginate each date where the API reports more records.
    """
    first_day = datetime.now(EAT).date() - timedelta(days=1)
    collected = []
    seen = set()
    limit = 100

    for day_number in range(days + 2):
        fixture_date = first_day + timedelta(days=day_number)
        offset = 0

        for _ in range(20):
            params = {"limit": limit, "offset": offset}
            if league_id:
                params["leagueId"] = str(league_id)

            payload = goal_request(
                f"fixtures/date/{fixture_date.isoformat()}",
                params,
            )
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

            if total is not None and next_offset >= total:
                break
            if next_offset <= offset:
                break
            if len(rows) < page_limit and meta.get("hasMore") is not True:
                break

            offset = next_offset

    return collected

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
        # "Competition Name | Region". Keep provider IDs internal.
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
                              AND LOWER(TRIM(COALESCE(region, ''))) = LOWER(TRIM(%s))
                            ORDER BY name LIMIT 100
                            """,
                            (PROVIDER_NAME, competition_name, competition_region),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT provider_league_id, name, region
                            FROM leagues
                            WHERE provider = %s AND
                                  (LOWER(TRIM(name)) = LOWER(TRIM(%s))
                                   OR provider_league_id = %s)
                            ORDER BY name LIMIT 100
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
                            ORDER BY name LIMIT 100
                            """,
                            (PROVIDER_NAME, f"%{competition_name}%"),
                        )
                        matches = cursor.fetchall()
                    return matches
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

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
