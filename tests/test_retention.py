"""Privacy retention tests for old terminal bridge records."""

from datetime import timedelta

from sqlalchemy import func, select

from app.db.models import (
    AuditEvent,
    DeliveryJob,
    DeliveryJobKind,
    DeliveryJobStatus,
    OAuthState,
    OlxChat,
    OlxMessage,
    OutboundMessage,
    OutboundMessageStatus,
    TelegramUpdate,
    TelegramUpdateStatus,
    utc_now,
)
from app.db.session import Database
from app.services.retention import RetentionService


async def seed_old_terminal_flow_and_current_data(database: Database) -> None:
    now = utc_now()
    old = now - timedelta(days=100)
    async with database.session_factory.begin() as session:
        old_chat = OlxChat(
            chat_id="retention-old-chat",
            list_id="retention-old-listing",
            buyer_name="Pessoa Antiga",
            buyer_email="old@example.test",
            buyer_phone="+5500000000000",
            last_message_at=old,
            created_at=old,
            updated_at=old,
        )
        current_chat = OlxChat(
            chat_id="retention-current-chat",
            list_id="retention-current-listing",
            buyer_name="Pessoa Atual",
            last_message_at=now,
        )
        session.add_all((old_chat, current_chat))
        await session.flush()

        old_message = OlxMessage(
            message_id="retention-old-message",
            chat_id=old_chat.chat_id,
            list_id=old_chat.list_id,
            origin="buyer",
            sender_type="account",
            text="Mensagem expirada",
            olx_timestamp=old,
            received_at=old,
            telegram_message_id=700,
            telegram_chat_id="-100700",
        )
        current_message = OlxMessage(
            message_id="retention-current-message",
            chat_id=current_chat.chat_id,
            list_id=current_chat.list_id,
            origin="buyer",
            sender_type="account",
            text="Mensagem atual",
            olx_timestamp=now,
            received_at=now,
        )
        old_update = TelegramUpdate(
            update_id=700,
            received_at=old,
            processed_at=old,
            status=TelegramUpdateStatus.PROCESSED,
        )
        current_update = TelegramUpdate(
            update_id=701,
            received_at=now,
            processed_at=now,
            status=TelegramUpdateStatus.PROCESSED,
        )
        session.add_all((old_message, current_message, old_update, current_update))
        await session.flush()

        old_outbound = OutboundMessage(
            telegram_update_id=old_update.update_id,
            olx_chat_id=old_chat.chat_id,
            olx_reference_message_id=old_message.message_id,
            text="Resposta expirada",
            status=OutboundMessageStatus.SENT,
            attempts=1,
            created_at=old,
            updated_at=old,
            sent_at=old,
        )
        session.add(old_outbound)
        await session.flush()

        session.add_all(
            (
                DeliveryJob(
                    kind=DeliveryJobKind.OLX_TO_TELEGRAM,
                    status=DeliveryJobStatus.SUCCEEDED,
                    olx_message_id=old_message.id,
                    attempts=1,
                    max_attempts=3,
                    next_attempt_at=old,
                    created_at=old,
                    updated_at=old,
                    completed_at=old,
                ),
                DeliveryJob(
                    kind=DeliveryJobKind.TELEGRAM_TO_OLX,
                    status=DeliveryJobStatus.SUCCEEDED,
                    outbound_message_id=old_outbound.id,
                    attempts=1,
                    max_attempts=3,
                    next_attempt_at=old,
                    created_at=old,
                    updated_at=old,
                    completed_at=old,
                ),
                OAuthState(
                    state_hash="a" * 64,
                    created_at=old,
                    expires_at=old,
                    used_at=old,
                ),
                OAuthState(
                    state_hash="b" * 64,
                    expires_at=now + timedelta(minutes=10),
                ),
                AuditEvent(event_type="old", metadata_json={}, created_at=old),
                AuditEvent(event_type="current", metadata_json={}, created_at=now),
            )
        )


async def count(database: Database, model: type) -> int:
    async with database.session_factory() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


async def test_retention_deletes_only_expired_terminal_records(
    schema_database: Database,
) -> None:
    await seed_old_terminal_flow_and_current_data(schema_database)
    service = RetentionService(
        database=schema_database,
        message_retention_days=30,
        audit_retention_days=30,
    )

    result = await service.prune()

    assert result.delivery_jobs == 2
    assert result.outbound_messages == 1
    assert result.telegram_updates == 1
    assert result.olx_messages == 1
    assert result.olx_chats == 1
    assert result.oauth_states == 1
    assert result.audit_events == 1
    assert await count(schema_database, DeliveryJob) == 0
    assert await count(schema_database, OutboundMessage) == 0
    assert await count(schema_database, TelegramUpdate) == 1
    assert await count(schema_database, OlxMessage) == 1
    assert await count(schema_database, OlxChat) == 1
    assert await count(schema_database, OAuthState) == 1
    assert await count(schema_database, AuditEvent) == 1
