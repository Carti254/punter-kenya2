from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "9031121490:AAGq3wUDUrD9qXc1OspIyNZWMTZ5jfY9bMI"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is online!")

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    print("Bot started successfully")

    app.run_polling()

if __name__ == "__main__":
    main()
