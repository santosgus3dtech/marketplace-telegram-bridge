"""Telegram webhook authorization, commands, and idempotency tests."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db.models import ConnectionStatus, TelegramUpdate, TelegramUpdateStatus
from app.db.repositories import OlxCredentialRepository
from app.db.session import Database
from app.main import create_app

ResponseHandler = Callable[[httpx.Request], httpx.Response]
WEBHOOK_SECRET = "telegram_webhook_secret_for_test"  # noqa: S105
BOT_TOKEN = "telegram-bot-token-for-test"  # noqa: S105
TARGET_CHAT_ID = "-100200300"
ALLOWED_USER_ID = 900100


def telegram_settings(database: Database, **overrides: object) -> Settings:
    """Return isolated Telegram configuration for tests."""

    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": str(database.engine.url),
        "log_level": "INFO",
        "telegram_bot_token": BOT_TOKEN,
        "telegram_target_chat_id": TARGET_CHAT_ID,
        "telegram_allowed_user_ids": str(ALLOWED_USER_ID),
        "telegram_webhook_secret": WEBHOOK_SECRET,
    }
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def telegram_test_client(
    database: Database,
    handler: ResponseHandler,
    **setting_overrides: object,
) -> AsyncIterator[httpx.AsyncClient]:
    """Run the application with a mocked Telegram Bot API."""

    settings = telegram_settings(database, **setting_overrides)
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
                yield test_client
    finally:
        await upstream.aclose()


def valid_update(**overrides: object) -> dict[str, object]:
    """Build one minimal official Telegram message update."""

    update: dict[str, object] = {
        "update_id": 7001,
        "message": {
            "message_id": 501,
            "from": {"id": ALLOWED_USER_ID, "is_bot": False, "first_name": "Teste"},
            "chat": {"id": int(TARGET_CHAT_ID), "type": "supergroup"},
            "date": 1_790_000_000,
            "text": "/help",
        },
    }
    update.update(overrides)
    return update


def webhook_headers(secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    """Return the Telegram authenticity header."""

    return {"X-Telegram-Bot-Api-Secret-Token": secret}


def successful_send(request: httpx.Request) -> httpx.Response:
    """Confirm a mocked Telegram sendMessage request."""

    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {"message_id": 8001, "chat": {"id": body["chat_id"]}},
        },
    )


async def test_invalid_header_is_rejected_before_processing(schema_database: Database) -> None:
    calls = 0

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with telegram_test_client(schema_database, unexpected_request) as client:
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers("wrong-secret"),
            json=valid_update(),
        )

    assert response.status_code == 403
    assert calls == 0


async def test_wrong_chat_and_user_are_rejected_without_bot_calls(
    schema_database: Database,
) -> None:
    calls = 0

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    wrong_chat_update = valid_update(
        update_id=7002,
        message={
            "message_id": 502,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": -999},
            "text": "/help",
        },
    )
    wrong_user_update = valid_update(
        update_id=7003,
        message={
            "message_id": 503,
            "from": {"id": 123456},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "/help",
        },
    )

    async with telegram_test_client(schema_database, unexpected_request) as client:
        wrong_chat = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=wrong_chat_update,
        )
        wrong_user = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=wrong_user_update,
        )
        async with schema_database.session_factory() as session:
            records = (await session.scalars(select(TelegramUpdate))).all()

    assert wrong_chat.status_code == 200
    assert wrong_chat.json() == {"status": "rejected"}
    assert wrong_user.status_code == 200
    assert wrong_user.json() == {"status": "rejected"}
    assert calls == 0
    assert {record.status for record in records} == {TelegramUpdateStatus.REJECTED}


async def test_help_command_is_sent_once_for_duplicate_update(
    schema_database: Database,
) -> None:
    requests: list[httpx.Request] = []

    def telegram_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return successful_send(request)

    async with telegram_test_client(schema_database, telegram_response) as client:
        first = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=valid_update(),
        )
        duplicate = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=valid_update(),
        )
        async with schema_database.session_factory() as session:
            stored = await session.scalar(select(TelegramUpdate))

    assert first.status_code == 200
    assert first.json() == {"status": "processed"}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}
    assert len(requests) == 1
    request_body = json.loads(requests[0].content)
    assert request_body["chat_id"] == TARGET_CHAT_ID
    assert "/help" in request_body["text"]
    assert "/status" in request_body["text"]
    assert "/olx_status" in request_body["text"]
    assert stored is not None
    assert stored.status == TelegramUpdateStatus.PROCESSED
    assert stored.processed_at is not None


async def test_status_reports_dependencies_without_secrets(
    schema_database: Database,
    capsys,
) -> None:
    requests: list[httpx.Request] = []

    def telegram_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "Bot"}},
            )
        return successful_send(request)

    status_update = valid_update(
        update_id=7004,
        message={
            "message_id": 504,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "/status@guussvianna_olx_bridge_bot",
        },
    )
    async with telegram_test_client(schema_database, telegram_response) as client:
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=status_update,
        )

    visible_output = capsys.readouterr().out
    assert response.status_code == 200
    assert len(requests) == 2
    response_body = json.loads(requests[-1].content)
    assert "App: ok" in response_body["text"]
    assert "Banco: ok" in response_body["text"]
    assert "Telegram: ok" in response_body["text"]
    assert "OLX: desconectado" in response_body["text"]
    assert BOT_TOKEN not in response_body["text"]
    assert WEBHOOK_SECRET not in response_body["text"]
    assert BOT_TOKEN not in visible_output
    assert WEBHOOK_SECRET not in visible_output


async def test_olx_status_reflects_current_connection(schema_database: Database) -> None:
    requests: list[httpx.Request] = []
    async with schema_database.session_factory.begin() as session:
        await OlxCredentialRepository().save(
            session,
            access_token_encrypted="encrypted-value",  # noqa: S106
            token_type="Bearer",  # noqa: S106
            connection_status=ConnectionStatus.CONNECTED,
        )

    def telegram_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return successful_send(request)

    update = valid_update(
        update_id=7005,
        message={
            "message_id": 505,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "/olx_status",
        },
    )
    async with telegram_test_client(schema_database, telegram_response) as client:
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=update,
        )

    assert response.status_code == 200
    assert json.loads(requests[0].content)["text"] == "OLX: conectado."


async def test_non_command_text_without_reply_receives_guidance(
    schema_database: Database,
) -> None:
    requests: list[httpx.Request] = []

    def telegram_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return successful_send(request)

    update = valid_update(
        update_id=7006,
        message={
            "message_id": 506,
            "from": {"id": ALLOWED_USER_ID},
            "chat": {"id": int(TARGET_CHAT_ID)},
            "text": "Olá",
        },
    )
    async with telegram_test_client(schema_database, telegram_response) as client:
        response = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=update,
        )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    assert len(requests) == 1
    assert "use Reply" in json.loads(requests[0].content)["text"]


async def test_failed_delivery_can_be_retried_by_telegram(schema_database: Database) -> None:
    calls = 0

    def telegram_response(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return successful_send(request)

    update = valid_update(update_id=7007)
    async with telegram_test_client(schema_database, telegram_response) as client:
        first = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=update,
        )
        retry = await client.post(
            "/webhooks/telegram",
            headers=webhook_headers(),
            json=update,
        )

    assert first.status_code == 502
    assert retry.status_code == 200
    assert retry.json() == {"status": "processed"}
    assert calls == 2
