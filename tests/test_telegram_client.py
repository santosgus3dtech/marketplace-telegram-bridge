"""Telegram Bot API client configuration tests."""

import json

import httpx
import pytest

from app.clients.telegram import TelegramClient, TelegramSetupError
from app.config import Settings


async def test_set_webhook_uses_secret_and_message_only_updates(capsys) -> None:
    requests: list[httpx.Request] = []
    token = "token-not-for-logs"  # noqa: S105
    secret = "webhook_secret_for_test"  # noqa: S105
    settings = Settings(
        _env_file=None,
        app_env="test",
        telegram_bot_token=token,
        telegram_webhook_secret=secret,
    )

    def telegram_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram_response)) as client:
        telegram = TelegramClient(settings, client)
        await telegram.set_webhook(
            "https://bridge.example/webhooks/telegram",
            drop_pending_updates=True,
        )

    body = json.loads(requests[0].content)
    visible_output = capsys.readouterr().out
    assert body == {
        "url": "https://bridge.example/webhooks/telegram",
        "secret_token": secret,
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    }
    assert token not in visible_output
    assert secret not in visible_output


async def test_set_webhook_requires_https_and_configured_secret() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        telegram_bot_token="token-for-test",  # noqa: S106
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None)) as client:
        telegram = TelegramClient(settings, client)
        with pytest.raises(TelegramSetupError, match="HTTPS URL"):
            await telegram.set_webhook("http://localhost/webhooks/telegram")
