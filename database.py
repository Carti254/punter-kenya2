
import os
import logging
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from database import get_connection, test_connection


# ============================================================
# PUNTER KENYA — TELEGRAM FOOTBALL DATA FOUNDATION
# ============================================================

BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
)

GOAL_API_KEY = os.getenv("GOAL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

GOAL_API_BASE = os.getenv(
    "GOAL_API_BASE",
    "https://api.goal-api.com/v1",
).rstrip("/")

PROVIDER_NAME = "goal_api"
EAT = ZoneInfo("Africa/Nairobi")

MAX_LEAGUE_PAGES = 100
LEAGUE_PAGE_SIZE = 100

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("punter_kenya")

HTTP = requests.Session()

RETRY = Retry(
    total=3,
    connect=3,
    read=2,
    status=3,
    backoff_factor=0.6,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    respect_retry_after_header=True,
)

HTTP.mount("https://", HTTPAdapter(max_retries=RETRY))
HTTP.mount("http://", HTTPAdapter(max_retries=RETRY))


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc_naive():
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
        try:
            parsed = datetime.fromisoformat(
                str(value).strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None

    if parsed.tzinfo is None:
        return parsed

    return parsed.astimezone(
        timezone.utc
    ).replace(tzinfo=None)


def extract_list(payload, preferred_keys=()):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    keys = list(preferred_keys) + [
        "items",
        "results",
        "data",
    ]

    for key in keys:
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for inner_key in keys:
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

    if not isinstance(metadata, dict):
        return {}

    return metadata


def metadata_bool(value):
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() in (
            "true",
            "1",
            "yes",
        )

    if isinstance(value, (int, float)):
        return bool(value)

    return None


# ============================================================
# GOAL API CONNECTION
# ============================================================

def goal_request(endpoint, params=None):

    if not GOAL_API_KEY:
        raise RuntimeError(
            "GOAL_API_KEY is not configured in Railway."
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

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            "GOAL API returned invalid JSON."
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

    if not fixture_id or not home_name or not away_name:
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
        "region",
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

    return {
        "provider": PROVIDER_NAME,
        "provider_match_id": str(fixture_id),
        "competition": str(competition) if competition else None,
        "country": str(country) if country else None,
        "home_team": str(home_name),
        "away_team": str(away_name),
        "kickoff_at": parse_datetime(kickoff),
        "status": str(status) if status else None,
        "home_score": safe_int(
            first_value(raw, "homeTeamScore", "homeScore", "home_score")
        ),
        "away_score": safe_int(
            first_value(raw, "awayTeamScore", "awayScore", "away_score")
        ),
    }


# ============================================================
# LEAGUE NORMALISATION
# ============================================================

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

    region = first_value(
        raw,
        "region",
        "countryName",
        "country",
        "area",
    )

    if isinstance(region, dict):
        region = nested_name(region)

    if not league_id or not name:
        return None

    return {
        "id": str(league_id),
        "name": str(name),
        "region": str(region or "Unknown"),
    }


def upcoming_fixtures(raw_fixtures, limit=20):

    now = now_utc_naive()
    valid = []

    for raw in raw_fixtures:
        item = normalise_fixture(raw)

        if (
            item
            and item["kickoff_at"]
            and item["kickoff_at"] >= now
        ):
            valid.append(
                (item["kickoff_at"], raw, item)
            )

    valid.sort(key=lambda row: row[0])

    return [
        (raw, item)
        for _, raw, item in valid[:limit]
    ]


# ============================================================
# DATABASE HELPERS
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
        row["COLUMN_NAME"].lower()
        for row in cursor.fetchall()
    }


# ============================================================
# COMPLETE LEAGUE CATALOGUE SYNCHRONISATION
# ============================================================

def sync_leagues_to_database():

    connection = get_connection()

    total_received = 0
    total_processed = 0
    total_skipped = 0
    pages_processed = 0
    api_total = None
    offset = 0

    try:
        with connection.cursor() as cursor:

            columns = get_table_columns(
                cursor,
                "leagues",
            )

            required = {
                "provider",
                "provider_league_id",
                "name",
                "region",
            }

            missing = required - columns

            if missing:
                raise RuntimeError(
                    "Leagues table is missing columns: "
                    + ", ".join(sorted(missing))
                )

            sql = """
                INSERT INTO leagues
                (
                    provider,
                    provider_league_id,
                    name,
                    region
                )
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    region = VALUES(region)
            """

            while pages_processed < MAX_LEAGUE_PAGES:

                payload = goal_request(
                    "leagues",
                    {
                        "limit": LEAGUE_PAGE_SIZE,
                        "offset": offset,
                    },
                )

                rows = extract_leagues(payload)
                metadata = response_metadata(payload)

                if not rows:
                    if offset == 0:
                        raise RuntimeError(
                            "The API returned no leagues on its first page."
                        )
                    break

                pages_processed += 1
                total_received += len(rows)

                reported_total = safe_int(
                    metadata.get("total")
                )

                if reported_total is not None:
                    api_total = reported_total

                batch = []

                for raw in rows:

                    league = normalise_league(raw)

                    if not league:
                        total_skipped += 1
                        continue

                    batch.append((
                        PROVIDER_NAME,
                        league["id"],
                        league["name"],
                        league["region"],
                    ))

                if batch:
                    cursor.executemany(sql, batch)
                    total_processed += len(batch)

                offset += len(rows)

                has_more = metadata_bool(
                    metadata.get(
                        "hasMore",
                        metadata.get("has_more"),
                    )
                )

                if has_more is False:
                    break

                if has_more is True:
                    continue

                if api_total is not None:
                    if offset >= api_total:
                        break
                elif len(rows) < LEAGUE_PAGE_SIZE:
                    break

            else:
                raise RuntimeError(
                    "Safety limit reached. "
                    "Catalogue may be incomplete. "
                    "Run synchronisation again."
                )

        return {
            "received": total_received,
            "processed": total_processed,
            "skipped": total_skipped,
            "pages": pages_processed,
            "api_total": api_total,
        }

    finally:
        connection.close()


def get_league_database_stats():

    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(DISTINCT region) AS regions
                FROM leagues
                WHERE provider = %s
                """,
                (PROVIDER_NAME,),
            )

            return cursor.fetchone()

    finally:
        connection.close()


def get_leagues_from_database(search="", limit=25):

    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            if search:
                cursor.execute(
                    """
                    SELECT
                        provider_league_id,
                        name,
                        region
                    FROM leagues
                    WHERE provider = %s
                    AND name LIKE %s
                    ORDER BY name
                    LIMIT %s
                    """,
                    (
                        PROVIDER_NAME,
                        f"%{search}%",
                        limit,
                    ),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        provider_league_id,
                        name,
                        region
                    FROM leagues
                    WHERE provider = %s
                    ORDER BY name
                    LIMIT %s
                    """,
                    (PROVIDER_NAME, limit),
                )

            return cursor.fetchall()

    finally:
        connection.close()


# ============================================================
# FIXTURE DATABASE STORAGE
# ============================================================

def save_fixtures_to_database(raw_fixtures):

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
                    "Matches table is missing columns: "
                    + ", ".join(sorted(missing))
                )

            optional = [
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

            fields.extend(
                field for field in optional
                if field in columns
            )

            assignments = [
                f"`{field}` = VALUES(`{field}`)"
                for field in fields
                if field not in (
                    "provider",
                    "provider_match_id",
                )
            ]

            sql = (
                "INSERT INTO matches ("
                + ", ".join(f"`{field}`" for field in fields)
                + ") VALUES ("
                + ", ".join(["%s"] * len(fields))
                + ") ON DUPLICATE KEY UPDATE "
                + ", ".join(assignments)
            )

            for raw in raw_fixtures:

                item = normalise_fixture(raw)

                if not item:
                    skipped += 1
                    continue

                values = []

                for field in fields:
                    if field == "last_synced_at":
                        values.append(now_utc_naive())
                    else:
                        values.append(item.get(field))

                cursor.execute(sql, tuple(values))
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
        "LEAGUE COMMANDS\n"
        "/leagues — browse stored competitions\n"
        "/syncleagues — synchronise all competitions\n"
        "/leaguestats — database catalogue statistics\n\n"
        "FOOTBALL DATA\n"
        "/diagnose — test GOAL API endpoints\n"
        "/fixtures — show upcoming fixtures\n"
        "/syncfixtures — save upcoming fixtures\n"
        "/live — show live matches\n\n"
        "SYSTEM COMMANDS\n"
        "/dbtest — test database connection\n"
        "/odds — check odds API configuration\n"
        "/bookmakers — show bookmaker registry\n"
        "/compare — odds comparison status\n"
        "/calculator ODDS STAKE — calculate returns\n"
        "/help — show this message\n\n"
        "Recommendations will only be made when "
        "verified evidence and prices support them."
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
        result = await asyncio.to_thread(test_connection)

        await reply(
            update,
            "✅ DATABASE CONNECTION SUCCESSFUL\n\n"
            f"Database: {result.get('database_name', 'unknown')}\n"
            f"Server: {result.get('database_version', 'unknown')}",
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
        "🧪 Running GOAL API diagnostics...",
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
            metadata = response_metadata(payload)

            message = (
                f"✅ {label} ENDPOINT\n"
                f"Endpoint: /{endpoint}\n"
                f"Records detected: {len(rows)}\n"
                f"Pagination metadata: "
                f"{str(metadata)[:500] if metadata else 'not supplied'}"
            )

            if label == "LEAGUES" and rows:

                samples = []

                for raw in rows[:5]:
                    item = normalise_league(raw)

                    if item:
                        samples.append(
                            f"• {item['name']} — "
                            f"{item['region']} "
                            f"(ID: {item['id']})"
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
# FULL LEAGUE SYNCHRONISATION COMMAND
# ============================================================

async def syncleagues(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "🔄 PUNTER KENYA LEAGUE SYNCHRONISATION\n\n"
        "Downloading the complete competition catalogue.\n"
        "Pages are being processed and saved to TiDB.\n\n"
        "Please wait...",
    )

    try:
        stats = await asyncio.to_thread(
            sync_leagues_to_database
        )

        await reply(
            update,
            "✅ LEAGUE SYNCHRONISATION COMPLETE\n\n"
            f"API total reported: {stats['api_total'] or 'Not supplied'}\n"
            f"Pages processed: {stats['pages']}\n"
            f"Records received: {stats['received']}\n"
            f"Records inserted/updated: {stats['processed']}\n"
            f"Records skipped: {stats['skipped']}\n\n"
            "Run /leaguestats to verify the database.",
        )

    except Exception as exc:
        logger.exception("League synchronisation failed")

        await reply(
            update,
            "❌ LEAGUE SYNCHRONISATION FAILED\n\n"
            f"{str(exc)[:900]}",
        )


# ============================================================
# LEAGUE DATABASE STATISTICS
# ============================================================

async def leaguestats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    try:
        stats = await asyncio.to_thread(
            get_league_database_stats
        )

        await reply(
            update,
            "📊 PUNTER KENYA LEAGUE DATABASE\n\n"
            f"Stored competitions: {stats['total']}\n"
            f"Distinct regions: {stats['regions']}\n\n"
            "Use /syncleagues to refresh the catalogue.",
        )

    except Exception as exc:
        logger.exception("League statistics failed")

        await reply(
            update,
            f"❌ DATABASE STATISTICS FAILED\n\n{str(exc)[:700]}",
        )


# ============================================================
# DATABASE-BACKED LEAGUE BROWSER
# ============================================================

async def leagues(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    search = " ".join(context.args).strip()

    try:
        rows = await asyncio.to_thread(
            get_leagues_from_database,
            search,
            25,
        )

        if not rows:
            await reply(
                update,
                "No stored competitions found.\n\n"
                "Run /syncleagues first.",
            )
            return

        title = (
            f"🏆 SEARCH RESULTS: {search}"
            if search
            else "🏆 STORED LEAGUE CATALOGUE"
        )

        lines = [
            title,
            "",
        ]

        for row in rows:
            lines.append(
                f"• {row['name']}\n"
                f"  Region: {row['region']}\n"
                f"  ID: {row['provider_league_id']}"
            )

        lines.append(
            "\nShowing up to 25 competitions."
        )

        lines.append(
            "Search with /leagues NAME"
        )

        await reply(
            update,
            "\n\n".join(lines),
        )

    except Exception as exc:
        logger.exception("League browsing failed")

        await reply(
            update,
            f"❌ LEAGUE DATABASE ERROR\n\n{str(exc)[:700]}",
        )


# ============================================================
# UPCOMING FIXTURES
# ============================================================

async def fixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await reply(
        update,
        "⚽ Fetching upcoming fixtures from GOAL API...",
    )

    try:
        payload = await asyncio.to_thread(
            goal_request,
            "fixtures",
            {"limit": 100, "offset": 0},
        )

        raw_rows = extract_fixtures(payload)

        selected = upcoming_fixtures(
            raw_rows,
            limit=20,
        )

        if not selected:
            await reply(
                update,
                "No upcoming fixtures could be identified "
                "in the first API page.\n"
                f"Records received: {len(raw_rows)}\n"
                "This does not prove there are no fixtures.",
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
        logger.exception("Fixture retrieval failed")

        await reply(
            update,
            f"❌ FIXTURE REQUEST FAILED\n\n{str(exc)[:700]}",
        )


# ============================================================
# FIXTURE DATABASE SYNCHRONISATION
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
        payload = await asyncio.to_thread(
            goal_request,
            "fixtures",
            {"limit": 100, "offset": 0},
        )

        raw_rows = extract_fixtures(payload)

        selected = [
            raw
            for raw, _ in upcoming_fixtures(
                raw_rows,
                limit=100,
            )
        ]

        if not selected:
            await reply(
                update,
                "⚠️ No upcoming fixtures identified "
                "on the first API page. Nothing was saved.\n"
                f"Records received: {len(raw_rows)}",
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
            "Note: this command processes the first API page only.",
        )

    except Exception as exc:
        logger.exception("Fixture database sync failed")

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
                "No live matches were returned by the API.",
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
        logger.exception("Live fixtures request failed")

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
        + "\n".join(f"• {name}" for name in names)
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
            "This is arithmetic, not a prediction.",
        )

    except (ValueError, OverflowError):
        await reply(
            update,
            "❌ Enter valid positive numbers.\n"
            "Example: /calculator 2.50 100",
        )


# ============================================================
# TELEGRAM ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    error = context.error

    if error:
        logger.error(
            "Unhandled Telegram handler error",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
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

        # League catalogue
        ("leagues", leagues),
        ("syncleagues", syncleagues),
        ("leaguestats", leaguestats),

        # Football data
        ("fixtures", fixtures),
        ("syncfixtures", syncfixtures),
        ("live", live),

        # System
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
