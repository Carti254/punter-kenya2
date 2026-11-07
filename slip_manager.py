"""Persistent internal bet-slip storage for PUNTER KENYA.

This stores user-created selection lists; it does not submit bets or mint
bookmaker booking codes.
"""
import json
from database import get_connection


def ensure_schema():
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS punter_slips (
                    slip_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    bookmaker VARCHAR(80) NOT NULL DEFAULT 'Manual',
                    selections_json LONGTEXT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_punter_slips_user_status (user_id, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
    finally:
        connection.close()


def create_slip(user_id, selections, bookmaker="Manual"):
    ensure_schema()
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO punter_slips (user_id, bookmaker, selections_json) VALUES (%s,%s,%s)",
                (int(user_id), str(bookmaker)[:80], json.dumps(selections, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def get_slip(user_id, slip_id):
    ensure_schema()
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT slip_id,user_id,bookmaker,selections_json,status,created_at "
                "FROM punter_slips WHERE user_id=%s AND slip_id=%s",
                (int(user_id), int(slip_id)),
            )
            row = cursor.fetchone()
            if row:
                row["selections"] = json.loads(row.pop("selections_json"))
            return row
    finally:
        connection.close()


def list_slips(user_id, limit=10):
    ensure_schema()
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT slip_id,bookmaker,selections_json,status,created_at "
                "FROM punter_slips WHERE user_id=%s AND status='active' "
                "ORDER BY slip_id DESC LIMIT %s",
                (int(user_id), int(limit)),
            )
            rows = cursor.fetchall()
            for row in rows:
                row["selections"] = json.loads(row.pop("selections_json"))
            return rows
    finally:
        connection.close()


def update_slip(user_id, slip_id, selections):
    ensure_schema()
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE punter_slips SET selections_json=%s WHERE user_id=%s AND slip_id=%s AND status='active'",
                (json.dumps(selections, ensure_ascii=False), int(user_id), int(slip_id)),
            )
            return cursor.rowcount == 1
    finally:
        connection.close()


def set_slip_status(user_id, slip_id, status):
    ensure_schema()
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE punter_slips SET status=%s WHERE user_id=%s AND slip_id=%s AND status='active'",
                (str(status), int(user_id), int(slip_id)),
            )
            return cursor.rowcount == 1
    finally:
        connection.close()
