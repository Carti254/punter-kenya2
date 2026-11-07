import os
import logging
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from database import get_connection, test_connection


# ==========================================================
# PUNTER KENYA - ODDS INTELLIGENCE AI
# Version 3.1 - filtered and sorted upcoming fixtures
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOAL_API_KEY = os.getenv("GOAL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

GOAL_API_BASE = "https://api.goal-api.com/v1"
PROVIDER_NAME = "goal_api"

EAT = ZoneInfo("Africa/Nairobi")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("punter_kenya")


# ==========================================================
# GENERAL HELPERS
# ==========================================================

def eat_now():
    return datetime.now(EAT)


def safe_int(value):
    if value is None or value == "":
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
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
        return first_value(value, "name", "teamName", "title")

    if isinstance(value, str):
        return value

    return None


def parse_kickoff(value):
    if not value:
        return None

    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )

        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return dt

    except (TypeError, ValueError):
        return None


def extract_fixtures(response):
    """
    Supports the normal GOAL API data-list response
    and common nested response structures.
    """

    if isinstance(response, list):
        return response

    if not isinstance(response, dict):
        return []

    data = response.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("fixtures", "matches", "results"):
            if isinstance(data.get(key), list):
                return data[key]

    for key in ("fixtures", "matches", "results"):
        if isinstance(response.get(key), list):
            return response[key]

    return []


# ==========================================================
# GOAL API CONNECTION
# ==========================================================

def goal_request(endpoint, params=None):
    if not GOAL_API_KEY:
        raise RuntimeError("GOAL_API_KEY is missing in Railway.")

    url = f"{GOAL_API_BASE}/{endpoint.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {GOAL_API_KEY}",
        "Accept": "application/json",
    }

    response = requests.get(
        url,
        headers=headers,
        params=params or {},
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(
            result.get("message", "GOAL API returned an error.")
        )

    return result


# ==========================================================
# FIXTURE NORMALISATION
# ==========================================================

def normalise_fixture(fixture):
    if not isinstance(fixture, dict):
        return None

    fixture_id = first_value(
        fixture,
        "id",
        "fixtureId",
        "matchId",
        "match_id",
    )

    home = first_value(
        fixture,
        "homeTeamName",
        "homeTeam",
        "home_team",
        "home",
    )

    away = first_value(
        fixture,
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
        fixture,
        "leagueName",
        "competitionName",
        "competition",
        "league",
    )

    country = first_value(
        fixture,
        "countryName",
        "country",
    )

    if isinstance(competition, dict):
        if not country:
            country = nested_name(
                competition.get("country")
            )

        competition = nested_name(competition)

    kickoff = first_value(
        fixture,
        "kickoffUtc",
        "kickoff_utc",
        "dateTime",
        "matchDateTime",
        "date",
    )

    status = first_value(
        fixture,
        "matchStatus",
        "status",
        "fixtureStatus",
    )

    home_score = first_value(
        fixture,
        "homeTeamScore",
        "homeScore",
        "home_score",
    )

    away_score = first_value(
        fixture,
        "awayTeamScore",
        "awayScore",
        "away_score",
    )

    return {
        "provider_match_id": str(fixture_id),
        "competition": str(competition) if competition else None,
        "country": str(country) if country else None,
        "home_team": str(home_name),
        "away_team": str(away_name),
        "kickoff_at": parse_kickoff(kickoff),
        "status": str(status) if status is not None else None,
        "home_score": safe_int(home_score),
        "away_score": safe_int(away_score),
    }


def select_upcoming_fixtures(fixtures, limit=None):
    """
    Keep only fixtures with a valid kickoff time in the future.
    GOAL API timestamps are normalised to naive UTC by parse_kickoff().
    Sort nearest kickoff first.
    """

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    selected = []

    for raw_fixture in fixtures:
        normalised = normalise_fixture(raw_fixture)

        if not normalised:
            continue

        kickoff = normalised["kickoff_at"]

        if kickoff is None or kickoff < now_utc:
            continue

        selected.append((kickoff, raw_fixture))

    selected.sort(key=lambda item: item[0])

    selected = [raw for _, raw in selected]

    if limit is not None:
        selected = selected[:limit]

    return selected


# ==========================================================
# DATABASE FIXTURE STORAGE
# ==========================================================

def save_fixtures_to_database(fixtures):
    """
    Saves fixtures using the unique provider + fixture ID.
    Existing records are updated instead of duplicated.
    """

    connection = get_connection()

    received = len(fixtures)
    saved = 0
    skipped = 0
    started_at = datetime.utcnow()

    try:
        with connection.cursor() as cursor:

            cursor.execute(
                """
                INSERT INTO sync_runs
                (
                    provider,
                    sync_type,
                    records_received,
                    records_saved,
                    status,
                    started_at
                )
                VALUES (%s, %s, %s, 0, %s, %s)
                """,
                (
                    PROVIDER_NAME,
                    "fixtures",
                    received,
                    "running",
                    started_at,
                ),
            )

            sync_id = cursor.lastrowid

            for raw_fixture in fixtures:
                fixture = normalise_fixture(raw_fixture)

                if not fixture:
                    skipped += 1
                    continue

                cursor.execute(
                    """
                    INSERT INTO matches
                    (
                        provider,
                        provider_match_id,
                        competition,
                        country,
                        home_team,
                        away_team,
                        kickoff_at,
                        status,
                        home_score,
                        away_score,
                        last_synced_at
                    )
                    VALUES
                    (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, CURRENT_TIMESTAMP
                    )
                    ON DUPLICATE KEY UPDATE
                        competition = VALUES(competition),
                        country = VALUES(country),
                        home_team = VALUES(home_team),
                        away_team = VALUES(away_team),
                        kickoff_at = VALUES(kickoff_at),
                        status = VALUES(status),
                        home_score = VALUES(home_score),
                        away_score = VALUES(away_score),
                        last_synced_at = CURRENT_TIMESTAMP
                    """,
                    (
                        PROVIDER_NAME,
                        fixture["provider_match_id"],
                        fixture["competition"],
                        fixture["country"],
                        fixture["home_team"],
                        fixture["away_team"],
                        fixture["kickoff_at"],
                        fixture["status"],
                        fixture["home_score"],
                        fixture["away_score"],
                    ),
                )

                saved += 1

            cursor.execute(
                """
                UPDATE sync_runs
                SET
                    records_saved = %s,
                    status = %s,
                    completed_at = %s
                WHERE id = %s
                """,
                (
                    saved,
                    "success",
                    datetime.utcnow(),
                    sync_id,
                ),
            )

        return {
            "received": received,
            "saved": saved,
            "skipped": skipped,
        }

    except Exception as error:
        logger.exception("Fixture database sync failed.")

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO sync_runs
                    (
                        provider,
                        sync_type,
                        records_received,
                        records_saved,
                        status,
                        error_message,
                        started_at,
                        completed_at
                    )
                    VALUES (%s, %s, %s, 0, %s, %s, %s, %s)
                    """,
                    (
                        PROVIDER_NAME,
                        "fixtures",
                        received,
                        "failed",
                        str(error)[:5000],
                        started_at,
                        datetime.utcnow(),
                    ),
                )

        except Exception:
            logger.exception("Could not record failed sync.")

        raise

    finally:
        connection.close()


# ==========================================================
# TELEGRAM COMMANDS
# ==========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "⚽ PUNTER KENYA - ODDS INTELLIGENCE AI\n\n"
        "Welcome to your football research system.\n\n"
        "COMMANDS\n\n"
        "/fixtures - Upcoming football fixtures\n"
        "/live - Live football matches\n"
        "/syncfixtures - Save fixtures into the database\n"
        "/dbtest - Test database connection\n"
        "/diagnose - Test GOAL API connection\n"
        "/odds - Check odds API configuration\n"
        "/compare - Bookmaker comparison status\n"
        "/bookmakers - Supported bookmaker list\n"
        "/calculator - Odds calculator\n"
        "/help - Show commands\n\n"
        "PUNTER KENYA is being built to research "
        "football data, bookmaker odds and market movements."
    )

    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ==========================================================
# FIXTURES COMMAND
# ==========================================================

async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Fetching and checking upcoming fixtures from GOAL API..."
    )

    try:
        result = await asyncio.to_thread(
            goal_request,
            "fixtures",
            {"limit": 100, "offset": 0},
        )

        returned_matches = extract_fixtures(result)

        upcoming_matches = select_upcoming_fixtures(
            returned_matches
        )

        if not upcoming_matches:
            await update.message.reply_text(
                "No valid future fixtures were found in the GOAL API response.\n\n"
                f"Records received: {len(returned_matches)}\n"
                "No fixtures were saved. The provider response may need investigation."
            )
            return

        output = [
            "⚽ UPCOMING FIXTURES",
            "",
        ]

        for match in upcoming_matches[:15]:
            normalised = normalise_fixture(match)

            kickoff = normalised["kickoff_at"]

            kickoff_text = (
                kickoff.replace(tzinfo=timezone.utc)
                .astimezone(EAT)
                .strftime("%d %b %Y - %H:%M EAT")
            )

            competition = (
                normalised["competition"] or "Competition unavailable"
            )

            output.append(
                f"🏆 {competition}\n"
                f"🏠 {normalised['home_team']}\n"
                f"🆚 {normalised['away_team']}\n"
                f"🕒 {kickoff_text}\n"
            )

        await update.message.reply_text(
            "\n".join(output)[:4000]
        )

        try:
            stats = await asyncio.to_thread(
                save_fixtures_to_database,
                upcoming_matches,
            )

            await update.message.reply_text(
                "✅ DATABASE FIXTURE SYNC\n\n"
                f"API records received: {len(returned_matches)}\n"
                f"Future fixtures selected: {len(upcoming_matches)}\n"
                f"Fixtures saved/updated: {stats['saved']}\n"
                f"Fixtures skipped: {stats['skipped']}\n\n"
                "Only valid future fixtures from this API response were saved."
            )

        except Exception as db_error:
            logger.exception("Automatic fixture storage failed.")

            await update.message.reply_text(
                "⚠️ Fixtures were retrieved, but database storage failed.\n\n"
                f"Error: {str(db_error)[:700]}"
            )

    except requests.RequestException as error:
        logger.exception("GOAL API fixture request failed.")

        await update.message.reply_text(
            "❌ GOAL API request failed.\n\n"
            f"{str(error)[:800]}"
        )

    except Exception as error:
        logger.exception("Fixture command failed.")

        await update.message.reply_text(
            "❌ Fixture error.\n\n"
            f"{str(error)[:800]}"
        )


# ==========================================================
# MANUAL FIXTURE SYNC COMMAND
# ==========================================================

async def syncfixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🔄 Fetching, filtering and saving upcoming fixtures into TiDB..."
    )

    try:
        result = await asyncio.to_thread(
            goal_request,
            "fixtures",
            {"limit": 100, "offset": 0},
        )

        returned_matches = extract_fixtures(result)

        upcoming_matches = select_upcoming_fixtures(
            returned_matches
        )

        if not upcoming_matches:
            await update.message.reply_text(
                "No valid future fixtures were found.\n"
                f"API records received: {len(returned_matches)}\n"
                "Nothing was saved."
            )
            return

        stats = await asyncio.to_thread(
            save_fixtures_to_database,
            upcoming_matches,
        )

        await update.message.reply_text(
            "✅ FIXTURE SYNC COMPLETED\n\n"
            f"API records received: {len(returned_matches)}\n"
            f"Future fixtures selected: {len(upcoming_matches)}\n"
            f"Saved/updated: {stats['saved']}\n"
            f"Skipped: {stats['skipped']}\n\n"
            "Fixtures are sorted by nearest kickoff before selection."
        )

    except Exception as error:
        logger.exception("Manual fixture sync failed.")

        await update.message.reply_text(
            "❌ FIXTURE SYNC FAILED\n\n"
            f"{str(error)[:900]}"
        )


# ==========================================================
# LIVE MATCHES
# ==========================================================

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔴 Checking live football matches..."
    )

    try:
        result = await asyncio.to_thread(
            goal_request,
            "fixtures/live",
        )

        matches = extract_fixtures(result)

        if not matches:
            await update.message.reply_text(
                "No live matches currently returned by GOAL API."
            )
            return

        output = [
            "🔴 LIVE FOOTBALL MATCHES",
            "",
        ]

        for match in matches[:20]:
            normalised = normalise_fixture(match)

            if not normalised:
                continue

            home_score = normalised["home_score"]
            away_score = normalised["away_score"]

            score = (
                f"{home_score if home_score is not None else '-'}"
                f" - "
                f"{away_score if away_score is not None else '-'}"
            )

            status = normalised["status"] or "LIVE"

            output.append(
                f"⚽ {normalised['home_team']} "
                f"{score} {normalised['away_team']}\n"
                f"⏱ {status}\n"
            )

        await update.message.reply_text(
            "\n".join(output)[:4000]
        )

    except Exception as error:
        logger.exception("Live matches request failed.")

        await update.message.reply_text(
            "❌ Could not retrieve live matches.\n\n"
            f"{str(error)[:800]}"
        )


# ==========================================================
# DATABASE TEST
# ==========================================================

async def dbtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔄 Testing TiDB database connection..."
    )

    try:
        result = await asyncio.to_thread(test_connection)

        await update.message.reply_text(
            "✅ DATABASE CONNECTION SUCCESSFUL\n\n"
            f"Database: {result['database_name']}\n"
            f"Server version: {result['database_version']}"
        )

    except Exception as error:
        logger.exception("Database test failed.")

        await update.message.reply_text(
            "❌ DATABASE CONNECTION FAILED\n\n"
            f"{str(error)[:900]}"
        )


# ==========================================================
# API DIAGNOSTICS
# ==========================================================

async def diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 GOAL API DIAGNOSTICS\n\n"
        "Checking API authentication and fixtures."
    )

    try:
        result = await asyncio.to_thread(
            goal_request,
            "fixtures",
            {"limit": 5, "offset": 0},
        )

        matches = extract_fixtures(result)

        await update.message.reply_text(
            "✅ API AUTHENTICATION SUCCESSFUL\n\n"
            "GOAL API accepted the request.\n"
            "Endpoint: /fixtures\n"
            f"Fixtures returned: {len(matches)}\n"
            "Success: True"
        )

    except Exception as error:
        logger.exception("GOAL API diagnostic failed.")

        await update.message.reply_text(
            "❌ GOAL API DIAGNOSTIC FAILED\n\n"
            f"{str(error)[:900]}"
        )

    try:
        live_result = await asyncio.to_thread(
            goal_request,
            "fixtures/live",
        )

        live_matches = extract_fixtures(live_result)

        await update.message.reply_text(
            "🔴 LIVE ENDPOINT TEST\n\n"
            "Request successful.\n"
            f"Live matches returned: {len(live_matches)}"
        )

    except Exception as error:
        logger.exception("Live endpoint diagnostic failed.")

        await update.message.reply_text(
            "⚠️ LIVE ENDPOINT TEST FAILED\n\n"
            f"{str(error)[:700]}"
        )


# ==========================================================
# ODDS CONFIGURATION
# ==========================================================

async def odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ODDS_API_KEY:
        message = (
            "✅ ODDS API KEY CONFIGURED\n\n"
            "An odds API key is present in Railway.\n"
            "Actual bookmaker odds collection is not yet connected."
        )

    else:
        message = (
            "⚠️ ODDS API KEY NOT CONFIGURED\n\n"
            "No ODDS_API_KEY was found in Railway.\n"
            "The bookmaker odds collection system is still under development."
        )

    await update.message.reply_text(message)


# ==========================================================
# BOOKMAKERS
# ==========================================================

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

    message = "🏦 PUNTER KENYA BOOKMAKERS\n\n"

    for name in names:
        message += f"• {name}\n"

    message += (
        "\nThese bookmakers are registered in the system's "
        "initial list. Live odds feeds have not yet been verified "
        "or connected."
    )

    await update.message.reply_text(message)


# ==========================================================
# BOOKMAKER COMPARISON STATUS
# ==========================================================

async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 ODDS COMPARISON SYSTEM\n\n"
        "Status: Under development.\n\n"
        "The comparison engine will need verified bookmaker "
        "odds feeds before it can compare prices, identify "
        "price differences or calculate value.\n\n"
        "No live bookmaker comparison is being claimed yet."
    )


# ==========================================================
# ODDS CALCULATOR
# ==========================================================

async def calculator(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.args:
        await update.message.reply_text(
            "🧮 ODDS CALCULATOR\n\n"
            "Usage:\n"
            "/calculator 2.50 100\n\n"
            "Example:\n"
            "/calculator 2.50 100"
        )
        return

    try:
        decimal_odds = float(context.args[0])

        stake = (
            float(context.args[1])
            if len(context.args) > 1
            else 100
        )

        if decimal_odds <= 1 or stake <= 0:
            raise ValueError(
                "Odds must exceed 1 and stake must be positive."
            )

        potential_return = decimal_odds * stake
        potential_profit = potential_return - stake
        implied_probability = (1 / decimal_odds) * 100

        await update.message.reply_text(
            "🧮 ODDS CALCULATION\n\n"
            f"Decimal odds: {decimal_odds:.2f}\n"
            f"Stake: KSh {stake:,.2f}\n"
            f"Potential return: KSh {potential_return:,.2f}\n"
            f"Potential profit: KSh {potential_profit:,.2f}\n"
            f"Implied probability: {implied_probability:.2f}%"
        )

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid input.\n\n"
            "Example: /calculator 2.50 100"
        )


# ==========================================================
# ERROR HANDLER
# ==========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram update caused an error:",
        exc_info=context.error,
    )


# ==========================================================
# START BOT
# ==========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing in Railway variables."
        )

    if not GOAL_API_KEY:
        logger.warning(
            "GOAL_API_KEY is missing. GOAL API commands will not work."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(CommandHandler("fixtures", fixtures))
    application.add_handler(CommandHandler("syncfixtures", syncfixtures))
    application.add_handler(CommandHandler("live", live))

    application.add_handler(CommandHandler("dbtest", dbtest))
    application.add_handler(CommandHandler("diagnose", diagnose))

    application.add_handler(CommandHandler("odds", odds))
    application.add_handler(CommandHandler("bookmakers", bookmakers))
    application.add_handler(CommandHandler("compare", compare))
    application.add_handler(CommandHandler("calculator", calculator))

    application.add_error_handler(error_handler)

    logger.info("PUNTER KENYA is starting...")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
