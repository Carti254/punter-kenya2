import os
import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 PunterKenya AI\n\n"
        "/fixtures\n"
        "/live"
    )


def football_request(endpoint, params=None):
    headers = {
        "x-apisports-key": API_FOOTBALL_KEY
    }

    response = requests.get(
        f"{API_FOOTBALL_URL}/{endpoint}",
        headers=headers,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    return response.json()


async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = football_request(
            "fixtures",
            {"next": 10}
        )

        fixtures_data = data.get("response", [])

        if not fixtures_data:
            await update.message.reply_text("No fixtures found.")
            return

        message = "⚽ Upcoming Fixtures\n\n"

        for match in fixtures_data:
            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            message += f"{home} vs {away}\n"

        await update.message.reply_text(message)

    except Exception as e:
        await update.message.reply_text(str(e))


async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = football_request(
            "fixtures",
            {"live": "all"}
        )

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text("No live matches.")
            return

        message = "🔴 Live Matches\n\n"

        for match in matches[:10]:
            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            hg = match["goals"]["home"]
            ag = match["goals"]["away"]

            message += f"{home} {hg} - {ag} {away}\n"

        await update.message.reply_text(message)

    except Exception as e:
        await update.message.reply_text(str(e))


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fixtures", fixtures))
    app.add_handler(CommandHandler("live", live))

    print("Bot started")

    app.run_polling()


if __name__ == "__main__":
    main()
