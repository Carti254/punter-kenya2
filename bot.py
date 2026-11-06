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
        "/fixtures\n"
        "/analyze Arsenal vs Chelsea\n"
        "/live\n"
        "/banker\n"
        "/valuebets\n"
        "/acca"
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

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text("No fixtures found.")
            return

        text = "⚽ Upcoming Fixtures\n\n"

        for match in matches:
            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            text += f"{home} vs {away}\n"

        await update.message.reply_text(text)

    except Exception as e:
        await update.message.reply_text(
            f"Error: {e}"
        )


async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = football_request(
            "fixtures",
            {"live": "all"}
        )

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text(
                "No live matches."
            )
            return

        text = "🔴 Live Matches\n\n"

        for match in matches[:15]:
            home = match["teams"]["home"]["name"]
            away = match["teams"]["away"]["name"]

            hg = match["goals"]["home"]
            ag = match["goals"]["away"]

            text += (
                f"{home} {hg} - {ag} {away}\n"
            )

        await update.message.reply_text(text)

    except Exception as e:
        await update.message.reply_text(
            f"Error: {e}"
        )


async def banker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏦 Banker Engine\n\n"
        "Next step: connect real odds and probability calculations."
    )


async def valuebets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💎 Value Bets Engine\n\n"
        "Next step: connect The Odds API."
    )


async def acca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 Accumulator Builder\n\n"
        "Next step: build selections from live odds."
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text(
            "Example:\n/analyze Arsenal vs Chelsea"
        )
        return

    match_name = " ".join(context.args)

    await update.message.reply_text(
        f"📊 Analysis\n\n"
        f"{match_name}\n\n"
        f"Feature under development."
    )


def main():
