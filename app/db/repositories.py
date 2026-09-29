"""Transaction-scoped repositories for durable bridge state."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditEvent,
    ConnectionStatus,
    DeliveryJob,
    DeliveryJobKind,
    DeliveryJobStatus,
    GmailReplyNotification,
    OAuthState,
    OlxChat,
    OlxCredential,
    OlxListing,
    OlxMessage,
    OutboundMessage,
    OutboundMessageStatus,
    TelegramUpdate,
    TelegramUpdateStatus,
    utc_now,
)


class OAuthStateRepository:
    """Store and atomically consume hashed OAuth states."""

    async def create(
        self,
        session: AsyncSession,
        *,
        state_hash: str,
        expires_at: datetime,
        created_at: datetime | None = None,
    ) -> OAuthState:
        record = OAuthState(
            state_hash=state_hash,
            created_at=created_at or utc_now(),
            expires_at=expires_at,
        )
        session.add(record)
        await session.flush()
        return record

    async def get_by_hash(self, session: AsyncSession, state_hash: str) -> OAuthState | None:
        result = await session.execute(
            select(OAuthState).where(OAuthState.state_hash == state_hash)
        )
        return result.scalar_one_or_none()

    async def consume(
        self,
        session: AsyncSession,
        *,
        state_hash: str,
        consumed_at: datetime | None = None,
    ) -> bool:
        now = consumed_at or utc_now()
        result = await session.execute(
            update(OAuthState)
            .where(
                OAuthState.state_hash == state_hash,
                OAuthState.used_at.is_(None),
                OAuthState.expires_at > now,
            )
            .values(used_at=now)
        )
        return result.rowcount == 1


class OlxCredentialRepository:
    """Persist the current encrypted OLX credential."""

    async def get_current(self, session: AsyncSession) -> OlxCredential | None:
        result = await session.execute(
            select(OlxCredential).order_by(OlxCredential.id.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def save(
        self,
        session: AsyncSession,
        *,
        access_token_encrypted: str,
        token_type: str,
        connection_status: ConnectionStatus = ConnectionStatus.AUTHORIZED,
    ) -> OlxCredential:
        record = await self.get_current(session)
        if record is None:
            record = OlxCredential(
                access_token_encrypted=access_token_encrypted,
                token_type=token_type,
                connection_status=connection_status,
            )
            session.add(record)
        else:
            record.access_token_encrypted = access_token_encrypted
            record.token_type = token_type
            record.connection_status = connection_status
            record.updated_at = utc_now()
        await session.flush()
        return record

    async def require_reauthorization(
        self,
        session: AsyncSession,
        *,
        occurred_at: datetime | None = None,
    ) -> OlxCredential | None:
        record = await self.get_current(session)
        if record is None:
            return None
        timestamp = occurred_at or utc_now()
        record.connection_status = ConnectionStatus.REAUTHORIZATION_REQUIRED
        record.last_401_at = timestamp
        record.updated_at = timestamp
        await session.flush()
        return record

    async def set_connection_status(
        self,
        session: AsyncSession,
        *,
        connection_status: ConnectionStatus,
    ) -> OlxCredential | None:
        """Update the current credential's operational state."""

        record = await self.get_current(session)
        if record is None:
            return None
        record.connection_status = connection_status
        record.updated_at = utc_now()
        await session.flush()
        return record


class OlxListingRepository:
    """Maintain local friendly listing metadata without external API assumptions."""

    async def upsert(
        self,
        session: AsyncSession,
        *,
        list_id: str,
        title: str,
        price: Decimal | None = None,
        status: str = "active",
    ) -> OlxListing:
        result = await session.execute(select(OlxListing).where(OlxListing.list_id == list_id))
        record = result.scalar_one_or_none()
        if record is None:
            record = OlxListing(list_id=list_id, title=title, price=price, status=status)
            session.add(record)
        else:
            record.title = title
            record.price = price
            record.status = status
            record.updated_at = utc_now()
        await session.flush()
        return record


class OlxChatRepository:
    """Create or refresh OLX conversation metadata."""

    async def upsert(
        self,
        session: AsyncSession,
        *,
        chat_id: str,
        list_id: str,
        buyer_name: str | None = None,
        buyer_email: str | None = None,
        buyer_phone: str | None = None,
        last_message_at: datetime | None = None,
    ) -> OlxChat:
        values = {
            "chat_id": chat_id,
            "list_id": list_id,
            "buyer_name": buyer_name,
            "buyer_email": buyer_email,
            "buyer_phone": buyer_phone,
            "last_message_at": last_message_at,
        }
        updates: dict[str, Any] = {
            "list_id": list_id,
            "last_message_at": last_message_at,
            "updated_at": utc_now(),
        }
        if buyer_name is not None:
            updates["buyer_name"] = buyer_name
        if buyer_email is not None:
            updates["buyer_email"] = buyer_email
        if buyer_phone is not None:
            updates["buyer_phone"] = buyer_phone

        statement = (
            sqlite_insert(OlxChat)
            .values(**values)
            .on_conflict_do_update(index_elements=["chat_id"], set_=updates)
        )
        await session.execute(statement)
        result = await session.execute(select(OlxChat).where(OlxChat.chat_id == chat_id))
        record = result.scalar_one_or_none()
        if record is None:
            raise RuntimeError("OLX chat upsert did not produce a durable record")
        return record

    async def get_by_chat_id(self, session: AsyncSession, chat_id: str) -> OlxChat | None:
        """Return persisted conversation metadata for notification rendering."""

        result = await session.execute(select(OlxChat).where(OlxChat.chat_id == chat_id))
        return result.scalar_one_or_none()


class OlxMessageRepository:
    """Insert OLX messages idempotently by the official message ID."""

    async def add_if_new(
        self,
        session: AsyncSession,
        *,
        message_id: str,
        chat_id: str,
        list_id: str,
        origin: str,
        sender_type: str,
        text: str,
        olx_timestamp: datetime,
    ) -> tuple[OlxMessage, bool]:
        statement = (
            sqlite_insert(OlxMessage)
            .values(
                message_id=message_id,
                chat_id=chat_id,
                list_id=list_id,
                origin=origin,
                sender_type=sender_type,
                text=text,
                olx_timestamp=olx_timestamp,
                received_at=utc_now(),
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(OlxMessage.id)
        )
        result = await session.execute(statement)
        inserted_id = result.scalar_one_or_none()
        created = inserted_id is not None

        if created:
            record = await session.get(OlxMessage, inserted_id)
        else:
            existing = await session.execute(
                select(OlxMessage).where(OlxMessage.message_id == message_id)
            )
            record = existing.scalar_one_or_none()
        if record is None:
            raise RuntimeError("OLX message insert did not produce a durable record")
        return record, created

    async def set_telegram_mapping(
        self,
        session: AsyncSession,
        *,
        message_id: str,
        telegram_message_id: int,
        telegram_chat_id: str,
    ) -> OlxMessage:
        """Attach the Telegram delivery identifiers used by future Reply routing."""

        result = await session.execute(
            update(OlxMessage)
            .where(OlxMessage.message_id == message_id)
            .values(
                telegram_message_id=telegram_message_id,
                telegram_chat_id=telegram_chat_id,
            )
            .returning(OlxMessage.id)
        )
        record_id = result.scalar_one_or_none()
        if record_id is None:
            raise RuntimeError("OLX message was not found for Telegram mapping")
        record = await session.get(OlxMessage, record_id)
        if record is None:
            raise RuntimeError("OLX message mapping did not produce a durable record")
        return record

    async def get_by_telegram_message_id(
        self,
        session: AsyncSession,
        *,
        telegram_message_id: int,
        telegram_chat_id: str,
    ) -> OlxMessage | None:
        """Resolve a Reply only inside the Telegram chat that received the notification."""

        result = await session.execute(
            select(OlxMessage).where(
                OlxMessage.telegram_message_id == telegram_message_id,
                OlxMessage.telegram_chat_id == telegram_chat_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, record_id: int) -> OlxMessage | None:
        """Load one persisted OLX message by internal primary key."""

        return await session.get(OlxMessage, record_id)


class TelegramUpdateRepository:
    """Insert Telegram updates idempotently by update ID."""

    async def add_if_new(
        self,
        session: AsyncSession,
        *,
        update_id: int,
    ) -> tuple[TelegramUpdate, bool]:
        statement = (
            sqlite_insert(TelegramUpdate)
            .values(
                update_id=update_id,
                received_at=utc_now(),
                status=TelegramUpdateStatus.RECEIVED,
            )
            .on_conflict_do_nothing(index_elements=["update_id"])
            .returning(TelegramUpdate.id)
        )
        result = await session.execute(statement)
        inserted_id = result.scalar_one_or_none()
        created = inserted_id is not None

        if created:
            record = await session.get(TelegramUpdate, inserted_id)
        else:
            existing = await session.execute(
                select(TelegramUpdate).where(TelegramUpdate.update_id == update_id)
            )
            record = existing.scalar_one_or_none()
        if record is None:
            raise RuntimeError("Telegram update insert did not produce a durable record")
        return record, created

    async def set_status(
        self,
        session: AsyncSession,
        *,
        update_id: int,
        status: TelegramUpdateStatus,
    ) -> TelegramUpdate:
        """Record the terminal processing state for an inbound update."""

        result = await session.execute(
            update(TelegramUpdate)
            .where(TelegramUpdate.update_id == update_id)
            .values(status=status, processed_at=utc_now())
            .returning(TelegramUpdate.id)
        )
        record_id = result.scalar_one_or_none()
        if record_id is None:
            raise RuntimeError("Telegram update was not found for status update")
        record = await session.get(TelegramUpdate, record_id)
        if record is None:
            raise RuntimeError("Telegram update status did not produce a durable record")
        return record


class OutboundMessageRepository:
    """Create durable Telegram-to-OLX delivery records."""

    async def create(
        self,
        session: AsyncSession,
        *,
        telegram_update_id: int,
        olx_chat_id: str,
        olx_reference_message_id: str,
        text: str,
        status: OutboundMessageStatus = OutboundMessageStatus.PENDING,
    ) -> OutboundMessage:
        record = OutboundMessage(
            telegram_update_id=telegram_update_id,
            olx_chat_id=olx_chat_id,
            olx_reference_message_id=olx_reference_message_id,
            text=text,
            status=status,
        )
        session.add(record)
        await session.flush()
        return record

    async def add_if_new(
        self,
        session: AsyncSession,
        *,
        telegram_update_id: int,
        olx_chat_id: str,
        olx_reference_message_id: str,
        text: str,
    ) -> tuple[OutboundMessage, bool]:
        """Persist one outbound operation idempotently by Telegram update ID."""

        statement = (
            sqlite_insert(OutboundMessage)
            .values(
                telegram_update_id=telegram_update_id,
                olx_chat_id=olx_chat_id,
                olx_reference_message_id=olx_reference_message_id,
                text=text,
                status=OutboundMessageStatus.PENDING,
                attempts=0,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            .on_conflict_do_nothing(index_elements=["telegram_update_id"])
            .returning(OutboundMessage.id)
        )
        result = await session.execute(statement)
        inserted_id = result.scalar_one_or_none()
        created = inserted_id is not None
        if created:
            record = await session.get(OutboundMessage, inserted_id)
        else:
            existing = await session.execute(
                select(OutboundMessage).where(
                    OutboundMessage.telegram_update_id == telegram_update_id
                )
            )
            record = existing.scalar_one_or_none()
        if record is None:
            raise RuntimeError("Outbound message insert did not produce a durable record")
        return record, created

    async def set_delivery_state(
        self,
        session: AsyncSession,
        *,
        record_id: int,
        status: OutboundMessageStatus,
        attempts: int,
        http_status: int | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
        sent_at: datetime | None = None,
    ) -> OutboundMessage:
        """Persist one delivery attempt without storing response bodies or tokens."""

        result = await session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == record_id)
            .values(
                status=status,
                attempts=attempts,
                olx_http_status=http_status,
                error_code=error_code,
                next_attempt_at=next_attempt_at,
                sent_at=sent_at,
                updated_at=utc_now(),
            )
            .returning(OutboundMessage.id)
        )
        updated_id = result.scalar_one_or_none()
        if updated_id is None:
            raise RuntimeError("Outbound message was not found for delivery update")
        record = await session.get(OutboundMessage, updated_id)
        if record is None:
            raise RuntimeError("Outbound delivery update did not produce a durable record")
        return record

    async def get_by_id(self, session: AsyncSession, record_id: int) -> OutboundMessage | None:
        """Load a durable Telegram-to-OLX message by primary key."""

        return await session.get(OutboundMessage, record_id)


class DeliveryJobRepository:
    """Persist, atomically claim, and finish external delivery work."""

    async def add_olx_to_telegram_if_new(
        self,
        session: AsyncSession,
        *,
        olx_message_id: int,
        max_attempts: int,
    ) -> tuple[DeliveryJob, bool]:
        """Enqueue one buyer notification idempotently by OLX message."""

        now = utc_now()
        statement = (
            sqlite_insert(DeliveryJob)
            .values(
                kind=DeliveryJobKind.OLX_TO_TELEGRAM,
                status=DeliveryJobStatus.PENDING,
                olx_message_id=olx_message_id,
                attempts=0,
                max_attempts=max_attempts,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["kind", "olx_message_id"])
            .returning(DeliveryJob.id)
        )
        result = await session.execute(statement)
        inserted_id = result.scalar_one_or_none()
        created = inserted_id is not None
        if created:
            record = await session.get(DeliveryJob, inserted_id)
        else:
            existing = await session.execute(
                select(DeliveryJob).where(
                    DeliveryJob.kind == DeliveryJobKind.OLX_TO_TELEGRAM,
                    DeliveryJob.olx_message_id == olx_message_id,
                )
            )
            record = existing.scalar_one_or_none()
        if record is None:
            raise RuntimeError("OLX-to-Telegram job insert did not produce a durable record")
        return record, created

    async def add_telegram_to_olx_if_new(
        self,
        session: AsyncSession,
        *,
        outbound_message_id: int,
        max_attempts: int,
    ) -> tuple[DeliveryJob, bool]:
        """Enqueue one mapped OLX Reply idempotently by outbound record."""

        now = utc_now()
        statement = (
            sqlite_insert(DeliveryJob)
            .values(
                kind=DeliveryJobKind.TELEGRAM_TO_OLX,
                status=DeliveryJobStatus.PENDING,
                outbound_message_id=outbound_message_id,
                attempts=0,
                max_attempts=max_attempts,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["kind", "outbound_message_id"])
            .returning(DeliveryJob.id)
        )
        result = await session.execute(statement)
        inserted_id = result.scalar_one_or_none()
        created = inserted_id is not None
        if created:
            record = await session.get(DeliveryJob, inserted_id)
        else:
            existing = await session.execute(
                select(DeliveryJob).where(
                    DeliveryJob.kind == DeliveryJobKind.TELEGRAM_TO_OLX,
                    DeliveryJob.outbound_message_id == outbound_message_id,
                )
            )
            record = existing.scalar_one_or_none()
        if record is None:
            raise RuntimeError("Telegram-to-OLX job insert did not produce a durable record")
        return record, created

    @staticmethod
    def _claimable(now: datetime, stale_before: datetime):
        return or_(
            and_(
                DeliveryJob.status.in_((DeliveryJobStatus.PENDING, DeliveryJobStatus.RETRY)),
                DeliveryJob.next_attempt_at <= now,
            ),
            and_(
                DeliveryJob.status == DeliveryJobStatus.PROCESSING,
                DeliveryJob.locked_at <= stale_before,
            ),
        )

    async def claim_next(
        self,
        session: AsyncSession,
        *,
        lock_token: str,
        lock_timeout_seconds: float,
        now: datetime | None = None,
    ) -> DeliveryJob | None:
        """Atomically claim one due or stale job and increment its attempt count."""

        timestamp = now or utc_now()
        stale_before = timestamp - timedelta(seconds=lock_timeout_seconds)
        claimable = self._claimable(timestamp, stale_before)
        candidate = (
            select(DeliveryJob.id)
            .where(claimable)
            .order_by(DeliveryJob.next_attempt_at, DeliveryJob.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(DeliveryJob)
            .where(DeliveryJob.id == candidate, claimable)
            .values(
                status=DeliveryJobStatus.PROCESSING,
                attempts=DeliveryJob.attempts + 1,
                locked_at=timestamp,
                lock_token=lock_token,
                updated_at=timestamp,
            )
            .returning(DeliveryJob.id)
        )
        result = await session.execute(statement)
        record_id = result.scalar_one_or_none()
        if record_id is None:
            return None
        return await session.get(DeliveryJob, record_id)

    async def mark_succeeded(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        lock_token: str,
    ) -> DeliveryJob:
        """Finish a claimed job successfully only for its lock owner."""

        return await self._finish(
            session,
            job_id=job_id,
            lock_token=lock_token,
            status=DeliveryJobStatus.SUCCEEDED,
            error_code=None,
        )

    async def mark_failed(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        lock_token: str,
        error_code: str,
    ) -> DeliveryJob:
        """Finish a claimed job as a non-retryable failure."""

        return await self._finish(
            session,
            job_id=job_id,
            lock_token=lock_token,
            status=DeliveryJobStatus.FAILED,
            error_code=error_code,
        )

    async def mark_dead_letter(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        lock_token: str,
        error_code: str,
    ) -> DeliveryJob:
        """Finish an exhausted transient delivery in dead-letter state."""

        return await self._finish(
            session,
            job_id=job_id,
            lock_token=lock_token,
            status=DeliveryJobStatus.DEAD_LETTER,
            error_code=error_code,
        )

    async def _finish(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        lock_token: str,
        status: DeliveryJobStatus,
        error_code: str | None,
    ) -> DeliveryJob:
        timestamp = utc_now()
        result = await session.execute(
            update(DeliveryJob)
            .where(
                DeliveryJob.id == job_id,
                DeliveryJob.status == DeliveryJobStatus.PROCESSING,
                DeliveryJob.lock_token == lock_token,
            )
            .values(
                status=status,
                last_error_code=error_code,
                locked_at=None,
                lock_token=None,
                completed_at=timestamp,
                updated_at=timestamp,
            )
            .returning(DeliveryJob.id)
        )
        updated_id = result.scalar_one_or_none()
        if updated_id is None:
            raise RuntimeError("Delivery job lock ownership was lost")
        record = await session.get(DeliveryJob, updated_id)
        if record is None:
            raise RuntimeError("Delivery job completion did not produce a durable record")
        return record

    async def schedule_retry(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        lock_token: str,
        next_attempt_at: datetime,
        error_code: str,
    ) -> DeliveryJob:
        """Release a claimed job back to the due queue with persisted backoff."""

        timestamp = utc_now()
        result = await session.execute(
            update(DeliveryJob)
            .where(
                DeliveryJob.id == job_id,
                DeliveryJob.status == DeliveryJobStatus.PROCESSING,
                DeliveryJob.lock_token == lock_token,
            )
            .values(
                status=DeliveryJobStatus.RETRY,
                next_attempt_at=next_attempt_at,
                last_error_code=error_code,
                locked_at=None,
                lock_token=None,
                updated_at=timestamp,
            )
            .returning(DeliveryJob.id)
        )
        updated_id = result.scalar_one_or_none()
        if updated_id is None:
            raise RuntimeError("Delivery job lock ownership was lost")
        record = await session.get(DeliveryJob, updated_id)
        if record is None:
            raise RuntimeError("Delivery job retry did not produce a durable record")
        return record


class GmailReplyNotificationRepository:
    """Deduplicate reply alerts without retaining email contents or addresses."""

    async def get_or_create(
        self,
        session: AsyncSession,
        *,
        uid_validity: int,
        message_uid: int,
        message_id_hash: str,
    ) -> GmailReplyNotification:
        statement = (
            sqlite_insert(GmailReplyNotification)
            .values(
                uid_validity=uid_validity,
                message_uid=message_uid,
                message_id_hash=message_id_hash,
                attempts=0,
            )
            .on_conflict_do_nothing(
                index_elements=["uid_validity", "message_uid"],
            )
        )
        await session.execute(statement)
        result = await session.execute(
            select(GmailReplyNotification).where(
                GmailReplyNotification.uid_validity == uid_validity,
                GmailReplyNotification.message_uid == message_uid,
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise RuntimeError("Gmail reply notification upsert did not produce a durable record")
        return record

    async def mark_attempt(
        self,
        session: AsyncSession,
        *,
        record_id: int,
        notified: bool,
    ) -> GmailReplyNotification:
        values: dict[str, Any] = {
            "attempts": GmailReplyNotification.attempts + 1,
            "updated_at": utc_now(),
        }
        if notified:
            values["notified_at"] = utc_now()
        await session.execute(
            update(GmailReplyNotification)
            .where(GmailReplyNotification.id == record_id)
            .values(**values)
        )
        record = await session.get(GmailReplyNotification, record_id)
        if record is None:
            raise RuntimeError("Gmail reply notification record disappeared")
        return record

    async def has_notified(self, session: AsyncSession) -> bool:
        result = await session.execute(
            select(GmailReplyNotification.id)
            .where(GmailReplyNotification.notified_at.is_not(None))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


class AuditEventRepository:
    """Append secret-free audit metadata."""

    async def append(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        record = AuditEvent(
            event_type=event_type,
            correlation_id=correlation_id,
            metadata_json=metadata or {},
        )
        session.add(record)
        await session.flush()
        return record
