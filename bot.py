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

            message
