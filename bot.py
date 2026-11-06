from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests

TOKEN = "9031121490:AAGq3wUDUrD9qXc1OspIyNZWMTZ5jfY9bMI"
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """
✅ Punter Kenya Bot Online

Commands:
/start - Start bot
/fixtures - Upcoming EPL fixtures
/tips - Daily betting tips
"""
    await update.message.reply_text(msg)

async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        url = "https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id=4328"

        response = requests.get(url, timeout=10)
        data = response.json()

        events = data.get("events", [])

        if not events:
            await update.message.reply_text("No fixtures found.")
            return

        message = "⚽ Upcoming EPL Matches\n\n"

        for match in events[:10]:
            home = match.get("strHomeTeam", "Unknown")
            away = match.get("strAwayTeam", "Unknown")
            date = match.get("dateEvent", "")

            message += f"{home} vs {away}\n📅 {date}\n\n"

        await update.message.reply_text(message)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def tips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 Today's Sample Tips\n\n"
        "Over 1.5 Goals\n"
        "Both Teams To Score\n"
        "Home Team Double Chance"
    )

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fixtures", fixtures))
    app.add_handler(CommandHandler("tips", tips))

    print("Bot started successfully")

    app.run_polling()

if __name__ == "__main__":
    main()
