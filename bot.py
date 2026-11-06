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
        "🔥 Welcome to PunterKenyaBot\n\nChoose an option:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available options:\n\n"
        "⚽ Today's Matches\n"
        "🏆 Top Leagues\n"
        "📊 Analyze Match"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "🏆 Top Leagues":

        await update.message.reply_text(
            "🏆 Top Leagues\n\n"
            "🇬🇧 Premier League\n"
            "🇪🇸 La Liga\n"
            "🇮🇹 Serie A\n"
            "🇩🇪 Bundesliga\n"
            "🇫🇷 Ligue 1\n"
            "🏆 Champions League"
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

                message += (
                    f"{home} vs {away}\n"
                    f"📅 {date}\n\n"
                )

            await update.message.reply_text(message)

        except Exception as e:
            await update.message.reply_text(
                f"Error loading fixtures: {e}"
            )

    elif text == "📊 Analyze Match":

        await update.message.reply_text(
            "📊 Match Analysis\n\n"
            "Send a match exactly like this:\n\n"
            "Liverpool vs Arsenal\n"
            "Barcelona vs Real Madrid\n"
            "Chelsea vs Newcastle\n\n"
            "⚠ Real analysis engine coming next."
        )

    elif " vs " in text.lower():

        await update.message.reply_text(
            f"📊 Analysis requested:\n\n{text}\n\n"
            "✅ Match received\n"
            "✅ Teams detected\n\n"
            "🚧 Deep AI analysis will be added next."
        )

    else:

        await update.message.reply_text(
            "Please use the menu buttons."
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
