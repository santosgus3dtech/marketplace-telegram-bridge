"""Operational endpoint tests."""

from httpx import AsyncClient

from app.db.session import Database


async def test_health_reports_process_liveness(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_reports_database_availability(
    client: AsyncClient,
    database: Database,
) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}
    assert await database.journal_mode() == "wal"
