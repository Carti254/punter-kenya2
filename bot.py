from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "PUT_YOUR_NEW_BOT_TOKEN_HERE"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Punter Kenya!\n\nCommands:\n/tips\n/help"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available commands:\n/start\n/tips\n/help"
    )

async def tips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Today's sample tip:\nArsenal vs Chelsea\nOver 1.5 Goals"
    )

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("tips", tips))

app.run_polling()
