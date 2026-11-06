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
# ODDS INTELLIGENCE AI
# Version 1.1 - Diagnostic foundation
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"
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


def football_request(endpoint, params=None):

    if not API_FOOTBALL_KEY:
        raise APIRequestError(
            "API_FOOTBALL_KEY is missing from Railway Variables."
        )

    headers = {
        "x-apisports-key": API_FOOTBALL_KEY
    }

    try:
        response = requests.get(
            f"{API_FOOTBALL_URL}/{endpoint}",
            headers=headers,
            params=params,
            timeout=25,
        )

    except requests.exceptions.Timeout:
        raise APIRequestError(
            "The football API timed out. Please try again."
        )

    except requests.exceptions.ConnectionError:
        raise APIRequestError(
            "Could not connect to the football API."
        )

    except requests.exceptions.RequestException as error:
        raise APIRequestError(
            f"Network error: {str(error)[:500]}"
        )

    try:
        data = response.json()

    except ValueError:
        raise APIRequestError(
            f"API returned a non-JSON response. "
            f"HTTP status: {response.status_code}. "
            f"Response: {response.text[:700]}"
        )

    if not response.ok:
        raise APIRequestError(
            f"HTTP {response.status_code}: "
            f"{str(data)[:900]}"
        )

    api_errors = data.get("errors")

    if api_errors:
        raise APIRequestError(
            f"API-Football reported: {str(api_errors)[:900]}"
        )

    return data


# ==========================================
# FORMATTING FUNCTIONS
# ==========================================

def kenya_time(date_string):

    try:
        match_time = datetime.fromisoformat(
            date_string.replace("Z", "+00:00")
        )

        return match_time.astimezone(KENYA_TZ).strftime(
            "%d %b %Y, %H:%M EAT"
        )

    except (ValueError, TypeError):
        return "Time unavailable"


async def send_long_message(update, message):

    # Telegram messages have a maximum length.
    # Split long reports into safe chunks.

    limit = 3900

    for start in range(0, len(message), limit):
        await update.message.reply_text(
            message[start:start + limit]
        )


def empty_results_message(data):

    return (
        "No matches were returned by the API.\n\n"
        f"Results: {data.get('results', 'Not supplied')}\n"
        f"Paging: {data.get('paging', 'Not supplied')}\n"
        f"API errors: {data.get('errors', 'None reported')}\n\n"
        "This does not necessarily mean there are no matches. "
        "We need to inspect the API response."
    )


# ==========================================
# START AND HELP
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "⚽ ODDS INTELLIGENCE AI\n\n"
        "Welcome to your football research system.\n\n"
        "COMMANDS\n\n"
        "/fixtures - Upcoming football fixtures\n"
        "/live - Live football matches\n"
        "/diagnose - Test the football API\n"
        "/odds - Odds API configuration status\n"
        "/compare - Bookmaker comparison status\n"
        "/bookmakers - Bookmaker coverage\n"
        "/calculator - Calculate combined odds\n"
        "/help - Show this menu\n\n"
        "Our priority is verified data, accurate "
        "calculations and transparent analysis."
    )

    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await start(update, context)


# ==========================================
# UPCOMING FIXTURES
# ==========================================

async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🔎 Connecting to API-Football...\n"
        "Retrieving upcoming fixtures."
    )

    try:
        data = await asyncio.to_thread(
            football_request,
            "fixtures",
            {
                "next": 10,
                "timezone": "Africa/Nairobi",
            },
        )

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text(
                empty_results_message(data)
            )
            return

        lines = ["⚽ UPCOMING FIXTURES\n"]

        for match in matches:

            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            league = match["league"]["name"]
            country = match["league"].get("country", "")

            kickoff = kenya_time(
                match["fixture"]["date"]
            )

            lines.append(
                f"🏆 {league} ({country})\n"
                f"⚽ {home} vs {away}\n"
                f"🕒 {kickoff}\n"
            )

        await send_long_message(
            update,
            "\n".join(lines)
        )

    except APIRequestError as error:

        logger.error("Fixtures API error: %s", error)

        await update.message.reply_text(
            f"⚠️ FIXTURES API ERROR\n\n{str(error)[:3000]}"
        )

    except Exception as error:

        logger.exception("Unexpected fixtures error")

        await update.message.reply_text(
            f"⚠️ UNEXPECTED FIXTURES ERROR\n\n"
            f"{type(error).__name__}: {str(error)[:1000]}"
        )


# ==========================================
# LIVE MATCHES
# ==========================================

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🔴 Connecting to API-Football...\n"
        "Checking live matches."
    )

    try:
        data = await asyncio.to_thread(
            football_request,
            "fixtures",
            {
                "live": "all",
                "timezone": "Africa/Nairobi",
            },
        )

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text(
                empty_results_message(data)
            )
            return

        lines = ["🔴 LIVE FOOTBALL MATCHES\n"]

        for match in matches[:25]:

            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            home_goals = match["goals"]["home"]
            away_goals = match["goals"]["away"]

            status = match["fixture"]["status"]

            elapsed = status.get("elapsed")
            short_status = status.get("short", "LIVE")

            if elapsed is not None:
                match_status = f"{elapsed}'"
            else:
                match_status = short_status

            league = match["league"]["name"]

            lines.append(
                f"🏆 {league}\n"
                f"⚽ {home} {home_goals} - "
                f"{away_goals} {away}\n"
                f"⏱ {match_status}\n"
            )

        await send_long_message(
            update,
            "\n".join(lines)
        )

    except APIRequestError as error:

        logger.error("Live API error: %s", error)

        await update.message.reply_text(
            f"⚠️ LIVE API ERROR\n\n{str(error)[:3000]}"
        )

    except Exception as error:

        logger.exception("Unexpected live error")

        await update.message.reply_text(
            f"⚠️ UNEXPECTED LIVE ERROR\n\n"
            f"{type(error).__name__}: {str(error)[:1000]}"
        )


# ==========================================
# API DIAGNOSTICS
# ==========================================

async def diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🛠 RUNNING API DIAGNOSTICS...\n\n"
        "Checking your API-Football connection."
    )

    if not API_FOOTBALL_KEY:

        await update.message.reply_text(
            "❌ API_FOOTBALL_KEY is missing in Railway."
        )
        return

    try:

        data = await asyncio.to_thread(
            football_request,
            "status",
        )

        results = data.get("results", "Not supplied")

        await update.message.reply_text(
            "✅ API CONNECTION SUCCESSFUL\n\n"
            "API-Football accepted the request.\n"
            f"Status results: {results}\n\n"
            "Now testing upcoming fixtures..."
        )

        fixture_data = await asyncio.to_thread(
            football_request,
            "fixtures",
            {
                "next": 5,
                "timezone": "Africa/Nairobi",
            },
        )

        matches = fixture_data.get("response", [])

        if matches:

            await update.message.reply_text(
                f"✅ FIXTURE TEST SUCCESSFUL\n\n"
                f"Matches returned: {len(matches)}\n"
                "Your football API can retrieve fixtures."
            )

        else:

            await update.message.reply_text(
                "⚠️ API CONNECTION WORKED, "
                "BUT NO FIXTURES WERE RETURNED.\n\n"
                + empty_results_message(fixture_data)
            )

    except APIRequestError as error:

        logger.error("Diagnostic API error: %s", error)

        await update.message.reply_text(
            f"❌ API DIAGNOSTIC FAILED\n\n{str(error)[:3000]}"
        )

    except Exception as error:

        logger.exception("Unexpected diagnostic error")

        await update.message.reply_text(
            f"❌ DIAGNOSTIC ERROR\n\n"
            f"{type(error).__name__}: {str(error)[:1000]}"
        )


# ==========================================
# ODDS API STATUS
# ==========================================

async def odds(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if ODDS_API_KEY:

        message = (
            "📊 ODDS API STATUS\n\n"
            "✅ ODDS_API_KEY is configured in Railway.\n\n"
            "Actual bookmaker coverage has not yet "
            "been verified.\n\n"
            "We will only display prices returned by "
            "a genuine, permitted data source."
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
        "across verified bookmakers.\n\n"
        "We will not invent or estimate bookmaker prices."
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

        values = [
            float(value) for value in context.args
        ]

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

        implied_probability = 100 / combined

        await update.message.reply_text(
            "🧮 CALCULATION RESULT\n\n"
            f"Selections: {len(values)}\n"
            f"Combined odds: {combined:.2f}\n"
            f"Combined implied probability: "
            f"{implied_probability:.2f}%\n\n"
            "Example: KSh 100 stake would return "
            f"KSh {100 * combined:.2f} before any deductions.\n\n"
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

    logger.info("ODDS INTELLIGENCE AI starting...")

    app.run_polling()


if __name__ == "__main__":
    main()
