
import os
import certifi
import pymysql

from pymysql.cursors import DictCursor


def get_connection():

    required = [
        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASSWORD",
        "DB_NAME",
    ]

    missing = [
        name for name in required
        if not os.getenv(name)
    ]

    if missing:
        raise RuntimeError(
            "Missing database variables: "
            + ", ".join(missing)
        )

    if os.getenv("DB_SSL", "true").lower() != "true":
        raise RuntimeError(
            "Database TLS must remain enabled."
        )

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=15,
        read_timeout=20,
        write_timeout=20,
        ssl_ca=certifi.where(),
        ssl_verify_cert=True,
        ssl_verify_identity=True,
    )


def test_connection():

    connection = get_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATABASE() AS database_name, "
                "VERSION() AS database_version"
            )

            return cursor.fetchone()

    finally:
        connection.close()
