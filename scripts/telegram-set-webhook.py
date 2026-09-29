"""Configure Telegram's webhook without printing the bot token."""

import argparse
import asyncio

import httpx

from app.clients.telegram import TelegramClient, TelegramSetupError
from app.config import get_settings


async def configure_webhook(drop_pending_updates: bool) -> None:
    """Apply the configured public URL and webhook secret to Telegram."""

    settings = get_settings()
    webhook_url = f"{settings.public_base_url.rstrip('/')}/webhooks/telegram"
    async with httpx.AsyncClient() as http_client:
        telegram = TelegramClient(settings, http_client)
        await telegram.set_webhook(
            webhook_url,
            drop_pending_updates=drop_pending_updates,
        )
    print(f"Webhook Telegram configurado em {webhook_url}")


def main() -> int:
    """Run the safe local webhook configurator."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-pending-updates",
        action="store_true",
        help="Descartar updates antigos ao configurar o webhook.",
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(configure_webhook(arguments.drop_pending_updates))
    except TelegramSetupError as error:
        parser.exit(1, f"Falha ao configurar webhook: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
