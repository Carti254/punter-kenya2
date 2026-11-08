"""Explicit deployment-shell command for checking and applying DB migrations.

Run inside the deployed service environment:
    python migrate_database.py
The script prints database identity/version before applying migrations. It never
prints credentials. Take a provider-side backup before running against production.
"""
from database import test_connection
from schema_migrations import run_migrations


def main():
    info = test_connection()
    print(
        "Connected database:",
        info.get("database_name"),
        "| MySQL:",
        info.get("database_version"),
    )
    result = run_migrations()
    print("Migration result:", result)


if __name__ == "__main__":
    main()
