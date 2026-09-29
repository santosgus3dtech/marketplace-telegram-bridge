"""Inbound OLX webhook security, persistence, and forwarding tests."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import DeliveryJob, DeliveryJobStatus, OlxChat, OlxMessage
from app.db.session import Database
from app.main import create_app
from app.services.delivery import DeliveryWorker

ResponseHandler = Callable[[httpx.Request], httpx.Response]
WEBHOOK_SECRET = "webhook-path-for-test"  # noqa: S105
OLX_SOURCE_IP = "54.162.151.93"


def webhook_settings(database: Database, **overrides: object) -> Settings:
    """Return isolated webhook and Telegram configuration for tests."""

    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": str(database.engine.url),
        "log_level": "INFO",
        "olx_webhook_path_secret": WEBHOOK_SECRET,
        "olx_allowed_source_ip": OLX_SOURCE_IP,
        "telegram_bot_token": "telegram-token-for-test",  # noqa: S106
        "telegram_target_chat_id": "-100200300",
    }
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def webhook_test_client(
    database: Database,
    handler: ResponseHandler,
    **setting_overrides: object,
) -> AsyncIterator[tuple[httpx.AsyncClient, DeliveryWorker]]:
    """Run the application with a fully mocked Telegram transport."""

    settings = webhook_settings(database, **setting_overrides)
    settings.delivery_worker_enabled = False
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application = create_app(
        settings=settings,
        database=database,
        outbound_http_client=upstream,
    )
    try:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as test_client:
                yield (
                    test_client,
                    DeliveryWorker(
                        settings=settings,
                        database=database,
                        http_client=upstream,
                    ),
                )
    finally:
        await upstream.aclose()


def valid_payload(**overrides: object) -> dict[str, object]:
    """Build one payload matching the official OLX Chat schema."""

    payload: dict[str, object] = {
        "chatId": "chat-abcdef123456",
        "message": "Ainda está disponível?",
        "senderType": "account",
        "email": "buyer@example.test",
        "name": "Cliente Teste",
        "phone": "+5500000000000",
        "messageTimestamp": "2026-09-28T15:45:10.123",
        "messageId": "message-001",
        "origin": "buyer",
        "listId": "listing-9001",
    }
    payload.update(overrides)
    return payload


def webhook_headers(ip_address: str = OLX_SOURCE_IP) -> dict[str, str]:
    """Return the Cloudflare-origin header expected by the endpoint."""

    return {"CF-Connecting-IP": ip_address}


async def test_invalid_path_and_ip_are_rejected(schema_database: Database) -> None:
    calls = 0

    def unexpected_telegram(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with webhook_test_client(schema_database, unexpected_telegram) as (client, _worker):
        invalid_path = await client.post(
            "/webhooks/olx/not-the-secret",
            headers=webhook_headers(),
            json=valid_payload(),
        )
        invalid_ip = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            headers=webhook_headers("203.0.113.10"),
            json=valid_payload(),
        )

    assert invalid_path.status_code == 404
    assert invalid_ip.status_code == 403
    assert calls == 0


async def test_source_header_is_optional_when_cloudflare_trust_is_disabled(
    schema_database: Database,
) -> None:
    def unexpected_telegram(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("seller messages must not reach Telegram")

    async with webhook_test_client(
        schema_database,
        unexpected_telegram,
        trust_cloudflare=False,
    ) as (client, _worker):
        response = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            json=valid_payload(origin="seller", messageId="without-cloudflare-header"),
        )

    assert response.status_code == 200
    assert response.json() == {"status": "stored"}


async def test_new_buyer_message_is_persisted_forwarded_and_mapped(
    schema_database: Database,
    capsys,
) -> None:
    telegram_requests: list[httpx.Request] = []

    def telegram_response(request: httpx.Request) -> httpx.Response:
        telegram_requests.append(request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 712, "chat": {"id": -100200300}}},
        )

    payload = valid_payload()
    async with webhook_test_client(schema_database, telegram_response) as (client, worker):
        response = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            headers=webhook_headers(),
            json=payload,
        )
        assert telegram_requests == []
        assert await worker.process_once() is True
        async with schema_database.session_factory() as session:
            chat = await session.scalar(select(OlxChat))
            message = await session.scalar(select(OlxMessage))
            job = await session.scalar(select(DeliveryJob))

    visible_output = capsys.readouterr().out
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert len(telegram_requests) == 1
    telegram_body = json.loads(telegram_requests[0].content)
    assert telegram_body["chat_id"] == "-100200300"
    assert "Cliente Teste" in telegram_body["text"]
    assert "listing-9001" in telegram_body["text"]
    assert "Ainda está disponível?" in telegram_body["text"]
    assert "chat-abc…" in telegram_body["text"]
    assert str(payload["email"]) not in telegram_body["text"]
    assert str(payload["phone"]) not in telegram_body["text"]
    assert str(payload["email"]) not in visible_output
    assert str(payload["phone"]) not in visible_output
    assert "telegram-token-for-test" not in visible_output
    assert chat is not None
    assert chat.buyer_name == payload["name"]
    assert chat.buyer_email == payload["email"]
    assert chat.buyer_phone == payload["phone"]
    assert message is not None
    assert message.telegram_message_id == 712
    assert message.telegram_chat_id == "-100200300"
    assert job is not None
    assert job.status == DeliveryJobStatus.SUCCEEDED


async def test_duplicate_returns_200_without_resending(schema_database: Database) -> None:
    calls = 0

    def telegram_response(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 713, "chat": {"id": -100200300}}},
        )

    async with webhook_test_client(schema_database, telegram_response) as (client, worker):
        first = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            headers=webhook_headers(),
            json=valid_payload(),
        )
        assert calls == 0
        assert await worker.process_once() is True
        duplicate = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            headers=webhook_headers(),
            json=valid_payload(message="Texto alterado não deve substituir o original"),
        )
        async with schema_database.session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(OlxMessage))
            job_count = await session.scalar(select(func.count()).select_from(DeliveryJob))
            stored = await session.scalar(select(OlxMessage))

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}
    assert calls == 1
    assert count == 1
    assert job_count == 1
    assert stored is not None
    assert stored.text == "Ainda está disponível?"


async def test_seller_message_is_persisted_without_notification(
    schema_database: Database,
) -> None:
    def unexpected_telegram(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("seller messages must not generate buyer notifications")

    async with webhook_test_client(schema_database, unexpected_telegram) as (client, _worker):
        response = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            headers=webhook_headers(),
            json=valid_payload(origin="seller", messageId="message-seller-001"),
        )
        async with schema_database.session_factory() as session:
            chat = await session.scalar(select(OlxChat))
            message = await session.scalar(select(OlxMessage))

    assert response.status_code == 200
    assert response.json() == {"status": "stored"}
    assert chat is not None
    assert chat.buyer_email is None
    assert chat.buyer_phone is None
    assert message is not None
    assert message.origin == "seller"
    assert message.telegram_message_id is None


async def test_body_limit_and_strict_schema_are_enforced(schema_database: Database) -> None:
    def unexpected_telegram(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid requests must not reach Telegram")

    async with webhook_test_client(
        schema_database,
        unexpected_telegram,
        olx_webhook_body_max_bytes=1_024,
    ) as (client, _worker):
        too_large = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            headers={**webhook_headers(), "Content-Type": "application/json"},
            content=b"x" * 1_025,
        )
        strict_payload = valid_payload(unexpected="field")
        invalid_schema = await client.post(
            f"/webhooks/olx/{WEBHOOK_SECRET}",
            headers=webhook_headers(),
            json=strict_payload,
        )

    assert too_large.status_code == 413
    assert invalid_schema.status_code == 422
