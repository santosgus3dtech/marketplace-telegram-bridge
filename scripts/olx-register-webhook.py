"""Register the configured OLX Chat webhook without printing secrets or tokens."""

import argparse
import asyncio
from urllib.parse import quote

import httpx

from app.clients.olx import (
    OlxChatClient,
    OlxWebhookRegistrationUnavailable,
    OlxWebhookUnauthorized,
)
from app.config import Settings, get_settings
from app.db.models import ConnectionStatus
from app.db.repositories import OlxCredentialRepository
from app.db.session import Database
from app.services.security import TokenCipher, TokenDecryptionError


class OlxWebhookConfigurationError(RuntimeError):
    """Local settings or persisted credentials are not ready for registration."""


async def configure_webhook(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Decrypt the stored OLX token only in memory and register the private URL."""

    resolved_settings = settings or get_settings()
    webhook_secret = resolved_settings.olx_webhook_path_secret.get_secret_value()
    encryption_key = resolved_settings.token_encryption_key.get_secret_value()
    if (
        not resolved_settings.public_base_url.startswith("https://")
        or not webhook_secret
        or not encryption_key
    ):
        raise OlxWebhookConfigurationError("PUBLIC_BASE_URL and OLX secrets are incomplete")

    resolved_database = database or Database(
        resolved_settings.database_url,
        resolved_settings.sqlite_busy_timeout_ms,
    )
    resolved_http_client = http_client or httpx.AsyncClient()
    owns_database = database is None
    owns_http_client = http_client is None
    credentials = OlxCredentialRepository()
    try:
        await resolved_database.initialize()
        async with resolved_database.session_factory() as session:
            credential = await credentials.get_current(session)
        if credential is None:
            raise OlxWebhookConfigurationError("no OLX access token is stored")
        try:
            access_token = TokenCipher(encryption_key).decrypt(credential.access_token_encrypted)
        except (TokenDecryptionError, ValueError) as error:
            raise OlxWebhookConfigurationError("stored OLX token is unavailable") from error

        webhook_url = (
            f"{resolved_settings.public_base_url.rstrip('/')}/webhooks/olx/"
            f"{quote(webhook_secret, safe='')}"
        )
        client = OlxChatClient(resolved_settings, resolved_http_client)
        try:
            await client.register_webhook(
                access_token=access_token,
                webhook_url=webhook_url,
            )
        except OlxWebhookUnauthorized:
            async with resolved_database.session_factory.begin() as session:
                await credentials.require_reauthorization(session)
            raise

        async with resolved_database.session_factory.begin() as session:
            await credentials.set_connection_status(
                session,
                connection_status=ConnectionStatus.CONNECTED,
            )
    finally:
        if owns_http_client:
            await resolved_http_client.aclose()
        if owns_database:
            await resolved_database.dispose()


def main() -> int:
    """Run the safe OLX webhook registration command."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        asyncio.run(configure_webhook())
    except OlxWebhookConfigurationError as error:
        parser.exit(1, f"Configuração OLX incompleta: {error}\n")
    except OlxWebhookUnauthorized:
        parser.exit(1, "A OLX recusou o token; reautorização necessária.\n")
    except OlxWebhookRegistrationUnavailable as error:
        parser.exit(1, f"Falha ao registrar webhook OLX: {error}\n")
    print("Webhook oficial da OLX registrado com sucesso.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
