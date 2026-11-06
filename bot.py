from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
import os

TOKEN = os.getenv("BOT_TOKEN")


def get_fixtures():
    url = "https://www.thesportsdb.com/api/v1/json/123/eventsnextleague.php?id=4328"

    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        events = data.get("events", [])

        if not events:
            return "No fixtures found."

        message = "⚽ Upcoming Premier League Matches\n\n"

        for match in events[:10]:
            home = match.get("strHomeTeam", "Unknown")
            away = match.get("strAwayTeam", "Unknown")
            date = match.get("dateEvent", "")

            message += f"{home} vs {away}\n📅 {date}\n\n"

        return message

    except Exception as e:
        return f"Error fetching fixtures: {e}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot is running!\n\nUse /fixtures"
    )


async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_fixtures())


app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("fixtures", fixtures))

print("Bot started...")

app.run_polling()
