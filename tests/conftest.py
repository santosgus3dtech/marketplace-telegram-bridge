"""Shared test fixtures."""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.db import models as db_models  # noqa: F401
from app.db.base import Base
from app.db.session import Database
from app.main import create_app


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    instance = Database(database_url)
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest_asyncio.fixture
async def schema_database(database: Database) -> AsyncIterator[Database]:
    """Return a temporary database with the current metadata created."""

    await database.initialize()
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database


@pytest_asyncio.fixture
async def client(database: Database) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=str(database.engine.url),
        log_level="WARNING",
        delivery_worker_enabled=False,
    )
    application = create_app(settings=settings, database=database)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client
