"""Persistent SQLite outbox worker for both bridge delivery directions."""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta

import httpx

from app.clients.olx import (
    OlxChatClient,
    OlxMessageBadRequest,
    OlxMessagePermanentError,
    OlxMessageTransientError,
    OlxMessageUnauthorized,
)
from app.clients.telegram import (
    TelegramClient,
    TelegramDeliveryError,
    TelegramDeliveryPermanentError,
    TelegramDeliveryTransientError,
)
from app.config import Settings
from app.db.models import (
    ConnectionStatus,
    DeliveryJob,
    DeliveryJobKind,
    OutboundMessage,
    OutboundMessageStatus,
    utc_now,
)
from app.db.repositories import (
    DeliveryJobRepository,
    OlxChatRepository,
    OlxCredentialRepository,
    OlxMessageRepository,
    OutboundMessageRepository,
)
from app.db.session import Database
from app.services.bridge import build_telegram_notification_from_parts
from app.services.security import TokenCipher, TokenDecryptionError

logger = logging.getLogger(__name__)

OLX_NOT_CONNECTED = "A OLX não está conectada. Autorize novamente a integração antes de responder."
OLX_BAD_REQUEST = "A OLX rejeitou a resposta. A mensagem não foi enviada."
OLX_REAUTHORIZATION = "A conexão com a OLX expirou. Reautorize a integração antes de responder."
OLX_UNAVAILABLE = "A OLX está temporariamente indisponível. A mensagem não foi enviada."


class DeliveryWorker:
    """Claim persistent jobs with ownership locks and deliver them asynchronously."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        http_client: httpx.AsyncClient,
        jobs: DeliveryJobRepository | None = None,
        messages: OlxMessageRepository | None = None,
        chats: OlxChatRepository | None = None,
        outbound: OutboundMessageRepository | None = None,
        credentials: OlxCredentialRepository | None = None,
        telegram: TelegramClient | None = None,
        olx_chat: OlxChatClient | None = None,
        worker_token: str | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.jobs = jobs or DeliveryJobRepository()
        self.messages = messages or OlxMessageRepository()
        self.chats = chats or OlxChatRepository()
        self.outbound = outbound or OutboundMessageRepository()
        self.credentials = credentials or OlxCredentialRepository()
        self.telegram = telegram or TelegramClient(settings, http_client)
        self.olx_chat = olx_chat or OlxChatClient(settings, http_client)
        self.worker_token = worker_token or secrets.token_hex(16)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start one background loop; repeated calls are harmless."""

        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="delivery-worker")

    async def stop(self) -> None:
        """Wake the loop and wait for an in-flight delivery to finish."""

        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        """Continuously drain bounded batches until graceful shutdown."""

        logger.info(
            "delivery_worker_started",
            extra={"event": "delivery_worker", "status": "started"},
        )
        try:
            while not self._stop_event.is_set():
                processed = 0
                for _ in range(self.settings.delivery_worker_batch_size):
                    if self._stop_event.is_set():
                        break
                    try:
                        claimed = await self.process_once()
                    except Exception:
                        logger.exception(
                            "delivery_worker_iteration_failed",
                            extra={"event": "delivery_worker", "status": "failed"},
                        )
                        break
                    if not claimed:
                        break
                    processed += 1
                if processed == 0 and not self._stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=self.settings.delivery_worker_poll_seconds,
                        )
                    except TimeoutError:
                        pass
        finally:
            logger.info(
                "delivery_worker_stopped",
                extra={"event": "delivery_worker", "status": "stopped"},
            )

    async def process_once(self) -> bool:
        """Claim and process at most one due job; return whether one was claimed."""

        async with self.database.session_factory.begin() as session:
            job = await self.jobs.claim_next(
                session,
                lock_token=self.worker_token,
                lock_timeout_seconds=self.settings.delivery_lock_timeout_seconds,
            )
        if job is None:
            return False

        if job.attempts > job.max_attempts:
            await self._dead_letter(job, "stale_lock_exhausted")
            return True

        try:
            if job.kind == DeliveryJobKind.OLX_TO_TELEGRAM:
                await self._deliver_to_telegram(job)
            elif job.kind == DeliveryJobKind.TELEGRAM_TO_OLX:
                await self._deliver_to_olx(job)
            else:
                await self._fail_job(job, "unknown_delivery_kind")
        except Exception:
            logger.exception(
                "delivery_job_unexpected_error",
                extra={
                    "event": "delivery_job",
                    "status": "unexpected_error",
                    "job_id": job.id,
                    "job_kind": job.kind.value,
                },
            )
            await self._retry_or_dead_letter(job, "worker_unexpected_error")
        return True

    async def _deliver_to_telegram(self, job: DeliveryJob) -> None:
        if job.olx_message_id is None:
            await self._fail_job(job, "missing_olx_message_reference")
            return

        async with self.database.session_factory() as session:
            message = await self.messages.get_by_id(session, job.olx_message_id)
            chat = (
                await self.chats.get_by_chat_id(session, message.chat_id)
                if message is not None
                else None
            )
        if message is None or chat is None:
            await self._fail_job(job, "missing_olx_message")
            return
        if message.telegram_message_id is not None:
            await self._succeed_job(job)
            return

        text = build_telegram_notification_from_parts(
            buyer_name=chat.buyer_name,
            list_id=message.list_id,
            message=message.text,
            chat_id=message.chat_id,
        )
        try:
            receipt = await self.telegram.send_message(text)
        except TelegramDeliveryPermanentError:
            await self._fail_job(job, "telegram_permanent_error")
            return
        except (TelegramDeliveryTransientError, TelegramDeliveryError):
            await self._retry_or_dead_letter(job, "telegram_transient_error")
            return

        async with self.database.session_factory.begin() as session:
            await self.messages.set_telegram_mapping(
                session,
                message_id=message.message_id,
                telegram_message_id=receipt.message_id,
                telegram_chat_id=receipt.chat_id,
            )
            await self.jobs.mark_succeeded(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
            )

    async def _load_access_token(self) -> str | None:
        async with self.database.session_factory() as session:
            credential = await self.credentials.get_current(session)
        if credential is None or credential.connection_status in {
            ConnectionStatus.DISCONNECTED,
            ConnectionStatus.REAUTHORIZATION_REQUIRED,
        }:
            return None
        encryption_key = self.settings.token_encryption_key.get_secret_value()
        if not encryption_key:
            return None
        try:
            return TokenCipher(encryption_key).decrypt(credential.access_token_encrypted)
        except (TokenDecryptionError, ValueError):
            return None

    async def _deliver_to_olx(self, job: DeliveryJob) -> None:
        if job.outbound_message_id is None:
            await self._fail_job(job, "missing_outbound_reference")
            return
        async with self.database.session_factory() as session:
            outbound = await self.outbound.get_by_id(session, job.outbound_message_id)
        if outbound is None:
            await self._fail_job(job, "missing_outbound_message")
            return
        if outbound.status == OutboundMessageStatus.SENT:
            await self._succeed_job(job)
            return

        access_token = await self._load_access_token()
        if access_token is None:
            await self._fail_olx_job(
                job,
                outbound,
                error_code="olx_not_connected",
                feedback=OLX_NOT_CONNECTED,
            )
            return

        await self._set_outbound_state(
            outbound,
            status=OutboundMessageStatus.SENDING,
            attempts=job.attempts,
        )
        try:
            await self.olx_chat.send_message(
                access_token=access_token,
                text_message=outbound.text,
                message_id=outbound.olx_reference_message_id,
                chat_id=outbound.olx_chat_id,
            )
        except OlxMessageBadRequest as error:
            await self._fail_olx_job(
                job,
                outbound,
                error_code="olx_bad_request",
                feedback=OLX_BAD_REQUEST,
                http_status=error.http_status,
            )
            return
        except OlxMessageUnauthorized as error:
            async with self.database.session_factory.begin() as session:
                await self.credentials.require_reauthorization(session)
            await self._fail_olx_job(
                job,
                outbound,
                error_code="olx_unauthorized",
                feedback=OLX_REAUTHORIZATION,
                http_status=error.http_status,
            )
            return
        except OlxMessagePermanentError as error:
            await self._fail_olx_job(
                job,
                outbound,
                error_code="olx_permanent_error",
                feedback=OLX_BAD_REQUEST,
                http_status=error.http_status,
            )
            return
        except OlxMessageTransientError as error:
            await self._retry_olx_job(job, outbound, error.http_status)
            return

        async with self.database.session_factory.begin() as session:
            await self.outbound.set_delivery_state(
                session,
                record_id=outbound.id,
                status=OutboundMessageStatus.SENT,
                attempts=job.attempts,
                http_status=200,
                sent_at=utc_now(),
            )
            await self.jobs.mark_succeeded(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
            )

    async def _set_outbound_state(
        self,
        outbound: OutboundMessage,
        *,
        status: OutboundMessageStatus,
        attempts: int,
        http_status: int | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> OutboundMessage:
        async with self.database.session_factory.begin() as session:
            return await self.outbound.set_delivery_state(
                session,
                record_id=outbound.id,
                status=status,
                attempts=attempts,
                http_status=http_status,
                error_code=error_code,
                next_attempt_at=next_attempt_at,
            )

    async def _retry_olx_job(
        self,
        job: DeliveryJob,
        outbound: OutboundMessage,
        http_status: int | None,
    ) -> None:
        if job.attempts >= job.max_attempts:
            async with self.database.session_factory.begin() as session:
                await self.outbound.set_delivery_state(
                    session,
                    record_id=outbound.id,
                    status=OutboundMessageStatus.DEAD_LETTER,
                    attempts=job.attempts,
                    http_status=http_status,
                    error_code="olx_transient_exhausted",
                )
                await self.jobs.mark_dead_letter(
                    session,
                    job_id=job.id,
                    lock_token=self.worker_token,
                    error_code="olx_transient_exhausted",
                )
            await self._send_feedback(OLX_UNAVAILABLE)
            return

        next_attempt_at = self._next_attempt_at(job.attempts)
        async with self.database.session_factory.begin() as session:
            await self.outbound.set_delivery_state(
                session,
                record_id=outbound.id,
                status=OutboundMessageStatus.RETRY,
                attempts=job.attempts,
                http_status=http_status,
                error_code="olx_transient",
                next_attempt_at=next_attempt_at,
            )
            await self.jobs.schedule_retry(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
                next_attempt_at=next_attempt_at,
                error_code="olx_transient",
            )

    async def _fail_olx_job(
        self,
        job: DeliveryJob,
        outbound: OutboundMessage,
        *,
        error_code: str,
        feedback: str,
        http_status: int | None = None,
    ) -> None:
        async with self.database.session_factory.begin() as session:
            await self.outbound.set_delivery_state(
                session,
                record_id=outbound.id,
                status=OutboundMessageStatus.FAILED,
                attempts=job.attempts,
                http_status=http_status,
                error_code=error_code,
            )
            await self.jobs.mark_failed(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
                error_code=error_code,
            )
        await self._send_feedback(feedback)

    async def _send_feedback(self, text: str) -> None:
        try:
            await self.telegram.send_message(text)
        except TelegramDeliveryError:
            logger.warning(
                "delivery_feedback_failed",
                extra={"event": "delivery_feedback", "status": "failed"},
            )

    def _next_attempt_at(self, attempts: int) -> datetime:
        delay = self.settings.delivery_backoff_seconds * (2 ** max(0, attempts - 1))
        return utc_now() + timedelta(seconds=delay)

    async def _retry_or_dead_letter(self, job: DeliveryJob, error_code: str) -> None:
        if job.attempts >= job.max_attempts:
            await self._dead_letter(job, error_code)
            return
        async with self.database.session_factory.begin() as session:
            await self.jobs.schedule_retry(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
                next_attempt_at=self._next_attempt_at(job.attempts),
                error_code=error_code,
            )

    async def _succeed_job(self, job: DeliveryJob) -> None:
        async with self.database.session_factory.begin() as session:
            await self.jobs.mark_succeeded(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
            )

    async def _fail_job(self, job: DeliveryJob, error_code: str) -> None:
        async with self.database.session_factory.begin() as session:
            await self.jobs.mark_failed(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
                error_code=error_code,
            )

    async def _dead_letter(self, job: DeliveryJob, error_code: str) -> None:
        async with self.database.session_factory.begin() as session:
            await self.jobs.mark_dead_letter(
                session,
                job_id=job.id,
                lock_token=self.worker_token,
                error_code=error_code,
            )
