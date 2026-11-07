import os
import logging
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from database import get_connection, test_connection


# ==========================================================
# PUNTER KENYA - ODDS INTELLIGENCE AI
# Version 3.2 - league catalogue diagnostics
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOAL_API_KEY = os.getenv("GOAL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

GOAL_API_BASE = "https://api.goal-api.com/v1"
PROVIDER_NAME = "goal_api"

EAT = ZoneInfo("Africa/Nairobi")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("punter_kenya")


# ==========================================================
# GENERAL HELPERS
# ==========================================================

def eat_now():
    return datetime.now(EAT)


def safe_int(value):
    if value is None or value == "":
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def first_value(data, *keys):
    if not isinstance(data, dict):
        return None

    for key in keys:
        value = data.get(key)

        if value is not None and value != "":
            return value

    return None


def nested_name(value):
    if isinstance(value, dict):
        return first_value(value, "name", "teamName", "title")

    if isinstance(value, str):
        return value

    return None


def parse_kickoff(value):
    if not value:
        return None

    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )

        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return dt
