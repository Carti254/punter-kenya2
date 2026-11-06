import os
import requests

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("TOKEN")

keyboard = [
    ["⚽ Today's Matches"],
    ["🏆 Top Leagues"],
    ["📊 Analyze Match"],
    ["ℹ️ Help"]
]

reply_markup = ReplyKeyboardMarkup(
    keyboard,
    resize_keyboard=True
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 PunterKenyaBot\n\nSelect an option:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available features:\n\n"
        "⚽ Today's Matches\n"
        "🏆 Top Leagues\n"
        "📊 Analyze Match"
    )


def get_fixtures():
    url = "https://www.thesportsdb.com/api/v1/json/123/eventsnextleague.php?id=4328"

    response = requests.get(url, timeout=10)
    data = response.json()

    events = data.get("events", [])

    if not events:
        return "No fixtures found."

    message = "⚽ Upcoming Premier League Matches\n\n"

    for match in events[:10]:
        home = match
