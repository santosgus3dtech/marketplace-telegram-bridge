"""Minimal async client for Telegram Bot API message delivery."""

from dataclasses import dataclass

import httpx

from app.config import Settings


class TelegramDeliveryError(RuntimeError):
    """Telegram could not accept or confirm an outbound message."""


class TelegramDeliveryTransientError(TelegramDeliveryError):
    """Telegram delivery can be retried after backoff."""


class TelegramDeliveryPermanentError(TelegramDeliveryError):
    """Telegram permanently rejected the configured request."""


class TelegramSetupError(RuntimeError):
    """Telegram rejected or could not complete a configuration request."""


@dataclass(frozen=True, slots=True)
class TelegramMessageReceipt:
    """Identifiers returned after a successful Telegram sendMessage call."""

    message_id: int
    chat_id: str


class TelegramClient:
    """Send plain-text notifications through the official Telegram Bot API."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http_client = http_client

    def _endpoint(self, method: str) -> str:
        token = self.settings.telegram_bot_token.get_secret_value()
        if not token:
            raise TelegramDeliveryPermanentError("Telegram delivery is not configured")
        return f"{self.settings.telegram_api_base_url.rstrip('/')}/bot{token}/{method}"

    async def is_available(self) -> bool:
        """Check token validity without exposing bot identity or token details."""

        try:
            response = await self.http_client.get(
                self._endpoint("getMe"),
                timeout=self.settings.http_timeout_seconds,
            )
        except (TelegramDeliveryError, httpx.TimeoutException, httpx.RequestError):
            return False
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and payload.get("ok") is True

    async def send_message(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
    ) -> TelegramMessageReceipt:
        """Send one private notification without logging URL, token, or content."""

        target_chat_id = (
            str(chat_id) if chat_id is not None else self.settings.telegram_target_chat_id
        )
        if not target_chat_id:
            raise TelegramDeliveryPermanentError("Telegram delivery is not configured")

        try:
            response = await self.http_client.post(
                self._endpoint("sendMessage"),
                json={"chat_id": target_chat_id, "text": text},
                timeout=self.settings.http_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError) as error:
            raise TelegramDeliveryTransientError("Telegram API is unavailable") from error

        if response.status_code != 200:
            if response.status_code == 429 or response.status_code >= 500:
                raise TelegramDeliveryTransientError("Telegram API is temporarily unavailable")
            raise TelegramDeliveryPermanentError("Telegram API rejected the notification")

        try:
            payload = response.json()
        except ValueError as error:
            raise TelegramDeliveryTransientError("Telegram response was not valid JSON") from error

        if not isinstance(payload, dict):
            raise TelegramDeliveryTransientError("Telegram response did not confirm delivery")
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        response_chat = result.get("chat") if isinstance(result, dict) else None
        response_chat_id = response_chat.get("id") if isinstance(response_chat, dict) else None
        if payload.get("ok") is not True or not isinstance(message_id, int):
            raise TelegramDeliveryTransientError("Telegram response did not confirm delivery")
        if not isinstance(response_chat_id, (int, str)):
            response_chat_id = target_chat_id
        return TelegramMessageReceipt(message_id=message_id, chat_id=str(response_chat_id))

    async def set_webhook(
        self,
        webhook_url: str,
        *,
        drop_pending_updates: bool = False,
    ) -> None:
        """Configure Telegram delivery with the local webhook secret."""

        secret = self.settings.telegram_webhook_secret.get_secret_value()
        if not webhook_url.startswith("https://") or not secret:
            raise TelegramSetupError("public HTTPS URL and webhook secret are required")
        try:
            response = await self.http_client.post(
                self._endpoint("setWebhook"),
                json={
                    "url": webhook_url,
                    "secret_token": secret,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": drop_pending_updates,
                },
                timeout=self.settings.http_timeout_seconds,
            )
        except (TelegramDeliveryError, httpx.TimeoutException, httpx.RequestError) as error:
            raise TelegramSetupError("Telegram API is unavailable") from error
        try:
            payload = response.json()
        except ValueError as error:
            raise TelegramSetupError("Telegram returned an invalid response") from error
        accepted = (
            response.status_code == 200 and isinstance(payload, dict) and payload.get("ok") is True
        )
        if not accepted:
            raise TelegramSetupError("Telegram rejected the webhook configuration")
