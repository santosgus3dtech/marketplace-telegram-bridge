"""Bounded privacy retention for message, update, OAuth, and audit data."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select

from app.db.models import (
    AuditEvent,
    DeliveryJob,
    DeliveryJobStatus,
    OAuthState,
    OlxChat,
    OlxMessage,
    OutboundMessage,
    OutboundMessageStatus,
    TelegramUpdate,
    utc_now,
)
from app.db.session import Database

logger = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = (
    DeliveryJobStatus.SUCCEEDED,
    DeliveryJobStatus.FAILED,
    DeliveryJobStatus.DEAD_LETTER,
)
TERMINAL_OUTBOUND_STATUSES = (
    OutboundMessageStatus.SENT,
    OutboundMessageStatus.FAILED,
    OutboundMessageStatus.DEAD_LETTER,
)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Deletion counts safe to expose in operational logs."""

    delivery_jobs: int
    outbound_messages: int
    telegram_updates: int
    olx_messages: int
    olx_chats: int
    oauth_states: int
    audit_events: int


def affected_rows(result: object) -> int:
    """Normalize SQLAlchemy delete row counts for SQLite."""

    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount) if isinstance(rowcount, int) and rowcount > 0 else 0


class RetentionService:
    """Delete expired terminal data while preserving active delivery references."""

    def __init__(
        self,
        *,
        database: Database,
        message_retention_days: int,
        audit_retention_days: int,
    ) -> None:
        self.database = database
        self.message_retention_days = message_retention_days
        self.audit_retention_days = audit_retention_days

    async def prune(self) -> RetentionResult:
        """Apply retention in foreign-key-safe order inside one transaction."""

        now = utc_now()
        message_cutoff = now - timedelta(days=self.message_retention_days)
        audit_cutoff = now - timedelta(days=self.audit_retention_days)

        async with self.database.session_factory.begin() as session:
            jobs_result = await session.execute(
                delete(DeliveryJob).where(
                    DeliveryJob.status.in_(TERMINAL_JOB_STATUSES),
                    DeliveryJob.completed_at.is_not(None),
                    DeliveryJob.completed_at < message_cutoff,
                )
            )

            outbound_has_job = (
                select(DeliveryJob.id)
                .where(DeliveryJob.outbound_message_id == OutboundMessage.id)
                .exists()
            )
            outbound_result = await session.execute(
                delete(OutboundMessage).where(
                    OutboundMessage.status.in_(TERMINAL_OUTBOUND_STATUSES),
                    OutboundMessage.created_at < message_cutoff,
                    ~outbound_has_job,
                )
            )

            update_has_outbound = (
                select(OutboundMessage.id)
                .where(OutboundMessage.telegram_update_id == TelegramUpdate.update_id)
                .exists()
            )
            updates_result = await session.execute(
                delete(TelegramUpdate).where(
                    TelegramUpdate.received_at < message_cutoff,
                    ~update_has_outbound,
                )
            )

            message_has_outbound = (
                select(OutboundMessage.id)
                .where(OutboundMessage.olx_reference_message_id == OlxMessage.message_id)
                .exists()
            )
            message_has_job = (
                select(DeliveryJob.id).where(DeliveryJob.olx_message_id == OlxMessage.id).exists()
            )
            messages_result = await session.execute(
                delete(OlxMessage).where(
                    OlxMessage.received_at < message_cutoff,
                    ~message_has_outbound,
                    ~message_has_job,
                )
            )

            chat_has_message = (
                select(OlxMessage.id).where(OlxMessage.chat_id == OlxChat.chat_id).exists()
            )
            chats_result = await session.execute(delete(OlxChat).where(~chat_has_message))
            oauth_result = await session.execute(
                delete(OAuthState).where(OAuthState.expires_at < audit_cutoff)
            )
            audit_result = await session.execute(
                delete(AuditEvent).where(AuditEvent.created_at < audit_cutoff)
            )

        return RetentionResult(
            delivery_jobs=affected_rows(jobs_result),
            outbound_messages=affected_rows(outbound_result),
            telegram_updates=affected_rows(updates_result),
            olx_messages=affected_rows(messages_result),
            olx_chats=affected_rows(chats_result),
            oauth_states=affected_rows(oauth_result),
            audit_events=affected_rows(audit_result),
        )


class RetentionWorker:
    """Run privacy retention immediately and then at a bounded interval."""

    def __init__(self, *, service: RetentionService, interval_seconds: float) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="retention-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = await self.service.prune()
                logger.info(
                    "retention_cleanup_completed",
                    extra={
                        "event": "retention_cleanup",
                        "status": "succeeded",
                        "deleted_records": sum(
                            (
                                result.delivery_jobs,
                                result.outbound_messages,
                                result.telegram_updates,
                                result.olx_messages,
                                result.olx_chats,
                                result.oauth_states,
                                result.audit_events,
                            )
                        ),
                    },
                )
            except Exception:
                logger.exception(
                    "retention_cleanup_failed",
                    extra={"event": "retention_cleanup", "status": "failed"},
                )
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                pass
