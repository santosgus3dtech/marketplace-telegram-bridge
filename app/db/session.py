"""Async SQLAlchemy engine and SQLite lifecycle helpers."""

import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


class Database:
    """Own the process-wide async engine and session factory."""

    def __init__(self, database_url: str, sqlite_busy_timeout_ms: int = 5000) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        if self.engine.url.get_backend_name() == "sqlite":

            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute(f"PRAGMA busy_timeout={sqlite_busy_timeout_ms:d}")
                finally:
                    cursor.close()

    async def initialize(self) -> None:
        """Open one connection so SQLite applies its connection pragmas."""

        async with self.engine.begin() as connection:
            await connection.execute(text("SELECT 1"))

    def secure_sqlite_permissions(self, *, include_directory: bool = False) -> None:
        """Restrict a local SQLite database and its sidecar files to the service user."""

        if self.engine.url.get_backend_name() != "sqlite":
            return
        database_name = self.engine.url.database
        if not database_name or database_name == ":memory:":
            return
        database_path = Path(database_name).resolve()
        if include_directory and database_path.parent.exists():
            os.chmod(database_path.parent, 0o700)
        for candidate in (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        ):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    async def is_ready(self) -> bool:
        """Return false on dependency failure without exposing exception details."""

        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            logger.exception(
                "database_readiness_failed",
                extra={"event": "database_readiness", "status": "failed"},
            )
            return False
        return True

    async def journal_mode(self) -> str:
        """Return SQLite's active journal mode for diagnostics and tests."""

        async with self.engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA journal_mode")
            return str(result.scalar_one()).lower()

    async def sessions(self) -> AsyncIterator[AsyncSession]:
        """Yield a transaction-ready async session."""

        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        """Release all pooled connections during application shutdown."""

        await self.engine.dispose()
