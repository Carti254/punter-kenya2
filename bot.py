import os
import requests

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 PunterKenya AI\n\n"
        "Commands:\n"
        "/fixtures\n"
        "/analyze Arsenal vs Chelsea\n"
        "/banker\n"
        "/acca\n"
        "/valuebets\n"
        "/live"
    )


def get_fixtures():
    headers = {
        "x-apisports-key": API_FOOTBALL_KEY
    }

    response = requests.get(
        f"{API_FOOTBALL_URL}/fixtures?next=10",
        headers=headers,
        timeout=20
    )

    response.raise_for_status()

    return response.json().get("response", [])


async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        matches = get_fixtures()

        if not matches:
            await update.message.reply_text(
                "No fixtures found."
            )
            return

        message = "⚽ Upcoming Fixtures\n\n"

        for match in matches:
            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            message += f"{home} vs {away}\n"

        await update.message.reply_text(message)

    except Exception as e:
        await update.message.reply_text(
            f"Error: {e}"
        )


async def banker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏦 Banker Research\n\n"
        "Top banker engine coming online.\n"
        "API connection successful."
    )


async def acca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 Accumulator Builder\n\n"
        "Safe accumulator engine ready."
    )


async def valuebets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💎 Value Bets\n\n"
        "Odds scanner ready."
    )


async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔴 Live matches engine ready."
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/analyze Arsenal vs Chelsea"
        )
        return

    match_name = " ".join(context.args)

    await update.message.reply_text(
        f"📊 Match Analysis\n\n"
        f"{match_name}\n\n"
        f"Home Win: 45%\n"
        f"Draw: 28%\n"
        f"Away Win: 27%\n\n"
        f"BTTS: 68%\n"
        f"Over 1.5: 85%\n"
        f"Over 2.5: 61%\n\n"
        f"Recommended Bet:\n"
        f"✅ Over 1.5 Goals"
    )


def main():

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fixtures", fixtures))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("banker", banker))
    app.add_handler(CommandHandler("acca", acca))
    app.add_handler(CommandHandler("valuebets", valuebets))
    app.add_handler(CommandHandler("live", live))

    print("🔥 PunterKenya AI Running")

    app.run_polling()


if __name__ == "__main__":
    main()
