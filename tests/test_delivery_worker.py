"""Persistent outbox restart, locking, and graceful worker tests."""

import asyncio

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db.models import DeliveryJob, DeliveryJobStatus, OlxMessage, utc_now
from app.db.repositories import (
    DeliveryJobRepository,
    OlxChatRepository,
    OlxMessageRepository,
)
from app.db.session import Database
from app.services.delivery import DeliveryWorker


def worker_settings(database: Database, **overrides: object) -> Settings:
    """Return deterministic worker configuration for isolated tests."""

    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": str(database.engine.url),
        "telegram_bot_token": "worker-test-token",  # noqa: S106
        "telegram_target_chat_id": "-100200300",
        "delivery_worker_enabled": False,
        "delivery_max_attempts": 3,
        "delivery_backoff_seconds": 0,
        "delivery_worker_poll_seconds": 0.01,
        "delivery_lock_timeout_seconds": 1,
    }
    values.update(overrides)
    return Settings(**values)


async def seed_notification_job(database: Database) -> DeliveryJob:
    """Persist one buyer message and its notification job atomically."""

    chats = OlxChatRepository()
    messages = OlxMessageRepository()
    jobs = DeliveryJobRepository()
    async with database.session_factory.begin() as session:
        await chats.upsert(
            session,
            chat_id="restart-chat",
            list_id="restart-listing",
            buyer_name="Cliente",
            last_message_at=utc_now(),
        )
        message, _created = await messages.add_if_new(
            session,
            message_id="restart-message",
            chat_id="restart-chat",
            list_id="restart-listing",
            origin="buyer",
            sender_type="account",
            text="Mensagem que deve sobreviver ao restart",
            olx_timestamp=utc_now(),
        )
        job, _created = await jobs.add_olx_to_telegram_if_new(
            session,
            olx_message_id=message.id,
            max_attempts=3,
        )
        return job


async def test_retry_job_is_resumed_by_a_new_worker_instance(
    schema_database: Database,
) -> None:
    """A retry persisted by one process is delivered after a simulated restart."""

    await seed_notification_job(schema_database)
    first_calls = 0

    def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal first_calls
        first_calls += 1
        raise httpx.ReadTimeout("temporary failure", request=request)

    first_http = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
    settings = worker_settings(schema_database)
    try:
        first_worker = DeliveryWorker(
            settings=settings,
            database=schema_database,
            http_client=first_http,
        )
        assert await first_worker.process_once() is True
    finally:
        await first_http.aclose()

    async with schema_database.session_factory() as session:
        retry_job = await session.scalar(select(DeliveryJob))
    assert retry_job is not None
    assert retry_job.status == DeliveryJobStatus.RETRY
    assert retry_job.attempts == 1

    second_calls = 0

    def available(_request: httpx.Request) -> httpx.Response:
        nonlocal second_calls
        second_calls += 1
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 8801, "chat": {"id": -100200300}}},
        )

    second_http = httpx.AsyncClient(transport=httpx.MockTransport(available))
    try:
        restarted_worker = DeliveryWorker(
            settings=settings,
            database=schema_database,
            http_client=second_http,
        )
        assert await restarted_worker.process_once() is True
    finally:
        await second_http.aclose()

    async with schema_database.session_factory() as session:
        completed_job = await session.scalar(select(DeliveryJob))
        message = await session.scalar(select(OlxMessage))
    assert first_calls == 1
    assert second_calls == 1
    assert completed_job is not None
    assert completed_job.status == DeliveryJobStatus.SUCCEEDED
    assert completed_job.attempts == 2
    assert message is not None
    assert message.telegram_message_id == 8801


async def test_atomic_claim_allows_only_one_worker(schema_database: Database) -> None:
    """Two concurrent workers cannot own the same due outbox item."""

    await seed_notification_job(schema_database)
    repository = DeliveryJobRepository()

    async def claim(token: str) -> DeliveryJob | None:
        async with schema_database.session_factory.begin() as session:
            return await repository.claim_next(
                session,
                lock_token=token,
                lock_timeout_seconds=60,
            )

    claimed = await asyncio.gather(claim("worker-a"), claim("worker-b"))

    assert sum(job is not None for job in claimed) == 1
    winner = next(job for job in claimed if job is not None)
    assert winner.status == DeliveryJobStatus.PROCESSING
    assert winner.lock_token in {"worker-a", "worker-b"}


async def test_worker_start_and_stop_are_graceful(schema_database: Database) -> None:
    """An idle worker wakes immediately and finishes when shutdown is requested."""

    settings = worker_settings(schema_database)
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    try:
        worker = DeliveryWorker(
            settings=settings,
            database=schema_database,
            http_client=upstream,
        )
        await worker.start()
        await asyncio.sleep(0)
        await worker.stop()
    finally:
        await upstream.aclose()

    assert worker._task is None
