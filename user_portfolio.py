"""Persistent bankroll, rollover settings and explicit user preferences."""
import json
from database import get_connection
from premium_intelligence import stake_advice, rollover_projection


def save_bankroll(user_id, amount):
    amount = float(amount)
    if amount < 0 or amount != amount or amount == float("inf"):
        raise ValueError("Bankroll must be a finite non-negative amount.")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO punter_user_settings (user_id, bankroll)
                VALUES (%s,%s)
                ON DUPLICATE KEY UPDATE bankroll=VALUES(bankroll), updated_at=CURRENT_TIMESTAMP
            """, (int(user_id), amount))
        return amount
    finally:
        connection.close()


def get_settings(user_id):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM punter_user_settings WHERE user_id=%s", (int(user_id),))
            row = cursor.fetchone()
            return row or {"user_id": int(user_id), "bankroll": None, "preferences_json": "{}",
                           "rollover_days": None, "rollover_target": None}
    finally:
        connection.close()


def save_preferences(user_id, preferences):
    if not isinstance(preferences, dict):
        raise ValueError("Preferences must be an object.")
    # Store only explicitly supplied, non-secret user preferences.
    encoded = json.dumps(preferences, ensure_ascii=False)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO punter_user_settings (user_id, preferences_json)
                VALUES (%s,%s)
                ON DUPLICATE KEY UPDATE preferences_json=VALUES(preferences_json),
                    updated_at=CURRENT_TIMESTAMP
            """, (int(user_id), encoded))
    finally:
        connection.close()


def save_rollover(user_id, days, target):
    target = float(target)
    if not 2.0 <= target <= 5.0:
        raise ValueError("Rollover target odds must be between 2.00 and 5.00.")
    projection = rollover_projection(1, int(days), target)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO punter_user_settings (user_id, rollover_days, rollover_target, rollover_started_on)
                VALUES (%s,%s,%s,CURRENT_DATE)
                ON DUPLICATE KEY UPDATE rollover_days=VALUES(rollover_days),
                    rollover_target=VALUES(rollover_target), rollover_started_on=CURRENT_DATE,
                    updated_at=CURRENT_TIMESTAMP
            """, (int(user_id), projection["days"], projection["daily_target_odds"]))
        return projection
    finally:
        connection.close()


def calculate_user_stake(user_id, probability, odds, fraction=0.25, cap=0.02):
    settings = get_settings(user_id)
    if settings.get("bankroll") is None:
        raise ValueError("Set your bankroll first with /bankroll AMOUNT.")
    return stake_advice(settings["bankroll"], probability, odds, fraction, cap)
