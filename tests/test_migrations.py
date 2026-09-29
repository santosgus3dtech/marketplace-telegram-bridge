"""Alembic migration verification against a real SQLite file."""

import sqlite3

from alembic.config import Config

from alembic import command
from app.config import get_settings

EXPECTED_TABLES = {
    "audit_events",
    "delivery_jobs",
    "gmail_reply_notifications",
    "oauth_states",
    "olx_chats",
    "olx_credentials",
    "olx_listings",
    "olx_messages",
    "outbound_messages",
    "telegram_updates",
}


def unique_column_sets(connection: sqlite3.Connection, table: str) -> set[frozenset[str]]:
    """Return every unique index as its set of columns."""

    unique_sets: set[frozenset[str]] = set()
    for index_row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        if index_row[2] != 1:
            continue
        index_name = str(index_row[1]).replace('"', '""')
        columns = connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        unique_sets.add(frozenset(str(column[2]) for column in columns))
    return unique_sets


def test_initial_migration_upgrades_and_downgrades(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    try:
        command.upgrade(config, "head")

        with sqlite3.connect(database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert EXPECTED_TABLES <= tables

            all_columns = {
                row[1]
                for table in EXPECTED_TABLES
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            assert "client_secret" not in all_columns

            message_uniques = unique_column_sets(connection, "olx_messages")
            assert frozenset({"message_id"}) in message_uniques
            assert frozenset({"telegram_message_id"}) in message_uniques
            assert frozenset({"update_id"}) in unique_column_sets(connection, "telegram_updates")
            delivery_uniques = unique_column_sets(connection, "delivery_jobs")
            assert frozenset({"kind", "olx_message_id"}) in delivery_uniques
            assert frozenset({"kind", "outbound_message_id"}) in delivery_uniques
            assert frozenset({"uid_validity", "message_uid"}) in unique_column_sets(
                connection, "gmail_reply_notifications"
            )

        command.downgrade(config, "base")
        with sqlite3.connect(database_path) as connection:
            remaining_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert not EXPECTED_TABLES & remaining_tables
    finally:
        get_settings.cache_clear()
