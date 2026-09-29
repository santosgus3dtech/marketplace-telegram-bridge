"""Telegram Reply mapping and official OLX Chat delivery tests."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import Settings
from app.db.models import (
    ConnectionStatus,
    DeliveryJob,
    DeliveryJobStatus,
    OlxCredential,
    OutboundMessage,
    OutboundMessageStatus,
    TelegramUpdate,
    TelegramUpdateStatus,
    utc_now,
)
from app.db.repositories import OlxChatRepository, OlxMessageRepository
from app.db.session import Database
from app.main import create_app
from app.services.credentials import CredentialService
from app.services.delivery import DeliveryWorker
from app.services.security import TokenCipher

ResponseHandler = Callable[[httpx.Request], httpx.Response]
WEBHOOK_SECRET = "telegram_webhook_secret_for_reply_test"  # noqa: S105
BOT_TOKEN = "telegram-bot-token-for-reply-test"  # noqa: S105
OLX_TOKEN = "olx-access-token-for-reply-test"  # noqa: S105
TARGET_CHAT_ID = "-100200300"
ALLOWED_USER_ID = 900100
TELEGRAM_NOTIFICATION_ID = 712
OLX_MESSAGE_ID = "olx-message-001"
OLX_CHAT_ID = "olx-chat-001"


def reply_settings(database: Database, encryption_key: str, **overrides: object) -> Settings:
    """Return isolated settings for Telegram-to-OLX tests."""

    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": str(database.engine.url),
        "log_level": "INFO",
        "telegram_bot_token": BOT_TOKEN,
        "telegram_target_chat_id": TARGET_CHAT_ID,
        "telegram_allowed_user_ids": str(ALLOWED_USER_ID),
        "telegram_webhook_secret": WEBHOOK_SECRET,
        "token_encryption_key": encryption_key,
        "delivery_worker_enabled": False,
        "delivery_max_attempts": 3,
        "delivery_backoff_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


async def seed_mapping(
    database: Database,
    encryption_key: str,
    *,
    origin: str = "buyer",
    telegram_message_id: int = TELEGRAM_NOTIFICATION_ID,
) -> None:
    """Persist one OLX notification mapping and an encrypted connected token."""

    chats = OlxChatRepository()
    messages = OlxMessageRepository()
    credentials = CredentialService(TokenCipher(encryption_key))
    async with database.session_factory.begin() as session:
        await chats.upsert(
            session,
            chat_id=OLX_CHAT_ID,
            list_id="listing-001",
        )
        await messages.add_if_new(
            session,
            message_id=OLX_MESSAGE_ID,
            chat_id=OLX_CHAT_ID,
            list_id="listing-001",
            origin=origin,
            sender_type="account",
            text="Mensagem original",
            olx_timestamp=utc_now(),
        )
        await messages.set_telegram_mapping(
            session,
            message_id=OLX_MESSAGE_ID,
            telegram_message_id=telegram_message_id,
            telegram_chat_id=TARGET_CHAT_ID,
        )
        await credentials.store_access_token(
            session,
            access_token=OLX_TOKEN,
            connection_status=ConnectionStatus.CONNECTED,
        )


@asynccontextmanager
async def reply_test_client(
    database: Database,
    handler: ResponseHandler,
    *,
    seed: bool = True,
    mapping_origin: str = "buyer",
    **setting_overrides: object,
) -> AsyncIterator[tuple[httpx.AsyncClient, DeliveryWorker]]:
    """Run the application with mocked Telegram and OLX transports."""

    encryption_key = Fernet.generate_key().decode("ascii")
    if seed:
        await seed_mapping(database, encryption_key, origin=mapping_origin)
    settings = reply_settings(database, encryption_key, **setting_overrides)
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


def reply_update(**overrides: object) -> dict[str, object]:
    """Build one authorized Reply update."""

    update: dict[str, object] = {
        "update_id": 8101,
        "message": {
            "message_id": 901,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "Sim, ainda está disponível!",
            "reply_to_message": {"message_id": TELEGRAM_NOTIFICATION_ID},
        },
    }
    update.update(overrides)
    return update


def webhook_headers() -> dict[str, str]:
    """Return the configured Telegram authenticity header."""

    return {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}


def telegram_success(request: httpx.Request) -> httpx.Response:
    """Return a valid Telegram sendMessage response."""

    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {"message_id": 999, "chat": {"id": body["chat_id"]}},
        },
    )


async def test_valid_reply_maps_to_exact_olx_chat_and_message(
    schema_database: Database,
    capsys,
) -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/autoservice/v1/chat/send")
        return httpx.Response(200, json={"ok": True})

    async with reply_test_client(schema_database, upstream) as (client, worker):
        first = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=reply_update(),
        )
        assert requests == []
        assert await worker.process_once() is True
        duplicate = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=reply_update(),
        )
        async with schema_database.session_factory() as session:
            outbound = await session.scalar(select(OutboundMessage))
            update = await session.scalar(select(TelegramUpdate))
            job = await session.scalar(select(DeliveryJob))

    visible_output = capsys.readouterr().out
    assert first.status_code == 200
    assert first.json() == {"status": "queued"}
    assert duplicate.json() == {"status": "duplicate"}
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {OLX_TOKEN}"
    assert json.loads(requests[0].content) == {
        "textMessage": "Sim, ainda está disponível!",
        "messageId": OLX_MESSAGE_ID,
        "chatId": OLX_CHAT_ID,
    }
    assert OLX_TOKEN not in visible_output
    assert outbound is not None
    assert outbound.status == OutboundMessageStatus.SENT
    assert outbound.attempts == 1
    assert outbound.sent_at is not None
    assert update is not None
    assert update.status == TelegramUpdateStatus.PROCESSED
    assert job is not None
    assert job.status == DeliveryJobStatus.SUCCEEDED


async def test_message_without_valid_reply_only_sends_guidance(
    schema_database: Database,
) -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/sendMessage")
        return telegram_success(request)

    update = reply_update(
        update_id=8102,
        message={
            "message_id": 902,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "Resposta sem Reply",
        },
    )
    async with reply_test_client(schema_database, upstream) as (client, _worker):
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=update,
        )
        async with schema_database.session_factory() as session:
            outbound = await session.scalar(select(OutboundMessage))

    assert response.status_code == 200
    assert len(requests) == 1
    assert "use Reply" in json.loads(requests[0].content)["text"]
    assert outbound is None


async def test_unknown_or_seller_mapping_never_reaches_olx(
    schema_database: Database,
) -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return telegram_success(request)

    async with reply_test_client(
        schema_database,
        upstream,
        mapping_origin="seller",
    ) as (client, _worker):
        seller_mapping = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=reply_update(update_id=8103),
        )
        unknown = reply_update(
            update_id=8104,
            message={
                "message_id": 904,
                "from": {"id": ALLOWED_USER_ID},
                "chat": {"id": int(TARGET_CHAT_ID)},
                "text": "Resposta",
                "reply_to_message": {"message_id": 999999},
            },
        )
        unknown_mapping = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=unknown,
        )

    assert seller_mapping.status_code == 200
    assert unknown_mapping.status_code == 200
    assert len(requests) == 2
    assert all(request.url.path.endswith("/sendMessage") for request in requests)


async def test_empty_or_overlong_reply_text_never_reaches_olx(
    schema_database: Database,
) -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/sendMessage")
        return telegram_success(request)

    empty = reply_update(
        update_id=8110,
        message={
            "message_id": 910,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "   ",
            "reply_to_message": {"message_id": TELEGRAM_NOTIFICATION_ID},
        },
    )
    overlong = reply_update(
        update_id=8111,
        message={
            "message_id": 911,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "x" * 4_097,
            "reply_to_message": {"message_id": TELEGRAM_NOTIFICATION_ID},
        },
    )
    async with reply_test_client(schema_database, upstream) as (client, _worker):
        empty_response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=empty,
        )
        overlong_response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=overlong,
        )
        async with schema_database.session_factory() as session:
            outbound = await session.scalar(select(OutboundMessage))

    assert empty_response.status_code == 200
    assert overlong_response.status_code == 200
    assert len(requests) == 2
    assert all("1 e 4096" in json.loads(request.content)["text"] for request in requests)
    assert outbound is None


async def test_olx_400_is_permanent_and_sends_feedback(schema_database: Database) -> None:
    olx_calls = 0
    telegram_requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal olx_calls
        if request.url.path.endswith("/autoservice/v1/chat/send"):
            olx_calls += 1
            return httpx.Response(400, json={"error": "invalid"})
        telegram_requests.append(request)
        return telegram_success(request)

    async with reply_test_client(schema_database, upstream) as (client, worker):
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=reply_update(update_id=8105),
        )
        assert await worker.process_once() is True
        async with schema_database.session_factory() as session:
            outbound = await session.scalar(select(OutboundMessage))

    assert response.status_code == 200
    assert olx_calls == 1
    assert "rejeitou" in json.loads(telegram_requests[0].content)["text"]
    assert outbound is not None
    assert outbound.status == OutboundMessageStatus.FAILED
    assert outbound.olx_http_status == 400
    assert outbound.attempts == 1
    assert outbound.error_code == "olx_bad_request"


async def test_olx_401_requires_reauthorization_without_retry(
    schema_database: Database,
) -> None:
    olx_calls = 0
    telegram_requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal olx_calls
        if request.url.path.endswith("/autoservice/v1/chat/send"):
            olx_calls += 1
            return httpx.Response(401)
        telegram_requests.append(request)
        return telegram_success(request)

    async with reply_test_client(schema_database, upstream) as (client, worker):
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=reply_update(update_id=8106),
        )
        assert await worker.process_once() is True
        async with schema_database.session_factory() as session:
            outbound = await session.scalar(select(OutboundMessage))
            credential = await session.scalar(select(OlxCredential))

    assert response.status_code == 200
    assert olx_calls == 1
    assert "Reautorize" in json.loads(telegram_requests[0].content)["text"]
    assert outbound is not None
    assert outbound.status == OutboundMessageStatus.FAILED
    assert outbound.olx_http_status == 401
    assert outbound.error_code == "olx_unauthorized"
    assert credential is not None
    assert credential.connection_status == ConnectionStatus.REAUTHORIZATION_REQUIRED
    assert credential.last_401_at is not None


async def test_olx_5xx_retries_with_limit_then_succeeds(schema_database: Database) -> None:
    olx_calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal olx_calls
        assert request.url.path.endswith("/autoservice/v1/chat/send")
        olx_calls += 1
        if olx_calls < 3:
            return httpx.Response(503)
        return httpx.Response(200)

    async with reply_test_client(schema_database, upstream) as (client, worker):
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=reply_update(update_id=8107),
        )
        assert await worker.process_once() is True
        assert await worker.process_once() is True
        assert await worker.process_once() is True
        async with schema_database.session_factory() as session:
            outbound = await session.scalar(select(OutboundMessage))

    assert response.status_code == 200
    assert olx_calls == 3
    assert outbound is not None
    assert outbound.status == OutboundMessageStatus.SENT
    assert outbound.attempts == 3
    assert outbound.error_code is None
    assert outbound.next_attempt_at is None


async def test_olx_timeout_exhaustion_is_bounded_and_reported(
    schema_database: Database,
) -> None:
    olx_calls = 0
    telegram_requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal olx_calls
        if request.url.path.endswith("/autoservice/v1/chat/send"):
            olx_calls += 1
            raise httpx.ReadTimeout("timed out", request=request)
        telegram_requests.append(request)
        return telegram_success(request)

    async with reply_test_client(schema_database, upstream) as (client, worker):
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=reply_update(update_id=8108),
        )
        assert await worker.process_once() is True
        assert await worker.process_once() is True
        assert await worker.process_once() is True
        async with schema_database.session_factory() as session:
            outbound = await session.scalar(select(OutboundMessage))

    assert response.status_code == 200
    assert olx_calls == 3
    assert "temporariamente indisponível" in json.loads(telegram_requests[0].content)["text"]
    assert outbound is not None
    assert outbound.status == OutboundMessageStatus.DEAD_LETTER
    assert outbound.attempts == 3
    assert outbound.error_code == "olx_transient_exhausted"
