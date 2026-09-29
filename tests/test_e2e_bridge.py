"""Simulated end-to-end scenarios for both bridge directions."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import (
    ConnectionStatus,
    DeliveryJob,
    DeliveryJobStatus,
    OlxCredential,
    OlxMessage,
    OutboundMessage,
    OutboundMessageStatus,
    TelegramUpdate,
    TelegramUpdateStatus,
)
from app.db.session import Database
from app.main import create_app
from app.services.delivery import DeliveryWorker
from app.services.security import TokenCipher

OLX_WEBHOOK_SECRET = "e2e-olx-webhook-path"  # noqa: S105
TELEGRAM_WEBHOOK_SECRET = "e2e-telegram-webhook-secret"  # noqa: S105
TELEGRAM_BOT_TOKEN = "e2e-telegram-bot-token"  # noqa: S105
OLX_ACCESS_TOKEN = "e2e-olx-access-token"  # noqa: S105
TARGET_CHAT_ID = "-100900800"
ALLOWED_USER_ID = 700600
OLX_SOURCE_IP = "54.162.151.93"
TELEGRAM_NOTIFICATION_ID = 500


@dataclass(slots=True)
class MockUpstream:
    """Route fake OLX and Telegram requests while retaining safe evidence."""

    olx_send_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)
    telegram_bodies: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": OLX_ACCESS_TOKEN, "token_type": "Bearer"},
            )
        if path.endswith("/autoservice/v1/chat/send"):
            return httpx.Response(self.olx_send_status, json={"accepted": True})
        if path.endswith("/autoservice/v1/chat"):
            return httpx.Response(201, json={"registered": True})
        if path.endswith("/sendMessage"):
            body = json.loads(request.content)
            self.telegram_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": TELEGRAM_NOTIFICATION_ID + len(self.telegram_bodies) - 1,
                        "chat": {"id": int(body["chat_id"])},
                    },
                },
            )
        raise AssertionError(f"unexpected simulated upstream path: {path}")

    def count_path(self, suffix: str) -> int:
        """Count requests to one fake endpoint without inspecting credentials."""

        return sum(request.url.path.endswith(suffix) for request in self.requests)


def e2e_settings(database: Database) -> Settings:
    """Build complete settings containing only synthetic test values."""

    return Settings(
        _env_file=None,
        app_env="test",
        database_url=str(database.engine.url),
        log_level="WARNING",
        public_base_url="https://bridge.example.test",
        olx_client_id="e2e-client-id",
        olx_client_secret="e2e-client-secret",  # noqa: S106
        olx_redirect_uri="https://bridge.example.test/oauth/olx/callback",
        olx_webhook_path_secret=OLX_WEBHOOK_SECRET,
        olx_allowed_source_ip=OLX_SOURCE_IP,
        telegram_bot_token=TELEGRAM_BOT_TOKEN,
        telegram_target_chat_id=TARGET_CHAT_ID,
        telegram_allowed_user_ids=str(ALLOWED_USER_ID),
        telegram_webhook_secret=TELEGRAM_WEBHOOK_SECRET,
        token_encryption_key=Fernet.generate_key().decode("ascii"),
        delivery_worker_enabled=False,
        delivery_max_attempts=3,
        delivery_backoff_seconds=0,
    )


@asynccontextmanager
async def e2e_client(
    database: Database,
    upstream: MockUpstream,
) -> AsyncIterator[tuple[httpx.AsyncClient, DeliveryWorker, Settings]]:
    """Run one real ASGI application with only its providers mocked."""

    settings = e2e_settings(database)
    outbound_http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    application = create_app(
        settings=settings,
        database=database,
        outbound_http_client=outbound_http,
    )
    try:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                yield (
                    client,
                    DeliveryWorker(
                        settings=settings,
                        database=database,
                        http_client=outbound_http,
                    ),
                    settings,
                )
    finally:
        await outbound_http.aclose()


async def authorize_olx(client: httpx.AsyncClient) -> None:
    """Complete the synthetic authorization-code flow through HTTP routes."""

    start = await client.get("/oauth/olx/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = await client.get(
        "/oauth/olx/callback",
        params={"code": "e2e-authorization-code", "state": state},
    )
    assert start.status_code == 302
    assert callback.status_code == 200


def buyer_message() -> dict[str, object]:
    """Return one fictional payload matching the official OLX schema."""

    return {
        "chatId": "e2e-chat-001",
        "message": "Produto de exemplo ainda disponível?",
        "senderType": "account",
        "email": "pessoa@example.test",
        "name": "Pessoa Exemplo",
        "phone": "+5500000000000",
        "messageTimestamp": "2026-09-29T10:15:00.000",
        "messageId": "e2e-message-001",
        "origin": "buyer",
        "listId": "e2e-listing-001",
    }


def telegram_reply(*, update_id: int = 9001, user_id: int = ALLOWED_USER_ID) -> dict:
    """Return one fictional Telegram Reply tied to message 500."""

    return {
        "update_id": update_id,
        "message": {
            "message_id": 600,
            "from": {"id": user_id},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "Sim, o produto de exemplo está disponível.",
            "reply_to_message": {"message_id": TELEGRAM_NOTIFICATION_ID},
        },
    }


def olx_headers() -> dict[str, str]:
    return {"CF-Connecting-IP": OLX_SOURCE_IP}


def telegram_headers() -> dict[str, str]:
    return {"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET}


async def deliver_buyer_notification(
    client: httpx.AsyncClient,
    worker: DeliveryWorker,
) -> None:
    """Ingest a buyer webhook and deliver its queued Telegram notification."""

    response = await client.post(
        f"/webhooks/olx/{OLX_WEBHOOK_SECRET}",
        headers=olx_headers(),
        json=buyer_message(),
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert await worker.process_once() is True


async def test_complete_oauth_buyer_notification_and_reply_flow_is_idempotent(
    schema_database: Database,
) -> None:
    """Cover Prompt 09 scenarios A, B, C and D in one application lifecycle."""

    upstream = MockUpstream()
    async with e2e_client(schema_database, upstream) as (client, worker, settings):
        await authorize_olx(client)
        await deliver_buyer_notification(client, worker)

        duplicate_olx = await client.post(
            f"/webhooks/olx/{OLX_WEBHOOK_SECRET}",
            headers=olx_headers(),
            json=buyer_message(),
        )
        reply = await client.post(
            "/webhooks/telegram",
            headers=telegram_headers(),
            json=telegram_reply(),
        )
        assert await worker.process_once() is True
        duplicate_telegram = await client.post(
            "/webhooks/telegram",
            headers=telegram_headers(),
            json=telegram_reply(),
        )
        assert await worker.process_once() is False

        async with schema_database.session_factory() as session:
            credential = await session.scalar(select(OlxCredential))
            message = await session.scalar(select(OlxMessage))
            outbound = await session.scalar(select(OutboundMessage))
            olx_message_count = await session.scalar(select(func.count()).select_from(OlxMessage))
            outbound_count = await session.scalar(select(func.count()).select_from(OutboundMessage))
            jobs = (await session.scalars(select(DeliveryJob))).all()

    assert credential is not None
    assert credential.connection_status == ConnectionStatus.CONNECTED
    assert credential.access_token_encrypted != OLX_ACCESS_TOKEN
    assert (
        TokenCipher(settings.token_encryption_key).decrypt(credential.access_token_encrypted)
        == OLX_ACCESS_TOKEN
    )
    assert message is not None
    assert message.telegram_message_id == TELEGRAM_NOTIFICATION_ID
    assert message.telegram_chat_id == TARGET_CHAT_ID
    assert duplicate_olx.json() == {"status": "duplicate"}
    assert reply.json() == {"status": "queued"}
    assert duplicate_telegram.json() == {"status": "duplicate"}
    assert outbound is not None
    assert outbound.status == OutboundMessageStatus.SENT
    assert outbound.olx_chat_id == "e2e-chat-001"
    assert outbound.olx_reference_message_id == "e2e-message-001"
    assert olx_message_count == 1
    assert outbound_count == 1
    assert all(job.status == DeliveryJobStatus.SUCCEEDED for job in jobs)
    assert upstream.count_path("/sendMessage") == 1
    assert upstream.count_path("/autoservice/v1/chat/send") == 1
    olx_send = next(
        request
        for request in upstream.requests
        if request.url.path.endswith("/autoservice/v1/chat/send")
    )
    assert json.loads(olx_send.content) == {
        "textMessage": "Sim, o produto de exemplo está disponível.",
        "messageId": "e2e-message-001",
        "chatId": "e2e-chat-001",
    }


async def test_olx_send_401_disconnects_and_alerts_telegram(
    schema_database: Database,
) -> None:
    """Cover Prompt 09 scenario E through the complete queued delivery path."""

    upstream = MockUpstream(olx_send_status=401)
    async with e2e_client(schema_database, upstream) as (client, worker, _settings):
        await authorize_olx(client)
        await deliver_buyer_notification(client, worker)
        reply = await client.post(
            "/webhooks/telegram",
            headers=telegram_headers(),
            json=telegram_reply(update_id=9002),
        )
        assert await worker.process_once() is True
        assert await worker.process_once() is False

        async with schema_database.session_factory() as session:
            credential = await session.scalar(select(OlxCredential))
            outbound = await session.scalar(select(OutboundMessage))

    assert reply.json() == {"status": "queued"}
    assert credential is not None
    assert credential.connection_status == ConnectionStatus.REAUTHORIZATION_REQUIRED
    assert credential.last_401_at is not None
    assert outbound is not None
    assert outbound.status == OutboundMessageStatus.FAILED
    assert outbound.olx_http_status == 401
    assert outbound.error_code == "olx_unauthorized"
    assert upstream.count_path("/autoservice/v1/chat/send") == 1
    assert upstream.count_path("/sendMessage") == 2
    assert "Reautorize" in str(upstream.telegram_bodies[-1]["text"])


async def test_unauthorized_telegram_user_causes_no_external_action(
    schema_database: Database,
) -> None:
    """Cover Prompt 09 scenario F at the public HTTP boundary."""

    upstream = MockUpstream()
    async with e2e_client(schema_database, upstream) as (client, worker, _settings):
        response = await client.post(
            "/webhooks/telegram",
            headers=telegram_headers(),
            json=telegram_reply(update_id=9003, user_id=ALLOWED_USER_ID + 1),
        )
        assert await worker.process_once() is False
        async with schema_database.session_factory() as session:
            update = await session.scalar(select(TelegramUpdate))
            outbound_count = await session.scalar(select(func.count()).select_from(OutboundMessage))

    assert response.status_code == 200
    assert response.json() == {"status": "rejected"}
    assert update is not None
    assert update.status == TelegramUpdateStatus.REJECTED
    assert outbound_count == 0
    assert upstream.requests == []
