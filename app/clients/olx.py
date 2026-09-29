"""Minimal async client for the official OLX OAuth token endpoint."""

from dataclasses import dataclass

import httpx

from app.config import Settings


class OlxTokenExchangeError(RuntimeError):
    """Base class for safe token exchange failures."""


class OlxTokenExchangeRejected(OlxTokenExchangeError):
    """The OLX authorization server rejected the authorization code."""


class OlxTokenExchangeUnavailable(OlxTokenExchangeError):
    """The OLX authorization server could not provide a usable response."""


class OlxWebhookRegistrationError(RuntimeError):
    """Base class for safe OLX webhook registration failures."""


class OlxWebhookUnauthorized(OlxWebhookRegistrationError):
    """The OLX access token is invalid and reauthorization is required."""


class OlxWebhookRegistrationUnavailable(OlxWebhookRegistrationError):
    """OLX could not register the configured webhook."""


class OlxMessageSendError(RuntimeError):
    """Base class for safe OLX Chat message delivery failures."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class OlxMessageBadRequest(OlxMessageSendError):
    """OLX permanently rejected the reply payload."""


class OlxMessageUnauthorized(OlxMessageSendError):
    """The OLX token is no longer authorized for Chat."""


class OlxMessageTransientError(OlxMessageSendError):
    """OLX delivery may succeed if retried after backoff."""


class OlxMessagePermanentError(OlxMessageSendError):
    """OLX returned a non-retryable unexpected response."""


@dataclass(frozen=True, slots=True)
class OlxTokenGrant:
    """Validated subset of the documented OLX token response."""

    access_token: str
    token_type: str


class OlxOAuthClient:
    """Exchange an authorization code using the documented form request."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http_client = http_client

    async def exchange_code(self, code: str) -> OlxTokenGrant:
        """Exchange one short-lived code without logging request or response bodies."""

        try:
            response = await self.http_client.post(
                self.settings.olx_token_url,
                data={
                    "code": code,
                    "client_id": self.settings.olx_client_id,
                    "client_secret": self.settings.olx_client_secret.get_secret_value(),
                    "redirect_uri": self.settings.olx_redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
                timeout=self.settings.http_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError) as error:
            raise OlxTokenExchangeUnavailable("OLX token endpoint is unavailable") from error

        if response.status_code == 400:
            raise OlxTokenExchangeRejected("OLX rejected the authorization code")
        if response.status_code != 200:
            raise OlxTokenExchangeUnavailable("OLX token endpoint returned an unexpected status")

        try:
            payload = response.json()
        except ValueError as error:
            raise OlxTokenExchangeUnavailable("OLX token response was not valid JSON") from error

        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        token_type = payload.get("token_type") if isinstance(payload, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise OlxTokenExchangeUnavailable("OLX token response did not include an access token")
        if not isinstance(token_type, str) or not token_type:
            raise OlxTokenExchangeUnavailable("OLX token response did not include a token type")
        return OlxTokenGrant(access_token=access_token, token_type=token_type)


class OlxChatClient:
    """Use the official OLX Chat endpoints with an OAuth access token."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http_client = http_client

    async def register_webhook(self, *, access_token: str, webhook_url: str) -> None:
        """Create or update the webhook without exposing token or response bodies."""

        try:
            response = await self.http_client.post(
                self.settings.olx_chat_config_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                json={"webhook": webhook_url},
                timeout=self.settings.http_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError) as error:
            raise OlxWebhookRegistrationUnavailable(
                "OLX webhook endpoint is unavailable"
            ) from error

        if response.status_code in {200, 201}:
            return
        if response.status_code == 401:
            raise OlxWebhookUnauthorized("OLX access token was rejected")
        raise OlxWebhookRegistrationUnavailable(
            "OLX webhook endpoint returned an unexpected status"
        )

    async def send_message(
        self,
        *,
        access_token: str,
        text_message: str,
        message_id: str,
        chat_id: str,
    ) -> None:
        """Send one Reply to its mapped OLX chat without logging sensitive data."""

        try:
            response = await self.http_client.post(
                self.settings.olx_chat_send_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                json={
                    "textMessage": text_message,
                    "messageId": message_id,
                    "chatId": chat_id,
                },
                timeout=self.settings.http_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError) as error:
            raise OlxMessageTransientError("OLX Chat send endpoint is unavailable") from error

        if response.status_code == 200:
            return
        if response.status_code == 400:
            raise OlxMessageBadRequest(
                "OLX rejected the reply payload",
                http_status=response.status_code,
            )
        if response.status_code == 401:
            raise OlxMessageUnauthorized(
                "OLX access token was rejected",
                http_status=response.status_code,
            )
        if response.status_code >= 500:
            raise OlxMessageTransientError(
                "OLX Chat send endpoint returned a transient status",
                http_status=response.status_code,
            )
        raise OlxMessagePermanentError(
            "OLX Chat send endpoint returned a non-retryable status",
            http_status=response.status_code,
        )
