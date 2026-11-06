import os
import requests

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("9031121490:AAGq3wUDUrD9qXc1OspIyNZWMTZ5jfY9bMI")
API_FOOTBALL_KEY = os.getenv("d75bb15f1a4252aa256f633460cb4fe2e78433827f327a654cd43d8bc484ee94")
ODDS_API_KEY = os.getenv("9682f8a9f5db8cff1348a0f1603f8745")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 PunterKenya AI\n\n"
        "Commands:\n"
        "/fixtures\n"
        "/banker\n"
        "/acca\n"
        "/valuebets\n"
        "/analyze Arsenal vs Chelsea"
    )


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

        fixtures = data.get("response", [])

        if not fixtures:
            await
