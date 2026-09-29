"""Persist OLX chat messages and enqueue buyer notifications."""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import DeliveryJobRepository, OlxChatRepository, OlxMessageRepository
from app.schemas.olx import OlxWebhookPayload


@dataclass(frozen=True, slots=True)
class OlxWebhookResult:
    """Safe processing outcome returned to the HTTP boundary."""

    status: Literal["duplicate", "stored", "queued"]


def abbreviate_chat_id(chat_id: str) -> str:
    """Return a display-safe shortened chat identifier."""

    if len(chat_id) <= 8:
        return f"{chat_id}…"
    return f"{chat_id[:8]}…"


def build_telegram_notification(payload: OlxWebhookPayload) -> str:
    """Render the buyer notification without email or phone."""

    return build_telegram_notification_from_parts(
        buyer_name=payload.name,
        list_id=payload.list_id,
        message=payload.message,
        chat_id=payload.chat_id,
    )


def build_telegram_notification_from_parts(
    *,
    buyer_name: str | None,
    list_id: str,
    message: str,
    chat_id: str,
) -> str:
    """Render a safe notification from persisted records for worker restart recovery."""

    display_name = buyer_name.strip() if buyer_name else ""
    display_name = display_name or "Comprador"
    prefix = f"🟣 Nova mensagem OLX\n\n👤 {display_name}\n📦 Anúncio ID: {list_id}\n💬 "
    suffix = f"\n🆔 Chat: {abbreviate_chat_id(chat_id)}"
    available_message_length = max(1, 4_096 - len(prefix) - len(suffix))
    safe_message = message[:available_message_length]
    return f"{prefix}{safe_message}{suffix}"


class OlxWebhookService:
    """Coordinate durable OLX ingestion and persistent delivery enqueueing."""

    def __init__(
        self,
        *,
        max_attempts: int,
        chats: OlxChatRepository | None = None,
        messages: OlxMessageRepository | None = None,
        jobs: DeliveryJobRepository | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.chats = chats or OlxChatRepository()
        self.messages = messages or OlxMessageRepository()
        self.jobs = jobs or DeliveryJobRepository()

    async def process(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        payload: OlxWebhookPayload,
    ) -> OlxWebhookResult:
        """Persist once and enqueue buyer notification in the same transaction."""

        is_buyer = payload.origin == "buyer"
        async with session_factory.begin() as session:
            await self.chats.upsert(
                session,
                chat_id=payload.chat_id,
                list_id=payload.list_id,
                buyer_name=payload.name if is_buyer else None,
                buyer_email=payload.email if is_buyer else None,
                buyer_phone=payload.phone if is_buyer else None,
                last_message_at=payload.message_timestamp,
            )
            message, created = await self.messages.add_if_new(
                session,
                message_id=payload.message_id,
                chat_id=payload.chat_id,
                list_id=payload.list_id,
                origin=payload.origin,
                sender_type=payload.sender_type,
                text=payload.message,
                olx_timestamp=payload.message_timestamp,
            )

            if created and is_buyer:
                await self.jobs.add_olx_to_telegram_if_new(
                    session,
                    olx_message_id=message.id,
                    max_attempts=self.max_attempts,
                )

        if not created:
            return OlxWebhookResult(status="duplicate")
        if not is_buyer:
            return OlxWebhookResult(status="stored")
        return OlxWebhookResult(status="queued")
