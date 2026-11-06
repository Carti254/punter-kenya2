from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "YOUR_REAL_BOTFATHER_TOKEN"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot works!")

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))

print("Starting bot...")

app.run_polling(
    drop_pending_updates=True,
    allowed_updates=[]
)
