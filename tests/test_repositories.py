"""Repository idempotency and timestamp tests."""

from datetime import UTC

from sqlalchemy import func, select

from app.db.models import OlxMessage, TelegramUpdate, utc_now
from app.db.repositories import (
    OlxChatRepository,
    OlxMessageRepository,
    TelegramUpdateRepository,
)
from app.db.session import Database


async def test_olx_message_is_idempotent_by_message_id(schema_database: Database) -> None:
    chat_repository = OlxChatRepository()
    message_repository = OlxMessageRepository()

    async with schema_database.session_factory() as session:
        await chat_repository.upsert(session, chat_id="chat-1", list_id="listing-1")
        first, first_created = await message_repository.add_if_new(
            session,
            message_id="message-1",
            chat_id="chat-1",
            list_id="listing-1",
            origin="buyer",
            sender_type="account",
            text="Mensagem de teste",
            olx_timestamp=utc_now(),
        )
        duplicate, duplicate_created = await message_repository.add_if_new(
            session,
            message_id="message-1",
            chat_id="chat-1",
            list_id="listing-1",
            origin="buyer",
            sender_type="account",
            text="Conteúdo duplicado não deve substituir o original",
            olx_timestamp=utc_now(),
        )
        await session.commit()

        count = await session.scalar(select(func.count()).select_from(OlxMessage))

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert duplicate.text == "Mensagem de teste"
    assert count == 1
    assert first.received_at.tzinfo == UTC


async def test_telegram_update_is_idempotent_by_update_id(
    schema_database: Database,
) -> None:
    repository = TelegramUpdateRepository()

    async with schema_database.session_factory() as session:
        first, first_created = await repository.add_if_new(session, update_id=9001)
        duplicate, duplicate_created = await repository.add_if_new(session, update_id=9001)
        await session.commit()

        count = await session.scalar(select(func.count()).select_from(TelegramUpdate))

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert count == 1
