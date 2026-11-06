
import os
import math
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ==========================================
# PUNTER KENYA - ODDS INTELLIGENCE AI
# Version 2.0 - GOAL API integration
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
GOAL_API_KEY = os.getenv("GOAL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

GOAL_API_URL = "https://api.goal-api.com/v1"
KENYA_TZ = ZoneInfo("Africa/Nairobi")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ==========================================
# API ERROR HANDLING
# ==========================================

class APIRequestError(Exception):
    pass


def goal_request(endpoint, params=None):

    if not GOAL_API_KEY:
        raise APIRequestError(
            "GOAL_API_KEY is missing from Railway Variables."
        )

    headers = {
        "Authorization": f"Bearer {GOAL_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            f"{GOAL_API_URL}/{endpoint.lstrip('/')}",
            headers=headers,
            params=params,
            timeout=25,
        )

    except requests.exceptions.Timeout:
        raise APIRequestError(
            "GOAL API timed out. Please try again."
        )

    except requests.exceptions.ConnectionError:
        raise APIRequestError(
            "Could not connect to GOAL API."
        )

    except requests.exceptions.RequestException as error:
        raise APIRequestError(
            f"Network error: {str(error)[:400]}"
        )

    try:
        result = response.json()

    except ValueError:
        raise APIRequestError(
            f"API returned non-JSON data. "
            f"HTTP status: {response.status_code}"
        )

    if not response.ok:
        raise APIRequestError(
            f"GOAL API HTTP {response.status_code}: "
            f"{str(result)[:700]}"
        )

    if isinstance(result, dict) and result.get("success") is False:
        raise APIRequestError(
            f"GOAL API rejected the request: {str(result)[:700]}"
        )

    return result


def get_api_data(result):

    if not isinstance(result, dict):
        raise APIRequestError("Unexpected API response format.")

    data = result.get("data", [])

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        # Some endpoints may return one object.
        return [data]

    return []


# ==========================================
# DATA FORMATTING
# ==========================================

def kenya_time(date_string):

    if not date_string:
        return "Time unavailable"

    try:
        match_time = datetime.fromisoformat(
            str(date_string).replace("Z", "+00:00")
        )

        if match_time.tzinfo is None:
            match_time = match_time.replace(
                tzinfo=ZoneInfo("UTC")
            )

        return match_time.astimezone(KENYA_TZ).strftime(
            "%d %b %Y, %H:%M EAT"
        )

    except (ValueError, TypeError):
        return "Time unavailable"


def field(match, *names, default="N/A"):

    for name in names:
        value = match.get(name)

        if value is not None and value != "":
            return value

    return default


def match_teams(match):

    home = field(
        match,
        "homeTeamName",
        "home_team_name",
        default=None,
    )

    away = field(
        match,
        "awayTeamName",
        "away_team_name",
        default=None,
    )

    # Also support nested team objects if returned.
    if not home and isinstance(match.get("homeTeam"), dict):
        home = match["homeTeam"].get("name")

    if not away and isinstance(match.get("awayTeam"), dict):
        away = match["awayTeam"].get("name")

    return home or "Home team unavailable", away or "Away team unavailable"


async def send_long_message(update, message):

    limit = 3900

    for start in range(0, len(message), limit):
        await update.message.reply_text(
            message[start:start + limit]
        )


def empty_results_message(result):

    pagination = result.get("pagination", "Not supplied")

    return (
        "No matches were returned.\n\n"
        f"Pagination: {pagination}\n\n"
        "This does not necessarily mean no matches exist. "
        "Check competition coverage and API access."
    )


# ==========================================
# START AND HELP
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "⚽ PUNTER KENYA\n"
        "ODDS INTELLIGENCE AI\n\n"
        "Welcome to your football research system.\n\n"
        "COMMANDS\n\n"
        "/fixtures - Upcoming fixtures\n"
        "/live - Live football matches\n"
        "/diagnose - Test GOAL API\n"
        "/odds - Odds API configuration\n"
        "/compare - Bookmaker comparison\n"
        "/bookmakers - Bookmaker coverage\n"
        "/calculator - Calculate combined odds\n"
        "/help - Show commands\n\n"
        "Our priority is verified data, "
        "accurate calculations and transparent analysis."
    )

    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ==========================================
# UPCOMING FIXTURES
# ==========================================

async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🔎 CONNECTING TO GOAL API...\n\n"
        "Retrieving upcoming fixtures."
    )

    try:
        result = await asyncio.to_thread(
            goal_request,
            "fixtures",
            {"limit": 10, "offset": 0},
        )

        matches = get_api_data(result)

        if not matches:
            await update.message.reply_text(
                empty_results_message(result)
            )
            return

        lines = ["⚽ UPCOMING FOOTBALL FIXTURES\n"]

        for match in matches[:10]:

            home, away = match_teams(match)

            league = field(
                match,
                "leagueName",
                "league_name",
                default="League unavailable",
            )

            country = field(
                match,
                "countryName",
                "country_name",
                default="",
            )

            kickoff = kenya_time(
                field(
                    match,
                    "kickoffUtc",
                    "kickoff_utc",
                    "date",
                    default=None,
                )
            )

            competition = (
                f"{league} ({country})"
                if country else league
            )

            lines.append(
                f"🏆 {competition}\n"
                f"⚽ {home} vs {away}\n"
                f"🕒 {kickoff}\n"
            )

        await send_long_message(update, "\n".join(lines))

    except APIRequestError as error:

        logger.error("GOAL fixtures error: %s", error)

        await update.message.reply_text(
            f"⚠️ FIXTURES API ERROR\n\n{str(error)[:2500]}"
        )

    except Exception as error:

        logger.exception("Unexpected fixtures error")

        await update.message.reply_text(
            f"⚠️ UNEXPECTED ERROR\n\n"
            f"{type(error).__name__}: {str(error)[:800]}"
        )


# ==========================================
# LIVE MATCHES
# ==========================================

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🔴 CONNECTING TO GOAL API...\n\n"
        "Checking live matches."
    )

    try:
        result = await asyncio.to_thread(
            goal_request,
            "fixtures/live",
        )

        matches = get_api_data(result)

        if not matches:
            await update.message.reply_text(
                "🔴 NO LIVE MATCHES RETURNED.\n\n"
                "There may currently be no matches in play, "
                "or your plan may restrict live data."
            )
            return

        lines = ["🔴 LIVE FOOTBALL MATCHES\n"]

        for match in matches[:25]:

            home, away = match_teams(match)

            home_goals = field(
                match,
                "homeTeamScore",
                "home_team_score",
                default="0",
            )

            away_goals = field(
                match,
                "awayTeamScore",
                "away_team_score",
                default="0",
            )

            league = field(
                match,
                "leagueName",
                "league_name",
                default="League unavailable",
            )

            minute = field(
                match,
                "matchMinute",
                "matchElapsed",
                "match_status",
                default="LIVE",
            )

            lines.append(
                f"🏆 {league}\n"
                f"⚽ {home} {home_goals} - "
                f"{away_goals} {away}\n"
                f"⏱ {minute}\n"
            )

        await send_long_message(update, "\n".join(lines))

    except APIRequestError as error:

        logger.error("GOAL live error: %s", error)

        await update.message.reply_text(
            f"⚠️ LIVE API ERROR\n\n{str(error)[:2500]}"
        )

    except Exception as error:

        logger.exception("Unexpected live error")

        await update.message.reply_text(
            f"⚠️ UNEXPECTED ERROR\n\n"
            f"{type(error).__name__}: {str(error)[:800]}"
        )


# ==========================================
# API DIAGNOSTICS
# ==========================================

async def diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🛠 GOAL API DIAGNOSTICS\n\n"
        "Checking API authentication and fixtures."
    )

    if not GOAL_API_KEY:
        await update.message.reply_text(
            "❌ GOAL_API_KEY is missing in Railway Variables."
        )
        return

    try:

        fixture_result = await asyncio.to_thread(
            goal_request,
            "fixtures",
            {"limit": 5, "offset": 0},
        )

        matches = get_api_data(fixture_result)

        await update.message.reply_text(
            "✅ API AUTHENTICATION SUCCESSFUL\n\n"
            "GOAL API accepted the request.\n"
            "Endpoint: /fixtures\n"
            f"Fixtures returned: {len(matches)}\n"
            f"Success: {fixture_result.get('success', 'Not supplied')}\n"
        )

        try:

            live_result = await asyncio.to_thread(
                goal_request,
                "fixtures/live",
            )

            live_matches = get_api_data(live_result)

            await update.message.reply_text(
                "🔴 LIVE ENDPOINT TEST\n\n"
                "Request successful.\n"
                f"Live matches returned: {len(live_matches)}"
            )

        except APIRequestError as live_error:

            await update.message.reply_text(
                "⚠️ FIXTURES WORKED, BUT LIVE TEST FAILED.\n\n"
                f"{str(live_error)[:1500]}"
            )

    except APIRequestError as error:

        logger.error("GOAL diagnostic error: %s", error)

        await update.message.reply_text(
            f"❌ API DIAGNOSTIC FAILED\n\n{str(error)[:2500]}"
        )

    except Exception as error:

        logger.exception("Unexpected diagnostic error")

        await update.message.reply_text(
            f"❌ DIAGNOSTIC ERROR\n\n"
            f"{type(error).__name__}: {str(error)[:800]}"
        )


# ==========================================
# ODDS API STATUS
# ==========================================

async def odds(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if ODDS_API_KEY:

        message = (
            "📊 ODDS API STATUS\n\n"
            "✅ ODDS_API_KEY is configured.\n\n"
            "Actual bookmaker coverage has not yet "
            "been verified.\n\n"
            "Football fixtures and bookmaker odds "
            "are separate data services."
        )

    else:

        message = (
            "❌ ODDS_API_KEY is missing from Railway."
        )

    await update.message.reply_text(message)


# ==========================================
# BOOKMAKER COMPARISON
# ==========================================

async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "📊 BOOKMAKER COMPARISON\n\n"
        "The comparison engine is under development.\n\n"
        "It will compare matching events and markets "
        "using verified bookmaker prices.\n\n"
        "No bookmaker prices will be invented."
    )


async def bookmakers(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🇰🇪 KENYAN BOOKMAKER COVERAGE\n\n"
        "Bookmakers being investigated:\n\n"
        "1. BetPawa\n"
        "2. SportyBet\n"
        "3. SportPesa\n"
        "4. Odibets\n"
        "5. Mozzart Bet\n"
        "6. 1xBet\n\n"
        "Status: Not yet verified.\n\n"
        "A bookmaker will only be marked active "
        "after its data source is tested."
    )


# ==========================================
# ODDS CALCULATOR
# ==========================================

async def calculator(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:

        await update.message.reply_text(
            "🧮 ODDS CALCULATOR\n\n"
            "Enter decimal odds separated by spaces.\n\n"
            "Example:\n"
            "/calculator 1.50 2.00 1.80"
        )
        return

    try:

        values = [float(value) for value in context.args]

        if not values:
            raise ValueError("Enter at least one selection.")

        if any(
            not math.isfinite(value) or value <= 1
            for value in values
        ):
            raise ValueError(
                "Every decimal odd must be greater than 1."
            )

        combined = math.prod(values)

        if not math.isfinite(combined):
            raise ValueError("Combined odds are too large.")

        implied_probability = 100 / combined

        await update.message.reply_text(
            "🧮 CALCULATION RESULT\n\n"
            f"Selections: {len(values)}\n"
            f"Combined odds: {combined:.2f}\n"
            f"Combined implied probability: "
            f"{implied_probability:.2f}%\n\n"
            "KSh 100 stake would return "
            f"KSh {100 * combined:.2f} before deductions.\n\n"
            "This is a mathematical calculation, "
            "not a guarantee of winning."
        )

    except (ValueError, OverflowError) as error:

        await update.message.reply_text(
            f"⚠️ INVALID INPUT\n\n{str(error)}"
        )


# ==========================================
# TELEGRAM ERROR HANDLER
# ==========================================

async def error_handler(update, context):

    logger.error(
        "Telegram application error",
        exc_info=context.error,
    )


# ==========================================
# MAIN APPLICATION
# ==========================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing in Railway Variables."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CommandHandler("fixtures", fixtures))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("diagnose", diagnose))

    app.add_handler(CommandHandler("odds", odds))
    app.add_handler(CommandHandler("compare", compare))
    app.add_handler(CommandHandler("bookmakers", bookmakers))

    app.add_handler(CommandHandler("calculator", calculator))

    app.add_error_handler(error_handler)

    logger.info("PUNTER KENYA - ODDS INTELLIGENCE AI starting...")
    logger.info("Football provider: GOAL API")

    app.run_polling()


if __name__ == "__main__":
    main()
