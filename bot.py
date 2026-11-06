import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import os

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
        "🔥 Welcome to PunterKenyaBot\n\nChoose an option:",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available options:\n\n⚽ Today's Matches\n🏆 Top Leagues"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🏆 Top Leagues":
        await update.message.reply_text(
            "🏆 Supported leagues:\n\n"
            "Premier League\n"
            "La Liga\n"
            "Serie A\n"
            "Bundesliga\n"
            "Ligue 1"
        )

    elif text == "⚽ Today's Matches":
        await update.message.reply_text(
            "🚧 Live fixtures integration coming next.\n"
            "This button is now connected and ready."
        )

    else:
        await update.message.reply_text(
            "Choose one of the menu buttons."
        )

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()
