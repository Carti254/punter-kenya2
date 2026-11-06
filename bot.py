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
    ["ℹ️ Help"]
]

reply_markup = ReplyKeyboardMarkup(
    keyboard,
    resize_keyboard=True
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 Welcome to PunterKenyaBot",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use the buttons below."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🏆 Top Leagues":
        await update.message.reply_text(
            "🏆 Supported Leagues\n\n"
            "Premier League\n"
            "La Liga\n"
            "Serie A\n"
            "Bundesliga\n"
            "Ligue 1"
        )

    elif text == "⚽ Today's Matches":

        url = "https://www.thesportsdb.com/api/v1/json/123/eventsnextleague.php?id=4328"

        try:
            response = requests.get(url, timeout=10)
            data = response.json()

            events = data.get("events", [])

            if not events:
                await update.message.reply_text(
                    "No fixtures found."
                )
                return

            message = "⚽ Upcoming Premier League Matches\n\n"

            for match in events[:10]:
                home = match.get("strHomeTeam", "Unknown")
                away = match.get("strAwayTeam", "Unknown")
                date = match.get("dateEvent", "")

                message += f"{home} vs {away}\n📅 {date}\n\n"

            await update.message.reply_text(message)

        except Exception as e:
            await update.message.reply_text(
                f"Error loading fixtures: {e}"
            )

    else:
        await update.message.reply_text(
            "Use the menu buttons."
        )


app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    )
)

app.run_polling()
