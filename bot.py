import os
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

# -----------------------------
# CONFIGURATION
# -----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"

KENYA_TZ = ZoneInfo("Africa/Nairobi")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# -----------------------------
# FOOTBALL DATA CONNECTION
# -----------------------------

def football_request(endpoint, params=None):
    if not API_FOOTBALL_KEY:
        raise ValueError("API_FOOTBALL_KEY is missing in Railway.")

    headers = {
        "x-apisports-key": API_FOOTBALL_KEY
    }

    response = requests.get(
        f"{API_FOOTBALL_URL}/{endpoint}",
        headers=headers,
        params=params,
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    if data.get("errors"):
        raise ValueError(str(data["errors"]))

    return data


def format_kickoff(date_string):
    try:
        kickoff = datetime.fromisoformat(
            date_string.replace("Z", "+00:00")
        )
        return kickoff.astimezone(KENYA_TZ).strftime(
            "%d %b, %H:%M EAT"
        )
    except (ValueError, TypeError):
        return "Time unavailable"


# -----------------------------
# TELEGRAM COMMANDS
# -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "⚽ ODDS INTELLIGENCE AI\n\n"
        "Your football research and odds intelligence system.\n\n"
        "AVAILABLE COMMANDS\n"
        "/fixtures - Upcoming football matches\n"
        "/live - Live football scores\n"
        "/odds - Odds data status\n"
        "/compare - Bookmaker comparison status\n"
        "/bookmakers - Supported bookmaker coverage\n"
        "/calculator - Calculate combined odds\n"
        "/help - Show commands\n\n"
        "Our goal is to build a reliable research system "
        "using verified football data."
    )

    await update.message.reply_text(message)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await start(update, context)


async def fixtures(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text("🔎 Fetching upcoming fixtures...")

    try:
        data = await asyncio.to_thread(
            football_request,
            "fixtures",
            {"next": 10},
        )

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text(
                "No upcoming fixtures were returned."
            )
            return

        lines = ["⚽ UPCOMING FIXTURES\n"]

        for match in matches:
            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]
            league = match["league"]["name"]
            kickoff = format_kickoff(match["fixture"]["date"])

            lines.append(
                f"🏆 {league}\n"
                f"{home} vs {away}\n"
                f"🕒 {kickoff}\n"
            )

        await update.message.reply_text(
            "\n".join(lines)[:4000]
        )

    except Exception:
        logger.exception("Fixtures request failed")
        await update.message.reply_text(
            "Could not retrieve fixtures. Check the API connection "
            "and try again."
        )


async def live(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text("🔴 Checking live matches...")

    try:
        data = await asyncio.to_thread(
            football_request,
            "fixtures",
            {"live": "all"},
        )

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text(
                "There are no live matches available right now."
            )
            return

        lines = ["🔴 LIVE MATCHES\n"]

        for match in matches[:15]:
            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            home_goals = match["goals"]["home"]
            away_goals = match["goals"]["away"]

            elapsed = match["fixture"]["status"].get("elapsed")
            elapsed_text = (
                f"{elapsed}'" if elapsed is not None else "Live"
            )

            lines.append(
                f"{home} {home_goals} - "
                f"{away_goals} {away}\n"
                f"⏱ {elapsed_text}\n"
            )

        await update.message.reply_text(
            "\n".join(lines)[:4000]
        )

    except Exception:
        logger.exception("Live scores request failed")
        await update.message.reply_text(
            "Could not retrieve live scores. Please try again."
        )


async def odds(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not ODDS_API_KEY:
        await update.message.reply_text(
            "The Odds API key is not configured."
        )
        return

    await update.message.reply_text(
        "📊 ODDS DATA STATUS\n\n"
        "Your Odds API key is configured.\n\n"
        "We are still verifying which bookmakers and markets "
        "are genuinely available for Kenya.\n\n"
        "No unverified prices will be presented as Kenyan "
        "bookmaker odds."
    )


async def compare(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "📊 BOOKMAKER COMPARISON\n\n"
        "The comparison engine is being prepared.\n\n"
        "It will compare the same match and market across "
        "verified bookmakers once their permitted odds feeds "
        "are connected."
    )


async def bookmakers(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = (
        "🇰🇪 KENYAN BOOKMAKER COVERAGE\n\n"
        "Bookmakers to investigate:\n"
        "1. BetPawa\n"
        "2. SportyBet\n"
        "3. SportPesa\n"
        "4. Odibets\n"
        "5. Mozzart Bet\n"
        "6. 1xBet\n\n"
        "STATUS: Coverage not yet verified.\n\n"
        "We will only activate a bookmaker after confirming "
        "a free, legally accessible and reliable data source."
    )

    await update.message.reply_text(message)


async def calculator(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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

        if any(value <= 1 for value in values):
            raise ValueError("Odds must be greater than 1.")

        combined = 1.0

        for value in values:
            combined *= value

        implied_probability = 100 / combined

        await update.message.reply_text(
            "🧮 CALCULATION RESULT\n\n"
            f"Number of selections: {len(values)}\n"
            f"Combined odds: {combined:.2f}\n"
            f"Combined implied probability: "
            f"{implied_probability:.2f}%\n\n"
            "This is a mathematical calculation, "
            "not a prediction of the outcome."
        )

    except ValueError:
        await update.message.reply_text(
            "Invalid odds. Use decimal numbers greater than 1.\n\n"
            "Example: /calculator 1.50 2.00"
        )


# -----------------------------
# ERROR HANDLER
# -----------------------------

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram error: %s",
        context.error,
    )


# -----------------------------
# START BOT
# -----------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing in Railway Variables."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("fixtures", fixtures))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("odds", odds))
    app.add_handler(CommandHandler("compare", compare))
    app.add_handler(CommandHandler("bookmakers", bookmakers))
    app.add_handler(CommandHandler("calculator", calculator))

    app.add_error_handler(error_handler)

    logger.info("ODDS INTELLIGENCE AI is starting...")

    app.run_polling()


if __name__ == "__main__":
    main()
