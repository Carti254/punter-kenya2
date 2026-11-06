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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🔥 PunterKenya AI

Commands:

/fixtures
/banker
/acca
/valuebets

Example:
/analyze Arsenal vs Chelsea
"""
    await update.message.reply_text(text)


async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        url = "https://v3.football.api-sports.io/fixtures?next=10"

        headers = {
            "x-apisports-key": API_FOOTBALL_KEY
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=20
        )

        data = response.json()

        matches = data.get("response", [])

        if not matches:
            await update.message.reply_text(
                "No fixtures found."
            )
            return

        message = "⚽ Upcoming Fixtures\n\n"

        for match in matches[:10]:
            home = match["teams"]["home"]["name"]
    
