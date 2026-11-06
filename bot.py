from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "PUT_YOUR_NEW_BOT_TOKEN_HERE"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is online!")

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    print("Bot started successfully")
    app.run_polling()

if __name__ == "__main__":
    main()
